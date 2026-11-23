"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import audio, config, edl, roman, speaker
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


LAYOUTS = {"blur": "Full frame + blurred fill",
           "facecam_split": "Auto Facecam Split (Creator Top + Gameplay Bottom)",
           "split_cam_bottom": "Auto Facecam Split (Gameplay Top + Creator Bottom)",
           "split_cam_pip": "Facecam PIP (Corner Cam + Full Gameplay)",
           "split_cam_custom": "Custom Split Layout",
           "speaker_track": "Auto-Reframe (Active Speaker Tracking)",
           "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "left": "Fill the screen, keep the left side", "right": "Fill the screen, keep the right side",
           "fit": "Full frame on black", "split": "Split screen: left half on top, right half below (two people)",
           "gameplay": "Gameplay fills the screen, facecam in the corner"}
SPLIT_LAYOUTS = ("facecam_split", "split_cam", "split_cam_top", "split_cam_bottom", "split_cam_pip", "split_cam_custom")
_FILLS = ("crop", "left", "right", "gameplay", "speaker_track", "facecam_split", "split_cam", "split_cam_top", "split_cam_bottom", "split_cam_pip", "split_cam_custom")      # layouts where the picture covers the whole frame


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout not in LAYOUTS:
        layout = "blur"
    if layout in ("blur", "fit"):
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout in SPLIT_LAYOUTS:
        return layout, H, 220, 1350
    if layout in _FILLS:
        return layout, fg_h, 300, 1380
    if layout == "split":
        return layout, fg_h, 230, H // 2        # captions sit on the line between the two people
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    f, size = _Font.get(HOOK["font"]), HOOK["size"]
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > HOOK["max_w"] and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, caption_style=None) -> str:
    st = dict(STYLES.get(style_name, STYLES["bounce"]))
    if isinstance(caption_style, dict):
        if caption_style.get("font") and caption_style["font"] in FONT_FILES:
            st["font"] = caption_style["font"]
        if caption_style.get("size"):
            try:
                st["size"] = max(40, min(220, int(caption_style["size"])))
            except (ValueError, TypeError):
                pass
        if caption_style.get("active_color"):
            c = str(caption_style["active_color"]).lstrip("#")
            if len(c) == 6:
                st["active"] = c[4:6] + c[2:4] + c[0:2]
        if caption_style.get("base_color"):
            c = str(caption_style["base_color"]).lstrip("#")
            if len(c) == 6:
                st["base"] = c[4:6] + c[2:4] + c[0:2]
        if caption_style.get("y_pos") is not None:
            try:
                cap_y = max(100, min(1800, int(caption_style["y_pos"])))
            except (ValueError, TypeError):
                pass
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(_clean(hook), dur, hook_y)
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        if not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44, min(int(st["size"] * 0.7), int(1500 / max(1, len(line)) * 1.6)))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({W // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > CAP_MAX_W:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (W - total) / 2
        p = st["pop"]
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:                                                         # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


PIP_W = 380            # how wide the facecam is in the 1080-wide frame
PIP_MARGIN = 26


def _filter(layout, fg_h, facecam=None, clip=None):
    if layout in SPLIT_LAYOUTS:
        opts = clip or {}
        preset = opts.get("split_preset") or ("bottom" if layout == "split_cam_bottom" else "pip" if layout == "split_cam_pip" else "custom" if layout == "split_cam_custom" else "top")
        ratio = float(opts.get("split_ratio") or (0.55 if preset == "bottom" else 0.45))
        ratio = max(0.20, min(0.80, ratio))
        h_top = int(round(H * ratio / 2.0)) * 2
        h_bot = H - h_top
        cam_zoom = max(1.0, min(3.0, float(opts.get("cam_zoom") or 1.0)))
        game_zoom = max(1.0, min(3.0, float(opts.get("game_zoom") or 1.0)))
        game_x = max(0.0, min(1.0, float(opts.get("game_x") if opts.get("game_x") is not None else 0.5)))

        if facecam and len(facecam) == 4 and int(facecam[2]) > 8 and int(facecam[3]) > 8:
            fx, fy, fw, fh = (int(round(float(v))) for v in facecam)
        else:
            fx, fy, fw, fh = 0, 0, 576, 432

        if cam_zoom > 1.0:
            zfw = max(16, int(round(fw / cam_zoom / 2.0)) * 2)
            zfh = max(16, int(round(fh / cam_zoom / 2.0)) * 2)
            zfx = max(0, int(round(fx + (fw - zfw) / 2.0)))
            zfy = max(0, int(round(fy + (fh - zfh) / 2.0)))
        else:
            zfx, zfy, zfw, zfh = fx, fy, fw, fh

        if preset == "pip":
            pip_w = 460
            pip_h = max(32, int(round(pip_w * zfh / zfw / 2.0)) * 2)
            return (f"[0:v]fps={config.OUT_FPS},split=2[bg_src][fc_src];"
                    f"[bg_src]scale=trunc({W}*{game_zoom}/2)*2:trunc({H}*{game_zoom}/2)*2:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H}:(in_w-{W})*{game_x}:(in_h-{H})/2,setsar=1[bg];"
                    f"[fc_src]crop={zfw}:{zfh}:{zfx}:{zfy},scale={pip_w}:{pip_h},setsar=1,"
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.6:t=4[fc];"
                    f"[bg][fc]overlay=(W-{pip_w})/2:96[v0];")
        elif preset == "bottom":
            return (f"[0:v]fps={config.OUT_FPS},split=2[game_src][cam_src];"
                    f"[game_src]scale=trunc({W}*{game_zoom}/2)*2:trunc({h_top}*{game_zoom}/2)*2:force_original_aspect_ratio=increase,"
                    f"crop={W}:{h_top}:(in_w-{W})*{game_x}:(in_h-{h_top})/2,setsar=1[game];"
                    f"[cam_src]crop={zfw}:{zfh}:{zfx}:{zfy},scale={W}:{h_bot}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{h_bot},setsar=1[cam];"
                    f"[game][cam]vstack=inputs=2,drawbox=y={h_top-2}:color=black@0.9:width=iw:height=4:t=fill,setsar=1[v0];")
        else:
            return (f"[0:v]fps={config.OUT_FPS},split=2[cam_src][game_src];"
                    f"[cam_src]crop={zfw}:{zfh}:{zfx}:{zfy},scale={W}:{h_top}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{h_top},setsar=1[cam];"
                    f"[game_src]scale=trunc({W}*{game_zoom}/2)*2:trunc({h_bot}*{game_zoom}/2)*2:force_original_aspect_ratio=increase,"
                    f"crop={W}:{h_bot}:(in_w-{W})*{game_x}:(in_h-{h_bot})/2,setsar=1[game];"
                    f"[cam][game]vstack=inputs=2,drawbox=y={h_top-2}:color=black@0.9:width=iw:height=4:t=fill,setsar=1[v0];")

    if layout == "speaker_track":
        spk_x = float((clip or {}).get("speaker_x", 0.5))
        return (f"[0:v]fps={config.OUT_FPS},scale=-2:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:min(max(0, (in_w-{W})*{spk_x:.3f}), in_w-{W}):0,setsar=1[v0];")

    if layout == "gameplay" and facecam:
        # The game fills the screen, and the webcam sits in the top corner of it, so a viewer sees
        # the play and the face at the same time.
        fx, fy, fw, fh = (int(v) for v in facecam)
        if fw > 8 and fh > 8:
            pip_h = max(2, int(round(PIP_W * fh / fw)))
            return (f"[0:v]fps={config.OUT_FPS},split=2[base][fc];"
                    f"[base]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[b];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={PIP_W}:{pip_h},setsar=1[p];"
                    f"[b][p]overlay={W - PIP_W - PIP_MARGIN}:{PIP_MARGIN}[v0];")
    fill = f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if layout == "crop" or (layout == "gameplay" and not facecam):
        return fill + ",setsar=1[v0];"
    if layout == "left":
        return fill + ":0:0,setsar=1[v0];"
    if layout == "right":
        return fill + ":in_w-out_w:0,setsar=1[v0];"
    if layout == "fit":
        return f"[0:v]fps={config.OUT_FPS},scale={W}:{fg_h},pad={W}:{H}:0:(oh-ih)/2:black,setsar=1[v0];"
    if layout == "split":
        half = f"scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2}"
        return (f"[0:v]fps={config.OUT_FPS},split=2[l][r];[l]crop=iw/2:ih:0:0,{half}[t];"
                f"[r]crop=iw/2:ih:iw/2:0,{half}[u];[t][u]vstack,setsar=1[v0];")
    return (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v0];")


def _hook_y(layout, hook_y, facecam=None):
    """Where the heading goes. In the gameplay layout the webcam sits in the top corner, so a heading
    across the top would run under it: it goes just below the picture instead."""
    if layout in SPLIT_LAYOUTS:
        return 220
    if layout == "gameplay" and facecam and len(facecam) == 4 and facecam[2] > 8:
        return PIP_MARGIN + int(round(PIP_W * facecam[3] / facecam[2])) + 32
    return hook_y


_qsv_ok = None
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"))
    face = clip.get("facecam")
    if layout == "gameplay" and not (face and len(face) == 4):
        layout = "crop"     # no webcam found: the game fills the screen, which is still the right shape
    if layout == "speaker_track" and "speaker_x" not in clip:
        clip["speaker_x"] = speaker.track_speaker_center(src, start, end, src_w, src_h)

    hook_y = _hook_y(layout, hook_y, face)

    # Check for EDL cuts (excluded intervals)
    excluded = clip.get("edl", {}).get("excluded", [])
    corrections = clip.get("edl", {}).get("word_corrections", {})
    keep_segments = None
    clean_master = bool(clip.get("clean_master", False))
    caption_style = clip.get("caption_style")

    if excluded:
        keep_segments = edl.calculate_edl_segments(start, end, excluded)
        if len(keep_segments) > 1 or (len(keep_segments) == 1 and (keep_segments[0][0] > start + 0.05 or keep_segments[0][1] < end - 0.05)):
            recalculated_words = edl.calculate_recalculated_words(words, start, end, excluded, corrections)
            total_dur = sum(ke - ks for ks, ke in keep_segments)
            ass = build_ass(recalculated_words if clip.get("captions", True) and not clean_master else [],
                            0.0, total_dur, clip.get("hook", ""), clip.get("style", "bounce"),
                            clip.get("highlight_words", []), hook_y, cap_y, caption_style=caption_style)
        else:
            keep_segments = None
            ass = build_ass(words if clip.get("captions", True) and not clean_master else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                            clip.get("highlight_words", []), hook_y, cap_y, caption_style=caption_style)
    else:
        ass = build_ass(words if clip.get("captions", True) and not clean_master else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                        clip.get("highlight_words", []), hook_y, cap_y, caption_style=caption_style)

    work = Path(tempfile.mkdtemp(prefix="pk"))
    try:
        meta = {"title": clip.get("title") or "", "description": clip.get("description") or "", "comment": clip.get("description") or ""}
        return _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, meta=meta, clip=clip,
                          keep_segments=keep_segments, clean_master=clean_master)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, meta=None,
               clip=None, keep_segments=None, clean_master=False) -> dict:
    global _qsv_ok
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)

    clip_opts = clip or {}
    audio_cfg = clip_opts.get("audio", {})
    has_music = bool(audio_cfg.get("music_track"))
    music_track = audio_cfg.get("music_track")
    music_vol = float(audio_cfg.get("music_vol", 0.25))
    ducking = bool(audio_cfg.get("ducking", True))
    sfx_list = audio_cfg.get("sfx", [])
    studio_voice = bool(audio_cfg.get("studio_voice", False))

    inputs = []
    prep_filter = ""
    v_in = "[0:v]"
    a_in = "[0:a]"

    if keep_segments:
        inputs += ["-i", str(src)]
        trims_v = []
        trims_a = []
        for i, (ks, ke) in enumerate(keep_segments):
            trims_v.append(f"[0:v]trim=start={ks:.2f}:end={ke:.2f},setpts=PTS-STARTPTS[v_trim_{i}]")
            trims_a.append(f"[0:a]atrim=start={ks:.2f}:end={ke:.2f},asetpts=PTS-STARTPTS[a_trim_{i}]")
        concat_v_in = "".join(f"[v_trim_{i}]" for i in range(len(keep_segments)))
        concat_a_in = "".join(f"[a_trim_{i}]" for i in range(len(keep_segments)))
        prep_filter = ";".join(trims_v + trims_a) + f";{concat_v_in}concat=n={len(keep_segments)}:v=1:a=0[v_pre];{concat_a_in}concat=n={len(keep_segments)}:v=0:a=1[a_pre];"
        v_in = "[v_pre]"
        a_in = "[a_pre]"
    else:
        inputs += ["-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src)]

    raw_layout_fc = _filter(layout, fg_h, face, clip=clip)
    if v_in != "[0:v]":
        raw_layout_fc = raw_layout_fc.replace("[0:v]", v_in)

    v_filter = prep_filter + raw_layout_fc
    cur_v = "[v0]"

    if not clean_master and clip_opts.get("captions", True):
        v_filter += f"{cur_v}ass={name}.ass:fontsdir=f[v_sub];"
        cur_v = "[v_sub]"

    # Visual overlays
    overlays = clip_opts.get("overlays", [])
    for ov_idx, ov in enumerate(overlays):
        ov_path = Path(ov.get("src", ""))
        if ov_path.exists():
            inputs += ["-i", str(ov_path)]
            next_in_idx = (len(inputs) - 1) if not keep_segments else len(inputs) - 1
            ov_s = float(ov.get("start", 0.0))
            ov_e = float(ov.get("end", 9999.0))
            ov_x = ov.get("x", "(W-w)/2")
            ov_y = ov.get("y", "(H-h)/2")
            next_v = f"[v_ov_{ov_idx}]"
            v_filter += f"[{next_in_idx}:v]scale=trunc(iw*0.4/2)*2:-2[ov_scaled_{ov_idx}];{cur_v}[ov_scaled_{ov_idx}]overlay={ov_x}:{ov_y}:enable='between(t,{ov_s},{ov_e})'{next_v};"
            cur_v = next_v

    # Audio filtergraph
    music_file = audio.MUSIC_DIR / music_track if has_music and music_track else None
    if has_music and music_file and music_file.exists():
        inputs += ["-i", str(music_file)]
        music_in_idx = (len(inputs) - 1) if not keep_segments else len(inputs) - 1
        af_fc, a_out = audio.build_audio_filtergraph(
            has_music=True,
            music_filename=music_track,
            music_vol=music_vol,
            ducking=ducking,
            sfx_list=sfx_list,
            studio_voice=studio_voice,
            voice_input=a_in,
            music_input=f"[{music_in_idx}:a]"
        )
        total_fc = v_filter.rstrip(";") + ";" + af_fc
        a_map = a_out
    else:
        if studio_voice:
            af_fc, a_out = audio.build_audio_filtergraph(
                has_music=False,
                studio_voice=True,
                voice_input=a_in,
            )
            total_fc = v_filter.rstrip(";") + ";" + af_fc
            a_map = a_out
        else:
            total_fc = v_filter.rstrip(";")
            a_map = a_in

    base = ["ffmpeg", "-y", "-v", "error", *inputs,
            "-filter_complex", total_fc,
            "-map", cur_v, "-map", a_map]
    if not (has_music or studio_voice):
        base += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    base += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    for k, v in (meta or {}).items():
        if v:
            base += ["-metadata", f"{k}={v}"]
    base += ["-movflags", "+faststart", "-pix_fmt"]
    encoders = []
    if _qsv_ok is not False:
        encoders.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]))
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"]))
    err = None
    for label, enc in encoders:
        p = subprocess.run(base + enc + [f"{name}.mp4"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=RENDER_SECONDS)
        if p.returncode == 0:
            if label == "qsv":
                _qsv_ok = True
            break
        err = p.stderr[-1200:]
        if label == "qsv":
            _qsv_ok = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    cover_t = min(1.2, (end - start) / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        p_file = work / f"{name}.{ext}"
        if p_file.exists():
            shutil.move(str(p_file), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
