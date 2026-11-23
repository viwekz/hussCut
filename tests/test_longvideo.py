"""A very long video must not need gigabytes of memory just to measure how loud each second is. Runs against the
code directly; nothing is downloaded and nothing outside this folder is touched."""
import sys
import tracemalloc

import numpy as np

sys.path.insert(0, ".")
from studio import media  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


def all_at_once(audio):
    """The way it used to be done, kept here to compare with."""
    n = len(audio) // 16000
    x = audio[: n * 16000].reshape(n, 16000).astype(np.float32) / 32768.0
    return (20 * np.log10(np.sqrt((x ** 2).mean(axis=1) + 1e-10))).astype(np.float32)


rng = np.random.default_rng(1)
audio = (rng.standard_normal(16000 * 1330 + 123) * rng.uniform(100, 9000, 16000 * 1330 + 123)).astype(np.int16)   # 22 minutes, three pieces
got, want = media.loudness_per_second(audio), all_at_once(audio)
check("the answer is the same as measuring all at once", len(got) == 1330 and np.allclose(got, want, atol=1e-4), f"{len(got)} {np.abs(got - want).max():.5f}")
check("a piece boundary is not a seam", np.allclose(got[595:605], want[595:605], atol=1e-4))
check("a very short sound still works", len(media.loudness_per_second(np.zeros(100, dtype=np.int16))) == 1)
check("silence is very quiet", media.loudness_per_second(np.zeros(32000, dtype=np.int16))[0] < -90)

# six hours of sound: the old way would ask for 1.4 GB here (5 GB for a day); this must stay small
day = np.zeros(16000 * 21600, dtype=np.int16)            # 0.7 GB of silence
tracemalloc.start()
out = media.loudness_per_second(day)
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
check("a very long video is measured in a small amount of memory", len(out) == 21600 and peak < 200 * 1024 * 1024, f"{peak / 1e6:.0f} MB")

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
