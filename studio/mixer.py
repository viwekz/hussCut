"""Multi-clip mixer and sequencer: stitches multiple AI clips or segments,
generates climax teaser intros, applies ffmpeg xfade transitions, and audio ducking."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import List, Dict

from . import audio, config
from .media import NO_WINDOW, run

ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


def add_climax_teaser_intro(items: List[Dict], get_clip_fn=None) -> List[Dict]:
    """1-Click Climax Teaser Intro Generator:
    Finds the highest-energy 2.5s segment from later in the sequence and prepends it at 00:00
    with a quick dissolve/zoom transition."""
    if not items:
        return items
    items_copy = [dict(it) for it in items]

    # Pick candidate from item with highest score or later in sequence
    best_item = None
    best_score = -1
    for it in items_copy:
        score = it.get("score") or it.get("viral_score") or 50
        if score > best_score:
            best_score = score
            best_item = it

    if not best_item:
        best_item = items_copy[-1]

    s = float(best_item.get("start", 0.0))
    e = float(best_item.get("end", s + 10.0))
    dur = max(2.0, min(3.0, (e - s) * 0.4))
    teaser_start = round(s + (e - s) * 0.35, 2)
    teaser_end = round(teaser_start + dur, 2)

    teaser = {
        "job_id": best_item.get("job_id"),
        "clip_id": best_item.get("clip_id"),
        "src_path": best_item.get("src_path"),
        "start": teaser_start,
        "end": teaser_end,
        "transition": "fade",
        "title": "Teaser: " + str(best_item.get("title", "Hook Intro")),
        "is_teaser": True,
    }
    return [teaser] + items_copy


def render_sequence(items: List[Dict], out_file: Path, aspect: str = "9:16",
                    music_settings: Dict = None, studio_voice: bool = False,
                    clean_master: bool = False) -> Path:
    """Render a stitched multi-clip sequence into out_file using ffmpeg."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_w, out_h = ASPECT_DIMS.get(aspect, (1080, 1920))

    if not items:
        raise ValueError("Sequence has no items to render.")

    work = Path(tempfile.mkdtemp(prefix="pkseq"))
    try:
        # Pre-render / normalize each segment to out_w x out_h and standard fps
        rendered_pieces = []
        for idx, it in enumerate(items):
            src = Path(it["src_path"])
            if not src.exists():
                raise FileNotFoundError(f"Source video piece not found: {src}")

            s, e = float(it["start"]), float(it["end"])
            dur = max(0.5, e - s)
            piece_path = work / f"piece_{idx:03d}.mp4"

            # Normalize video aspect, scale, crop, fps, and audio
            vf = (f"fps={config.OUT_FPS},scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                  f"crop={out_w}:{out_h},setsar=1")

            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{s:.2f}", "-t", f"{dur:.2f}",
                "-i", str(src),
                "-vf", vf,
                "-c:v", "libx264", "-crf", "20", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                str(piece_path)
            ]
            subprocess.run(cmd, cwd=work, check=True, creationflags=NO_WINDOW)
            rendered_pieces.append(piece_path)

        # Check if transitions are all hard cuts or if xfade is needed
        has_xfade = any(it.get("transition") and it["transition"] not in ("cut", "none", None) for it in items[1:])

        final_concat_piece = work / "concat.mp4"

        if not has_xfade or len(rendered_pieces) == 1:
            # Simple fast concat list
            concat_txt = work / "concat.txt"
            lines = [f"file '{p.name}'" for p in rendered_pieces]
            concat_txt.write_text("\n".join(lines), encoding="utf-8")
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0", "-i", "concat.txt",
                "-c", "copy",
                str(final_concat_piece)
            ], cwd=work, check=True, creationflags=NO_WINDOW)
        else:
            # Build filter_complex with xfade and acrossfade
            # For N inputs: [0:v][1:v]xfade=transition=fade:duration=0.5:offset=...[v1]; ...
            # Get durations of each piece
            durs = []
            for p in rendered_pieces:
                info = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(p)
                ], capture_output=True, text=True, creationflags=NO_WINDOW)
                try:
                    durs.append(float(info.stdout.strip()))
                except Exception:
                    durs.append(3.0)

            inputs_args = []
            for p in rendered_pieces:
                inputs_args.extend(["-i", str(p)])

            v_filters = []
            a_filters = []
            trans_dur = 0.5
            last_v = "[0:v]"
            last_a = "[0:a]"
            offset = durs[0] - trans_dur

            for i in range(1, len(rendered_pieces)):
                trans = items[i].get("transition") or "fade"
                xfade_type = "fade"
                if trans in ("slideleft", "slideright", "wipeleft", "wiperight"):
                    xfade_type = trans
                elif trans in ("zoomin", "circlecrop"):
                    xfade_type = "circlecrop"

                cur_v_out = f"[v_mix_{i}]"
                cur_a_out = f"[a_mix_{i}]"
                v_filters.append(f"{last_v}[{i}:v]xfade=transition={xfade_type}:duration={trans_dur}:offset={max(0.1, offset):.2f}{cur_v_out}")
                a_filters.append(f"{last_a}[{i}:a]acrossfade=d={trans_dur}{cur_a_out}")
                last_v = cur_v_out
                last_a = cur_a_out
                offset += (durs[i] - trans_dur)

            fc = ";".join(v_filters + a_filters)
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                *inputs_args,
                "-filter_complex", fc,
                "-map", last_v, "-map", last_a,
                "-c:v", "libx264", "-crf", "21", "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                str(final_concat_piece)
            ], cwd=work, check=True, creationflags=NO_WINDOW)

        # Apply background music & audio processing if configured
        final_target = work / "final.mp4"
        music_cfg = music_settings or {}
        has_music = bool(music_cfg.get("track"))
        music_file = audio.MUSIC_DIR / music_cfg.get("track", "") if has_music else None

        if has_music and music_file and music_file.exists():
            af_filter, a_out = audio.build_audio_filtergraph(
                has_music=True,
                music_filename=music_cfg.get("track"),
                music_vol=float(music_cfg.get("volume", 0.25)),
                ducking=bool(music_cfg.get("ducking", True)),
                studio_voice=studio_voice,
                voice_input="[0:a]",
                music_input="[1:a]",
            )
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-i", str(final_concat_piece),
                "-i", str(music_file),
                "-filter_complex", af_filter,
                "-map", "0:v", "-map", a_out,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                str(final_target)
            ], cwd=work, check=True, creationflags=NO_WINDOW)
        elif studio_voice:
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-i", str(final_concat_piece),
                "-af", "highpass=f=80,acompressor=threshold=-20dB:ratio=4:attack=5:release=50,loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                str(final_target)
            ], cwd=work, check=True, creationflags=NO_WINDOW)
        else:
            final_target = final_concat_piece

        # Move to destination
        shutil.move(str(final_target), str(out_file))
        return out_file
    finally:
        shutil.rmtree(work, ignore_errors=True)
