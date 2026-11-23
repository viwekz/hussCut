"""The recording buffer knows where each piece of the stream sits.

A Twitch stream drops pieces when it reconnects. Adding the lengths of the pieces up instead of reading the
times ffmpeg writes made the buffer fall behind the clock the clips ask for, until a moment that was still in
the buffer looked long gone: every clip from that stream failed with "no longer in the buffer".

Runs against the code directly, with its own empty data folder. No recording, no network."""
import os, sys, tempfile, time
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-times-")
sys.path.insert(0, ".")
from studio import live  # noqa: E402

fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}"))
    ok or fails.append(n)


def session(buffer_min=10):
    d = tempfile.mkdtemp(prefix="pc-times-live-")
    s = live.Session({"title": "a stream", "uploader": "someone", "dir": d, "creator": "", "ai": {},
                      "source": {"url": "", "name": "a stream"}},
                     {"sleep": 0, "buffer_min": buffer_min})
    return s


def pieces(s, rows, part=1):
    """What ffmpeg's own list says: name, where this piece starts, where it ends."""
    lines = []
    for i, (a, b) in enumerate(rows):
        name = f"p{part:02d}_{len(s.segments) + i:05d}.ts"
        (s.buf / name).write_bytes(b"x")
        lines.append(f"{name},{a},{b}")
    path = s.buf / f"p{part:02d}.csv"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(old + "\n".join(lines) + "\n", encoding="utf-8")
    s.part = max(s.part, part)
    s._scan_segments()


# ---- the times are the ones ffmpeg wrote, not a running total
s = session()
pieces(s, [(0, 6), (6, 12), (12, 18)])
check("a piece keeps the time it was written with", [(a, b) for _, a, b in s.segments], [(0, 6), (6, 12), (12, 18)])
check("the clock is the end of the last piece", s.seg_clock, 18)

# ---- a dropped stretch of the stream does not shift everything after it
s = session()
pieces(s, [(0, 6), (6, 12)])
pieces(s, [(7200, 7206), (7206, 7212)])
check("a gap leaves the real times alone", [(a, b) for _, a, b in s.segments][-2:], [(7200, 7206), (7206, 7212)])
check("the clock follows the stream, not the pieces", s.seg_clock, 7212)

# ---- the moment a clip asks for is found: this is what failed on the real Twitch stream
#     (the buffer holds only the last few minutes, and its times are stream times, not piece counts)
s = session()
pieces(s, [(7182, 7188), (7188, 7194), (7194, 7200), (7200, 7206), (7206, 7212)])
wanted = [(7190.0, 7217.0)]
segs = [x for x in s.segments if x[2] > wanted[0][0] - live.PAD and x[1] < wanted[0][1] + live.PAD]
check("an old moment is still found in the buffer", bool(segs), True)
check("it starts at the moment asked for", segs[0][1] <= wanted[0][0] + 2, True)

# ---- only the last buffer_min minutes are kept, measured on the stream clock
s = session(buffer_min=10)
pieces(s, [(0, 6), (6, 12), (600, 606), (606, 612)])
# (a piece that ends exactly at the edge of the buffer is kept: the edge is where the buffer starts)
check("pieces older than the buffer are dropped", [a for _, a, _ in s.segments], [6.0, 600.0, 606.0])

# ---- a second ffmpeg run starts its own list at zero: it is put back on the stream clock
s = session()
pieces(s, [(0, 6), (6, 12)])
pieces(s, [(0.0, 6.0), (6.0, 12.0)], part=2)
check("a list that begins again at zero is shifted", [(a, b) for _, a, b in s.segments], [(0, 6), (6, 12), (12, 18), (18, 24)])

# ---- a second run whose list carries on is left alone
s = session()
pieces(s, [(0, 6), (6, 12)])
pieces(s, [(12.0, 18.0), (18.0, 24.0)], part=2)
check("a list that carries on is not shifted twice", [(a, b) for _, a, b in s.segments], [(0, 6), (6, 12), (12, 18), (18, 24)])

# ---- after a restart, the pieces on disk still get their real times
job_dir = tempfile.mkdtemp(prefix="pc-times-recover-")
buf = Path(job_dir) / "buffer"
buf.mkdir(parents=True)
(buf / "p01_00000.ts").write_bytes(b"x")
(buf / "p01_00001.ts").write_bytes(b"x")
(buf / "p01.csv").write_text("p01_00000.ts,700,706\np01_00001.ts,706,712\n", encoding="utf-8")
(buf / "p02_00000.ts").write_bytes(b"x")
(buf / "p02.csv").write_text("p02_00000.ts,0,6\n", encoding="utf-8")
job = {"title": "a stream", "uploader": "someone", "dir": job_dir, "creator": "", "ai": {}, "clips": [],
       "source": {"url": "", "name": "a stream"}}
s = live.Session(job, {"buffer_min": 10})
live._pieces_on_disk(s)
check("a recovered recording keeps the real times", [(a, b) for _, a, b in s.segments],
      [(700, 706), (706, 712), (712, 718)])

# ---- a clip asked for by hand stays inside the recorded picture
s = session()
s.samples = live.SR * 300
s.seg_clock = 120.0
s.clip_last(30)
check("a hand clip ends where the picture ends, not where the sound is", s.manual[-1], {"start": 90.0, "end": 120.0})
s = session()
s.samples = live.SR * 120
s.seg_clock = 120.0
s.clip_last(30)
check("a hand clip is unaffected when the picture is up to date", s.manual[-1], {"start": 90.0, "end": 120.0})

# ---- the end of a clip has to be in the picture: a clip that stops before its payoff is not made
s = session()
check("a clip inside the recorded picture is left alone", live._clip_span({"start": 100, "end": 130}, 92, 138), (100, 130))
check("a clip a hair past the picture ends where the picture does", live._clip_span({"start": 100, "end": 130}, 92, 129.4), (100, 129.4))
check("a clip whose end was never recorded is not cut at all", live._clip_span({"start": 100, "end": 130}, 92, 106.5), None)
#     (this is what made a real 18 KB, 0.7 second clip out of a 30 second moment: the picture was a minute
#      behind and the clip was trimmed to the little that had arrived instead of being dropped)

# ---- a piece ffmpeg never named is not kept: no clip can use it, and it fills the buffer
s = session()
pieces(s, [(0, 6)])
orphan = s.buf / "p01_00009.ts"
orphan.write_bytes(b"x" * 32)
old = time.time() - 300
os.utime(orphan, (old, old))
running = s.buf / "p01_00010.ts"
running.write_bytes(b"x")
s.swept = 0
s._scan_segments()
check("a piece with no line in ffmpeg's list is removed", orphan.exists(), False)
check("a piece still being written stays", running.exists(), True)
check("a piece a clip uses stays", (s.buf / "p01_00000.ts").exists(), True)

print("\n" + (f"{len(fails)} failed" if fails else "All checks passed"))
sys.exit(1 if fails else 0)
