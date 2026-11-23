"""The speech-to-text models, one per spoken language, each downloaded once.

English comes first and by itself, the first time it is needed (about 480 MB). The others are only downloaded when
someone asks for them, from Settings -> Speech models or when adding a video in that language. To add a language,
add a row to MODELS: any NVIDIA NeMo transducer model exported for sherpa-onnx works the same way.

A model is unpacked next to where it will live and moved into place only when it is whole, so a download that was
cut off can never leave half a model behind. When a model's fingerprint (SHA-256) is known, the downloaded file is
checked against it before anything is unpacked. (python -m studio.fetch_model [language] gets one ahead of time.)"""
import hashlib
import shutil
import sys
import tarfile
import threading
import time
import urllib.request

from . import config, safe

_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# language -> (name shown on the pages, the model's folder name, where it is downloaded from, size in MB, credit,
#              the archive's SHA-256 fingerprint, or "" when it is not checked)
# The folder name must also be the one folder inside the downloaded .tar.bz2.
MODELS = {
    "en": ("English", config.ASR_MODEL_NAME, _SHERPA + config.ASR_MODEL_NAME + ".tar.bz2", 480,
           "Parakeet TDT 0.6b v2 by NVIDIA (CC-BY-4.0)", ""),
    "ar": ("Arabic", config.ASR_MODEL_AR, config.ASR_MODEL_AR_URL, 107,
           "FastConformer Arabic by NVIDIA (CC-BY-4.0)", config.ASR_MODEL_AR_SHA256),
}
DEFAULT = "en"
WAIT = 60   # seconds of silence from the connection before giving up, so a stalled download cannot hang everything

_locks = {lang: threading.Lock() for lang in MODELS}
state = {lang: {"doing": False, "percent": 0, "error": ""} for lang in MODELS}


class WrongFile(Exception):
    """The downloaded file is not the one expected: its fingerprint does not match."""


def known(lang) -> bool:
    return lang in MODELS


def label(lang) -> str:
    return MODELS[lang][0] if lang in MODELS else str(lang)


def folder(lang: str = DEFAULT):
    return config.MODELS_DIR / MODELS[lang][1]


def ready(lang: str = DEFAULT) -> bool:
    """Is the whole model there: the word list and all three parts?"""
    d = folder(lang)
    if not (d / "tokens.txt").is_file():
        return False
    return all(any(d.glob(f"{part}*.onnx")) for part in ("encoder", "decoder", "joiner"))


def status(lang: str) -> str:
    """'ready', 'downloading' or 'missing'."""
    return "ready" if ready(lang) else "downloading" if state[lang]["doing"] else "missing"


def ensure(lang: str = DEFAULT, start: bool = True) -> None:
    """Make sure the model is on this computer, downloading it if it is not. One download per language at a time:
    anything else that needs it meanwhile waits for it here. With start=False nothing new is downloaded: this only
    waits for a download that is already running, and says so if the model is not there."""
    if ready(lang):
        return
    if not start:
        while state[lang]["doing"] and not ready(lang):
            time.sleep(1)
        if ready(lang):
            return
        raise RuntimeError(f"{label(lang)} needs its speech model, which isn't on this computer yet. "
                           "Add it in Settings → Speech models (an admin can do this), then try again.")
    with _locks[lang]:
        if not ready(lang):
            _fetch(lang)


def _save(lang: str, dest) -> str:
    """Save the archive to dest, giving up if the connection goes quiet for WAIT seconds. Returns its SHA-256."""
    st, digest = state[lang], hashlib.sha256()
    with urllib.request.urlopen(MODELS[lang][2], timeout=WAIT) as r, open(dest, "wb") as f:
        total, got = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if total > 0:
                st["percent"] = min(99, got * 100 // total)
    return digest.hexdigest()


def _fetch(lang: str):
    name, size, want = MODELS[lang][1], MODELS[lang][3], MODELS[lang][5]
    st = state[lang]
    st.update(doing=True, percent=0, error="")
    print(f"\n  Downloading the {label(lang)} speech model (one time, about {size} MB)...\n", flush=True)
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp, part = config.MODELS_DIR / f"speech-model-{lang}.tar.bz2", config.MODELS_DIR / f"speech-model-{lang}.unpacking"
    try:
        got_sha = _save(lang, tmp)
        if want and got_sha != want:
            raise WrongFile(f"its fingerprint (SHA-256) is {got_sha}, but PeakCut expects {want}")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir()
        with tarfile.open(tmp, "r:bz2") as t:
            for m in t.getmembers():  # only plain files and folders that stay inside the unpacking folder
                if (m.isfile() or m.isdir()) and not m.name.startswith(("/", "..")) and ".." not in m.name.split("/"):
                    t.extract(m, part)
        got = part / name
        if not got.is_dir():
            raise OSError("the download did not contain the model")
        shutil.rmtree(folder(lang), ignore_errors=True)        # a half-finished one from before
        got.rename(folder(lang))
        st["percent"] = 100
        print(f"  {label(lang)} speech model ready.\n", flush=True)
    except WrongFile as e:
        st["error"] = f"The downloaded file is not the expected one: {e}."
        raise RuntimeError(f"PeakCut downloaded the {label(lang)} speech model, but the file is not the one it expects, "
                           "so it was not used: it may have been changed or damaged on the way. Nothing was unpacked. "
                           "Try again later. If a different download address was set (a mirror), it must hold the very "
                           f"same file. Details: {e}.") from e
    except Exception as e:  # noqa: BLE001
        st["error"] = safe.friendly(e, 200)
        raise RuntimeError(f"PeakCut couldn't download its {label(lang)} speech model (about {size} MB, needed once). "
                           "Check the internet connection and free disk space, then try again, or run "
                           f"python -m studio.fetch_model {lang} to see what goes wrong. What it reported: {st['error']}") from e
    finally:
        st["doing"] = False
        tmp.unlink(missing_ok=True)
        shutil.rmtree(part, ignore_errors=True)


def start(lang: str) -> None:
    """Begin downloading in the background (from the pages); progress is in state[lang]."""
    if ready(lang) or state[lang]["doing"]:
        return
    state[lang].update(doing=True, percent=0, error="")    # shown as downloading straight away

    def run():
        try:
            with _locks[lang]:
                if not ready(lang):
                    _fetch(lang)
        except Exception:  # noqa: BLE001 - the reason is kept in state[lang]["error"]
            pass
        finally:
            state[lang]["doing"] = False
    threading.Thread(target=run, daemon=True, name=f"fetch-{lang}").start()


def remove(lang: str) -> None:
    """Delete a downloaded model to free the space. English stays: it is what PeakCut uses by default."""
    from . import transcribe
    if lang == DEFAULT:
        raise ValueError("The English speech model is always kept.")
    with _locks[lang]:
        if state[lang]["doing"]:
            raise RuntimeError(f"The {label(lang)} speech model is still downloading.")
        if not transcribe.unload(lang):
            raise RuntimeError(f"The {label(lang)} speech model is being used right now. Try again when the videos "
                               "in that language are done.")
        try:
            shutil.rmtree(folder(lang))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise RuntimeError(f"Couldn't delete the {label(lang)} speech model: {e}") from e
        state[lang].update(percent=0, error="")


def main():
    lang = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if lang not in MODELS:
        raise SystemExit(f"Unknown language {lang!r}. Known: {', '.join(MODELS)}")
    if ready(lang):
        print(f"{label(lang)} speech model already present.")
        return
    ensure(lang)


if __name__ == "__main__":
    main()
