"""Edit Decision List (EDL) text-based video cuts, pause removal, filler word removal,
and subtitle (SRT/VTT) generation with recalculated timestamps."""
import re
from typing import List, Dict, Tuple

COMMON_FILLERS = {
    "um", "uh", "er", "ah", "like", "you know", "literally", "basically",
    "hmm", "huh", "yeah", "so", "actually", "right"
}


def clean_word(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower().strip())


def merge_ranges(ranges: List[List[float]]) -> List[List[float]]:
    """Merge overlapping or contiguous [start, end] ranges."""
    if not ranges:
        return []
    sorted_r = sorted([[round(float(s), 2), round(float(e), 2)] for s, e in ranges if e > s], key=lambda x: x[0])
    merged = []
    for cur in sorted_r:
        if not merged:
            merged.append(cur)
        else:
            prev = merged[-1]
            if cur[0] <= prev[1] + 0.05:  # overlap or touch
                prev[1] = max(prev[1], cur[1])
            else:
                merged.append(cur)
    return merged


def calculate_edl_segments(clip_start: float, clip_end: float, excluded: List[List[float]]) -> List[List[float]]:
    """Calculate the active [start, end] video segments to keep after cutting out excluded ranges."""
    clip_start = round(float(clip_start), 2)
    clip_end = round(float(clip_end), 2)
    if not excluded:
        return [[clip_start, clip_end]]

    # Filter and clamp excluded ranges inside [clip_start, clip_end]
    clamped = []
    for s, e in excluded:
        s, e = float(s), float(e)
        if e <= clip_start or s >= clip_end:
            continue
        clamped.append([max(clip_start, s), min(clip_end, e)])

    merged_ex = merge_ranges(clamped)
    if not merged_ex:
        return [[clip_start, clip_end]]

    keep_segments = []
    cur_t = clip_start
    for ex_s, ex_e in merged_ex:
        if ex_s > cur_t + 0.05:
            keep_segments.append([round(cur_t, 2), round(ex_s, 2)])
        cur_t = max(cur_t, ex_e)

    if cur_t < clip_end - 0.05:
        keep_segments.append([round(cur_t, 2), round(clip_end, 2)])

    return keep_segments or [[clip_start, clip_end]]


def calculate_recalculated_words(words: List[Dict], clip_start: float, clip_end: float,
                                 excluded: List[List[float]], word_corrections: Dict = None) -> List[Dict]:
    """Given original words and excluded cut intervals, produce recalculated word timestamps
    relative to the cut video start (0.0)."""
    corrections = word_corrections or {}
    merged_ex = merge_ranges([[max(clip_start, s), min(clip_end, e)] for s, e in excluded if e > clip_start and s < clip_end])

    def _is_excluded(s, e):
        mid = (s + e) / 2.0
        for ex_s, ex_e in merged_ex:
            if ex_s <= mid <= ex_e:
                return True
        return False

    def _cut_duration_before(t):
        cut_sec = 0.0
        for ex_s, ex_e in merged_ex:
            if ex_e <= t:
                cut_sec += (ex_e - ex_s)
            elif ex_s < t < ex_e:
                cut_sec += (t - ex_s)
        return cut_sec

    out_words = []
    for idx, w in enumerate(words):
        s, e = float(w["s"]), float(w["e"])
        if s < clip_start - 0.05 or e > clip_end + 0.05:
            continue
        if _is_excluded(s, e):
            continue

        # Correct word text if edited
        word_text = corrections.get(str(idx), corrections.get(idx, w.get("w", "")))
        if not word_text:
            continue

        # Recalculate timing relative to cut video output (starting at 0.0)
        shift_s = s - clip_start - _cut_duration_before(s)
        shift_e = e - clip_start - _cut_duration_before(e)
        if shift_e > shift_s:
            out_words.append({
                "w": word_text,
                "s": round(max(0.0, shift_s), 2),
                "e": round(max(0.01, shift_e), 2),
                "orig_s": s,
                "orig_e": e,
                "idx": idx,
            })

    return out_words


def find_pauses(words: List[Dict], clip_start: float, clip_end: float, min_gap: float = 0.5) -> List[List[float]]:
    """Identify silent gaps longer than min_gap seconds between spoken words."""
    in_words = sorted([w for w in words if clip_start <= w["s"] <= clip_end], key=lambda x: x["s"])
    if not in_words:
        return []
    pauses = []
    # Check leading pause
    if in_words[0]["s"] - clip_start >= min_gap:
        pauses.append([round(clip_start, 2), round(in_words[0]["s"] - 0.05, 2)])

    for i in range(len(in_words) - 1):
        w_cur, w_nxt = in_words[i], in_words[i + 1]
        gap = w_nxt["s"] - w_cur["e"]
        if gap >= min_gap:
            pauses.append([round(w_cur["e"] + 0.05, 2), round(w_nxt["s"] - 0.05, 2)])

    # Check trailing pause
    if clip_end - in_words[-1]["e"] >= min_gap:
        pauses.append([round(in_words[-1]["e"] + 0.05, 2), round(clip_end, 2)])

    return pauses


def find_filler_words(words: List[Dict], clip_start: float, clip_end: float) -> List[List[float]]:
    """Identify timestamp ranges for common hesitation words ('um', 'uh', 'like', etc.)."""
    ranges = []
    for w in words:
        if clip_start <= w["s"] <= clip_end:
            cw = clean_word(w.get("w", ""))
            if cw in COMMON_FILLERS:
                ranges.append([round(max(clip_start, w["s"] - 0.02), 2), round(min(clip_end, w["e"] + 0.02), 2)])
    return ranges


def format_srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def generate_srt(words: List[Dict], max_words_per_line: int = 4) -> str:
    """Generate standard .srt subtitle text grouped by lines."""
    if not words:
        return ""
    lines = []
    cur_group = []
    idx = 1
    for w in words:
        cur_group.append(w)
        if len(cur_group) >= max_words_per_line or w["w"][-1:] in ".?!":
            s_str = format_srt_time(cur_group[0]["s"])
            e_str = format_srt_time(cur_group[-1]["e"])
            text = " ".join(item["w"] for item in cur_group)
            lines.append(f"{idx}\n{s_str} --> {e_str}\n{text}\n")
            idx += 1
            cur_group = []
    if cur_group:
        s_str = format_srt_time(cur_group[0]["s"])
        e_str = format_srt_time(cur_group[-1]["e"])
        text = " ".join(item["w"] for item in cur_group)
        lines.append(f"{idx}\n{s_str} --> {e_str}\n{text}\n")
    return "\n".join(lines)


def generate_vtt(words: List[Dict], max_words_per_line: int = 4) -> str:
    """Generate standard WebVTT .vtt subtitle text."""
    header = "WEBVTT\n\n"
    if not words:
        return header
    lines = [header]
    cur_group = []
    for w in words:
        cur_group.append(w)
        if len(cur_group) >= max_words_per_line or w["w"][-1:] in ".?!":
            s_str = format_vtt_time(cur_group[0]["s"])
            e_str = format_vtt_time(cur_group[-1]["e"])
            text = " ".join(item["w"] for item in cur_group)
            lines.append(f"{s_str} --> {e_str}\n{text}\n\n")
            cur_group = []
    if cur_group:
        s_str = format_vtt_time(cur_group[0]["s"])
        e_str = format_vtt_time(cur_group[-1]["e"])
        text = " ".join(item["w"] for item in cur_group)
        lines.append(f"{s_str} --> {e_str}\n{text}\n\n")
    return "".join(lines)
