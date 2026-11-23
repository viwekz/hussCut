"""Speech to text with word timestamps, through sherpa-onnx: Parakeet 0.6b for English, and one model per other
spoken language (see MODELS in fetch_model.py). Each language has its own copies of its model; one that has not
been used for a while is put away to free the memory (English stays).

It runs in one of two places. On this machine's processors (the default), or, when PEAKCUT_ASR_URL is set, on a
separate machine with a graphics card (studio/asr_worker.py), which does the same work many times faster. If that
machine cannot be reached, the pieces are done here instead, so transcription never stops because of it."""
import os
import queue
import re
import threading
import time

import numpy as np

from . import fetch_model

_lock = threading.Lock()

# Several copies of the speech model, each on a few cores, instead of one copy on all of them: one copy cannot keep
# a big machine busy, and several videos transcribing at once would queue behind each other inside it. On a small
# machine (under 8 cores) this is still a single copy, as before.
_CPUS = os.cpu_count() or 4
PROVIDER = os.environ.get("PEAKCUT_ASR_PROVIDER", "cpu").strip().lower() or "cpu"      # "cuda" on a graphics card
THREADS = int(os.environ.get("PEAKCUT_ASR_THREADS") or (4 if _CPUS >= 8 else max(2, _CPUS - 2)))
COPIES = int(os.environ.get("PEAKCUT_ASR_COPIES") or (1 if PROVIDER != "cpu" else max(1, _CPUS // THREADS) if _CPUS >= 8 else 1))
IDLE = int(os.environ.get("PEAKCUT_ASR_IDLE_MINUTES", "15")) * 60      # a language unused this long is put away


class _Pool:
    """The copies of one language's model: the idle ones wait in `free`; `made` counts all of them."""
    def __init__(self):
        self.free: "queue.LifoQueue" = queue.LifoQueue()
        self.made = 0
        self.used = time.time()


_pools: dict = {}


def _pool(lang: str) -> _Pool:
    """This language's copies, and a chance to put away other languages nobody has used for a while."""
    now = time.time()
    with _lock:
        for other, p in list(_pools.items()):
            if other not in (lang, fetch_model.DEFAULT) and now - p.used > IDLE and p.free.qsize() >= p.made:
                del _pools[other]
        p = _pools.get(lang)
        if p is None:
            p = _pools[lang] = _Pool()
        p.used = now
        return p


def unload(lang: str) -> bool:
    """Forget a language's copies (before its model is deleted). False if one is in use right now."""
    with _lock:
        p = _pools.get(lang)
        if p is not None:
            if p.free.qsize() < p.made:
                return False
            del _pools[lang]
    return True

# The machine with the graphics card, if there is one.
REMOTE = os.environ.get("PEAKCUT_ASR_URL", "").strip().rstrip("/")
REMOTE_TOKEN = os.environ.get("PEAKCUT_ASR_TOKEN", "").strip()
REMOTE_BATCH = int(os.environ.get("PEAKCUT_ASR_REMOTE_BATCH", "8"))         # pieces sent together: the card does them at once
REMOTE_PARALLEL = int(os.environ.get("PEAKCUT_ASR_REMOTE_PARALLEL", "4"))   # pieces of one video worked on at once
# The card has a ceiling: beyond a few requests at once it gets no faster. So only this many are sent to it at a
# time (all videos together), and the rest is done on this machine's own processors at the same moment: both are used.
REMOTE_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get("PEAKCUT_ASR_REMOTE_INFLIGHT", "6"))))
_remote_down_until = 0.0
_remote_lacks: dict = {}       # language -> until when: the card's machine has no model for it, so it is done here


def model_ready(lang: str = fetch_model.DEFAULT) -> bool:
    return fetch_model.ready(lang)


def _model_files(d) -> dict:
    """The model's three parts. A graphics card wants the full-precision files; processors the compact ones."""
    kinds = ("fp16", "", "int8") if PROVIDER != "cpu" else ("int8", "", "fp16")
    for k in kinds:
        names = {p: d / (f"{p}.{k}.onnx" if k else f"{p}.onnx") for p in ("encoder", "decoder", "joiner")}
        if all(f.is_file() for f in names.values()):
            return {p: str(f) for p, f in names.items()}
    raise FileNotFoundError(f"No speech model files in {d}")


_lib = {"mod": None}
_lib_lock = threading.Lock()


def _sherpa():
    """The speech library, loaded once. Several copies of the model start on parallel threads, so loading is done
    under a lock: two threads loading it at the same moment would trip over each other."""
    with _lib_lock:
        if _lib["mod"] is None:
            _lib["mod"] = _load_sherpa()
        return _lib["mod"]


def _load_sherpa():
    """On Windows a file whose full path is over 260 characters cannot be loaded, which happens when PeakCut sits in a
    deep folder ("DLL load failed ... The filename or extension is too long"). Then the library is loaded through
    the folder's short name (C:\\Users\\...\\SITE-P~1), which points at the same files, or else from a copy of it
    in a short folder (AppData\\Local\\PeakCut\\lib), made once."""
    import sys
    try:
        import sherpa_onnx
        return sherpa_onnx
    except ImportError as e:
        if os.name != "nt" or "too long" not in str(e).lower():
            raise
        first = e
    import ctypes
    import importlib
    import importlib.util
    import shutil
    from pathlib import Path

    def forget():
        for k in [m for m in sys.modules if m == "sherpa_onnx" or m.startswith("sherpa_onnx.")]:
            del sys.modules[k]
        importlib.invalidate_caches()

    def load_from(folder: str):
        forget()
        sys.path.insert(0, folder)          # stays first: later imports inside the library look here too
        try:
            return importlib.import_module("sherpa_onnx")
        except ImportError:
            sys.path.remove(folder)
            forget()
            return None

    forget()
    spec = importlib.util.find_spec("sherpa_onnx")
    pkg = Path(spec.origin).parent if spec and spec.origin else None
    if pkg is None:
        raise first
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(str(pkg.parent), buf, 1024) and len(buf.value) < len(str(pkg.parent)):
        mod = load_from(buf.value)
        if mod:
            return mod
    # A copy of the library in a short folder. Same version check each start, so an update replaces it.
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PeakCut" / "lib"
    stamp = base / "sherpa_onnx.from"
    want = f"{pkg}|{(pkg / '__init__.py').stat().st_mtime_ns}"
    try:
        if not (base / "sherpa_onnx").is_dir() or not stamp.is_file() or stamp.read_text(encoding="utf-8") != want:
            shutil.rmtree(base / "sherpa_onnx", ignore_errors=True)
            shutil.copytree(pkg, base / "sherpa_onnx")
            stamp.write_text(want, encoding="utf-8")
        mod = load_from(str(base))
        if mod:
            return mod
    except OSError:
        pass
    raise RuntimeError("PeakCut is in a folder too deep for Windows to load the speech model from. Move the PeakCut "
                       "folder somewhere short, like C:\\peakcut, and start it again.") from first


def _new(lang: str = fetch_model.DEFAULT):
    sherpa_onnx = _sherpa()
    # English is downloaded here, once, if it was never fetched. Other languages are only ever downloaded when
    # someone asks for them; here we only wait for a download already running.
    fetch_model.ensure(lang, start=lang == fetch_model.DEFAULT)
    d = fetch_model.folder(lang)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        **_model_files(d),
        tokens=str(d / "tokens.txt"),
        num_threads=THREADS,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
        model_type="nemo_transducer",
        provider=PROVIDER,
    )


def _take(lang: str = fetch_model.DEFAULT, wait: bool = True):
    """A free copy of the language's model: one that is idle, a new one while there is room for more, else wait for
    one (or, with wait=False, None)."""
    p = _pool(lang)
    try:
        return p.free.get_nowait()
    except queue.Empty:
        pass
    with _lock:
        make = p.made < COPIES
        if make:
            p.made += 1
    if make:
        try:
            return _new(lang)
        except Exception:
            with _lock:
                p.made -= 1
            raise
    return p.free.get() if wait else None


def _take_now(lang: str = fetch_model.DEFAULT):
    """A copy of the model if one is free (or can still be made) this instant, else None."""
    return _take(lang, wait=False)


def _give_back(rec, lang: str):
    _pool(lang).free.put(rec)


def _recognizer(lang: str = fetch_model.DEFAULT):
    """Kept for callers that only need the model loaded (warm-up)."""
    rec = _take(lang)
    _give_back(rec, lang)
    return rec


def _chunks(audio: np.ndarray, lo=12.0, hi=24.0):
    """Split at the quietest point between lo and hi seconds so words are not cut in half."""
    sr, hop = 16000, 800  # 50 ms frames
    n = len(audio)
    frames = n // hop
    if frames == 0:
        return
    x = audio[: frames * hop].reshape(frames, hop).astype(np.float32)
    rms = np.sqrt((x ** 2).mean(axis=1))
    pos = 0
    while pos < n:
        if n - pos <= hi * sr:
            yield pos, n
            return
        a, b = (pos + int(lo * sr)) // hop, (pos + int(hi * sr)) // hop
        cut = (a + int(np.argmin(rms[a:b]))) * hop + hop // 2
        yield pos, cut
        pos = cut


BATCH = int(os.environ.get("PEAKCUT_ASR_BATCH", "1"))   # pieces decoded together. Measured: no faster on CPU, since
# each piece already uses every core, so one at a time (which also keeps the words exactly as before).


ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")    # vowel marks and the stretching line (tatweel)


def _words(tokens, timestamps, a: int, b: int) -> list:
    out = []
    tokens = [ARABIC_MARKS.sub("", t) for t in tokens]       # Arabic captions read as people type them: no vowel marks
    off, seg_end = a / 16000.0, b / 16000.0
    cur, gap = None, False
    for tok, t in zip(tokens, timestamps):
        new_word = tok.startswith((" ", "▁")) or gap
        piece = tok.replace("▁", " ").strip()
        if not piece:
            gap = True      # a space on its own: whatever comes next starts a new word ("in" "120", not "in120")
            continue
        gap = False
        is_punct = all(not c.isalnum() for c in piece)
        if cur is None or (new_word and not is_punct):
            if cur:
                out.append(cur)
            cur = {"w": piece, "s": round(off + t, 2), "e": round(off + t + 0.12, 2)}
        else:
            cur["w"] += piece
            cur["e"] = round(off + t + 0.12, 2)
    if cur:
        cur["e"] = min(cur["e"], round(seg_end, 2))
        out.append(cur)
    return out


def decode_pieces(pieces: list, rec=None, lang: str = fetch_model.DEFAULT) -> list:
    """[(tokens, timestamps), ...] for float32 pieces of audio, done on this machine with one copy of the model."""
    streams = []
    rec = rec or _take(lang)       # held only for these pieces, so videos transcribing at once take turns fairly
    try:
        for seg in pieces:
            st = rec.create_stream()
            st.accept_waveform(16000, seg)
            streams.append(st)
        rec.decode_streams(streams)
    finally:
        _give_back(rec, lang)
    return [(list(st.result.tokens), [float(t) for t in st.result.timestamps]) for st in streams]


class RemoteLacks(Exception):
    """The card's machine has no model for this language (or is an older one that only knows English)."""


def _remote_ok(lang: str = fetch_model.DEFAULT) -> bool:
    return bool(REMOTE) and time.time() >= max(_remote_down_until, _remote_lacks.get(lang, 0.0))


def _decode_remote(pieces: list, lang: str = fetch_model.DEFAULT) -> list:
    """The same, on the machine with the graphics card. Audio goes as 16-bit sound, the answer comes back as text."""
    import httpx
    raw = [np.clip(seg * 32768.0, -32768, 32767).astype("<i2") for seg in pieces]
    r = httpx.post(REMOTE + "/decode", content=b"".join(x.tobytes() for x in raw), timeout=httpx.Timeout(120, connect=4),
                   headers={"Authorization": "Bearer " + REMOTE_TOKEN, "X-Lens": ",".join(str(len(x)) for x in raw),
                            "X-Lang": lang, "Content-Type": "application/octet-stream"})
    if r.status_code == 409:
        raise RemoteLacks(lang)
    r.raise_for_status()
    body = r.json()
    # An older machine ignores the language and would answer in English: only trust an answer that says which
    # language it was made in. (English is safe either way: that is what an older machine has.)
    if body.get("lang", fetch_model.DEFAULT) != lang:
        raise RemoteLacks(lang)
    got = body["pieces"]
    if len(got) != len(pieces):
        raise ValueError("wrong number of pieces came back")
    return [(p["tokens"], p["timestamps"]) for p in got]


def transcribe(audio: np.ndarray, on_progress=None, lang: str = fetch_model.DEFAULT) -> list:
    """Returns [{'w': word, 's': start_sec, 'e': end_sec}, ...] for the whole audio, heard as `lang`.
    Speech is cut at quiet moments into 12-24 s pieces; silent pieces are skipped."""
    global _remote_down_until
    lang = lang if fetch_model.known(lang) else fetch_model.DEFAULT
    words = []
    todo = []
    for a, b in _chunks(audio):
        seg = audio[a:b].astype(np.float32) / 32768.0
        if len(seg) < 4800 or float(np.sqrt((seg ** 2).mean())) < 0.003:
            continue  # silence: skip, saves time and avoids made-up words
        todo.append((a, b, seg))
    remote = _remote_ok(lang)
    size = max(1, REMOTE_BATCH if remote else BATCH)
    groups = [todo[i:i + size] for i in range(0, len(todo), size)]
    done = [0]

    def decode(group):
        global _remote_down_until
        segs = [seg for _, _, seg in group]
        got = None
        while got is None:
            # whichever is free first: a place on the card's machine, or a copy of the model on this one
            if remote and _remote_ok(lang) and REMOTE_SLOTS.acquire(blocking=False):
                try:
                    got = _decode_remote(segs, lang)
                except RemoteLacks:   # no model there for this language: done here for a while, English unaffected
                    _remote_lacks[lang] = time.time() + 600
                except Exception:  # noqa: BLE001 - the card's machine is away or busy: these pieces are done here
                    _remote_down_until = time.time() + 60
                finally:
                    REMOTE_SLOTS.release()
                continue
            rec = _take_now(lang)
            if rec is None and not (remote and _remote_ok(lang)):
                rec = _take(lang)                            # nothing else to wait for: wait for a copy here
            if rec is not None:
                got = []
                step = max(1, BATCH)
                try:
                    for i in range(0, len(segs), step):
                        got += decode_pieces(segs[i:i + step], rec, lang)
                        rec = None if i + step >= len(segs) else _take(lang)
                except Exception:
                    got = None
                    raise
                continue
            time.sleep(0.02)
        out = [_words(tok, ts, a, b) for (a, b, _), (tok, ts) in zip(group, got)]
        done[0] += 1
        if on_progress:
            on_progress(done[0] / len(groups))
        return out

    # Pieces are independent, so they go to every copy of the model that is free: a video alone on the server
    # uses all of it, and several videos share it piece by piece. The words come back in order.
    workers = REMOTE_PARALLEL if remote else COPIES
    if workers > 1 and len(groups) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(workers, len(groups)), thread_name_prefix="asr") as ex:
            results = list(ex.map(decode, groups))
    else:
        results = [decode(g) for g in groups]
    for out in results:
        for piece in out:
            for w in piece:
                if words and words[-1]["w"] == w["w"] and words[-1]["s"] == w["s"]:
                    continue  # the model sometimes says the same word twice at the same moment
                words.append(w)
    # A word lasts until the next one starts, capped so pauses stay pauses.
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, max(w["e"], w["s"] + 0.6))), 2)
    return words
