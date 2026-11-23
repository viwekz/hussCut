"""Active-Speaker Auto-Reframe: tracks the speaker's horizontal position across frames
to smoothly pan a 9:16 crop window in multi-person podcasts and streams."""
import subprocess
from pathlib import Path
from typing import List, Tuple

import numpy as np
from .media import NO_WINDOW

SW, SH = 320, 180


def track_speaker_center(src: Path, start: float, end: float, src_w: int, src_h: int) -> float:
    """Analyze keyframes across [start, end] and calculate the smoothed horizontal center (0.0 to 1.0)
    of the active speaker."""
    dur = max(1.0, end - start)
    # Sample up to 8 frames evenly spaced
    n_samples = min(8, max(3, int(dur // 2)))
    timestamps = [start + (i + 0.5) * (dur / n_samples) for i in range(n_samples)]

    centers = []
    for t in timestamps:
        cmd = [
            "ffmpeg", "-v", "error", "-ss", f"{t:.2f}",
            "-i", str(src),
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{SW}x{SH}",
            "pipe:1"
        ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            raw = p.stdout
            if len(raw) == SW * SH * 3:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((SH, SW, 3))
                r = frame[:, :, 0].astype(np.int32)
                g = frame[:, :, 1].astype(np.int32)
                b = frame[:, :, 2].astype(np.int32)

                # Skin chrominance / face detection heuristic in RGB
                skin = (r > 90) & (g > 40) & (b > 20) & (r > g) & (r > b) & ((r - g) > 12) & (r - b > 12)
                col_energy = skin.sum(axis=0)
                tot = col_energy.sum()
                if tot > 50:
                    cx = (np.arange(SW) * col_energy).sum() / tot
                    centers.append(cx / float(SW))
                else:
                    centers.append(0.5)
            else:
                centers.append(0.5)
        except Exception:
            centers.append(0.5)

    if not centers:
        return 0.5

    # Exponential smoothing
    smooth_x = centers[0]
    for c in centers[1:]:
        smooth_x = 0.35 * c + 0.65 * smooth_x

    # Clamp safely away from frame borders
    return max(0.18, min(0.82, float(smooth_x)))
