"""Saving a video's progress must not stop the video when Windows says "Access is denied" for a moment (a virus
scanner, the indexer or another thread has the file open). Runs against the code directly, with its own empty data folder."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-jobsave-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import auth, ctx, db, jobs  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


u, _ = auth._create("js@example.com", "Js", password_hash="x")
real_replace = Path.replace


def busy_for(times):
    """Path.replace that is refused `times` times, the way Windows does, and then works."""
    left = [times]

    def replace(self, target):
        if left[0] > 0:
            left[0] -= 1
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)
    return replace


with ctx.as_user(u.id):
    job = jobs.create({"type": "url", "url": "https://example.com/v", "name": "v"}, {}, enqueue=False)
    path = Path(job["dir"]) / "job.json"

    with patch.object(Path, "replace", busy_for(3)):
        jobs._set(job, progress=0.5)
    check("a save refused a few times still goes through", json.loads(path.read_text(encoding="utf-8"))["progress"] == 0.5)

    with patch.object(Path, "replace", busy_for(10 ** 6)):
        jobs._set(job, progress=0.7)
    check("a file that stays busy is written in place instead", json.loads(path.read_text(encoding="utf-8"))["progress"] == 0.7)
    check("...and no half-written file is left behind", not (Path(job["dir"]) / "job.json.tmp").exists())

    with patch.object(Path, "replace", busy_for(10 ** 6)), patch.object(Path, "write_text", side_effect=[None, PermissionError(5, "busy")]):
        try:
            jobs.save(job)
            ok = True
        except OSError:
            ok = False
    check("a save that cannot be done at all does not stop the video", ok)

    jobs._set(job, progress=0.9)
    check("the next save works as normal", json.loads(path.read_text(encoding="utf-8"))["progress"] == 0.9)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil  # noqa: E402

db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
