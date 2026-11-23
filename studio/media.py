"""Getting video in: link download, probing, audio extraction, loudness."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- when YouTube refuses this server
# YouTube sometimes refuses a server's downloads altogether ("sign in to confirm you're not a bot"). It is a
# refusal of the server, not of one video, so once it happens YouTube links are turned away at once for a few
# minutes, with a clear message, instead of each one waiting to fail. Then the next link simply tries again.
YT_PAUSE = 600
_yt_refused_until = 0.0
YT_BLOCKED = ("YouTube isn't letting PeakCut download videos right now. Upload the video file instead, or use a "
              "Twitch or Kick link. An admin can fix this by adding YouTube cookies under Settings, Connected apps. "
              "YouTube links will be tried again in a few minutes.")
_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)", re.I)


class YouTubeBlocked(RuntimeError):
    pass


class MediaError(RuntimeError):
    def __init__(self, message: str, raw_error: str = "", code: int = 422):
        super().__init__(message)
        self.message = message
        self.raw_error = raw_error
        self.code = code


def friendly_error(err: Exception, url: str = "") -> str:
    """Map yt-dlp / network exceptions to clear, actionable user messages."""
    text = str(err or "").strip()

    # 1. Missing JS Runtime
    if re.search(r"No supported JavaScript runtime|JS runtime|EJS|phantomjs", text, re.I):
        return ("YouTube requires a JavaScript runtime (Node.js or Deno) to solve video challenges. "
                "Please install Node.js or Deno on the server, or click 'Update yt-dlp' under Settings → Connected apps.")

    # 2. Bot detection / Sign-in required
    if re.search(r"not a bot|confirm you.?re not|Sign in to confirm(?! your age)|LOGIN_REQUIRED|captcha|automated queries", text, re.I):
        return ("YouTube requires sign-in verification (bot check). "
                "Please upload fresh YouTube cookies under Settings → Connected apps (export from a private browser window), "
                "or choose a browser cookies fallback in Settings.")

    # 3. Private Video
    if re.search(r"This video is private|Private video", text, re.I):
        return "This video is private. Only authorized accounts can view it."

    # 4. Members only / Paid
    if re.search(r"members-only|Join this channel to get access|requires purchase|payment", text, re.I):
        return "This video is members-only or requires a subscription/purchase."

    # 5. Age-restricted
    if re.search(r"Sign in to confirm your age|age-restricted|confirm your age", text, re.I):
        return "This video is age-restricted and requires YouTube cookies from an account over 18."

    # 6. Geo-blocked
    if re.search(r"not available in your country|uploader has not made this video available|geoblocked", text, re.I):
        return "This video is not available in the server's region (geo-blocked by YouTube)."

    # 7. Video removed / unavailable / 404
    if re.search(r"Video unavailable|has been removed|does not exist|HTTP Error 404", text, re.I):
        return "This video is unavailable or has been removed from YouTube."

    # 8. Signature / Format / Outdated yt-dlp
    if re.search(r"Requested format is not available|nsig|Signature extraction failed|player response", text, re.I):
        return ("YouTube updated its player signature format. "
                "Please click 'Update yt-dlp' under Settings → Connected apps to fetch the latest extractor.")

    # 9. Network / DNS error
    if re.search(r"getaddrinfo failed|Connection refused|Network is unreachable|timed out|Timeout", text, re.I):
        return "Could not connect to YouTube. Please check the server's network connection and DNS."

    # 10. Browser cookie lock / permission error
    if re.search(r"Could not copy Chrome cookie database|Permission denied.*Cookies", text, re.I):
        return ("Could not read browser cookies because the browser is currently running. "
                "Please close your browser or upload a cookies.txt file under Settings → Connected apps.")

    # 11. Clean fallback
    clean = re.sub(r"^(?:ERROR:\s*)?(?:\[\w+\]\s*)?[A-Za-z0-9_-]{6,16}:\s*", "", text)
    clean = re.sub(r"^(?:ERROR:\s*)", "", clean).strip()
    if clean and len(clean) < 180:
        return f"Couldn't open that video: {clean}"
    return "Couldn't open that video. Check that the link is public and complete."



def is_youtube(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be"))


def youtube_paused() -> bool:
    return time.time() < _yt_refused_until


def check_youtube(url: str):
    """Raise YouTubeBlocked straight away if YouTube is refusing this server right now."""
    if is_youtube(url) and youtube_paused():
        raise YouTubeBlocked(YT_BLOCKED)


def note_refusal(url: str, err) -> bool:
    """If this error is YouTube refusing the server, pause YouTube links for a while. True if it was."""
    global _yt_refused_until
    if is_youtube(url) and _BOT_CHECK.search(str(err)):
        _yt_refused_until = time.time() + YT_PAUSE
        return True
    return False


@contextlib.contextmanager
def _youtube_guard(url):
    check_youtube(url)
    try:
        yield
    except YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if note_refusal(url, e):
            raise YouTubeBlocked(YT_BLOCKED) from None
        raise


def site_allowed(url: str) -> bool:
    """Links are fetched by the server, so only known video sites are accepted (never arbitrary addresses)."""
    from urllib.parse import urlparse
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in config.ALLOWED_SITES)


def run(cmd, cwd=None, timeout=3600):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(str(cmd[0])).name} took too long and was stopped.")
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed: {p.stderr[-1500:]}")
    return p.stdout


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def cookie_opts(with_browser: bool = True) -> dict:
    """The YouTube cookies an admin saved (Settings, Connected apps), or browser cookies fallback."""
    if config.YT_COOKIES.is_file():
        return {"cookiefile": str(config.YT_COOKIES)}
    if with_browser:
        browser = config.get_yt_browser()
        if browser and browser in ("chrome", "firefox", "edge", "brave", "opera", "vivaldi", "safari"):
            return {"cookiesfrombrowser": (browser, None, None, None)}
    return {}


def find_js_runtimes() -> dict:
    """Build dict of runtime: {path: ...} for yt-dlp YoutubeDL(js_runtimes=...)."""
    runtimes = {}

    # 1. Deno: check PATH, ~/.deno/bin, %USERPROFILE%\.deno\bin
    deno_path = shutil.which("deno")
    if not deno_path:
        home = Path.home()
        for p in (home / ".deno" / "bin" / "deno", home / ".deno" / "bin" / "deno.exe"):
            if p.is_file():
                deno_path = str(p)
                break
    runtimes["deno"] = {"path": deno_path} if deno_path else {}

    # 2. Node: check PATH, common install locations
    node_path = shutil.which("node")
    if not node_path:
        for p in [
            Path(r"C:\Program Files\nodejs\node.exe"),
            Path(r"C:\Program Files (x86)\nodejs\node.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "node" / "node.exe",
            Path(os.environ.get("APPDATA", "")) / "npm" / "node.exe",
            Path("/usr/bin/node"),
            Path("/usr/local/bin/node"),
        ]:
            if p.is_file():
                node_path = str(p)
                break
    runtimes["node"] = {"path": node_path} if node_path else {}

    return runtimes


def find_js_runtimes_info() -> dict:
    """Detailed JS runtime information for diagnostics."""
    info_dict = {
        "deno": {"available": False, "version": "", "path": ""},
        "node": {"available": False, "version": "", "path": ""},
        "active": None,
    }

    # Check Deno
    home = Path.home()
    deno_candidates = [shutil.which("deno"), str(home / ".deno" / "bin" / "deno.exe"), str(home / ".deno" / "bin" / "deno")]
    for cand in deno_candidates:
        if cand and Path(cand).is_file():
            try:
                res = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
                if res.returncode == 0:
                    v = res.stdout.splitlines()[0] if res.stdout else "deno"
                    info_dict["deno"] = {"available": True, "version": v, "path": cand}
                    break
            except Exception:
                pass

    # Check Node
    node_candidates = [
        shutil.which("node"),
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "node" / "node.exe"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "node.exe"),
        "/usr/bin/node",
        "/usr/local/bin/node",
    ]
    for cand in node_candidates:
        if cand and Path(cand).is_file():
            try:
                res = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
                if res.returncode == 0:
                    v = res.stdout.strip()
                    info_dict["node"] = {"available": True, "version": v, "path": cand}
                    break
            except Exception:
                pass

    if info_dict["deno"]["available"]:
        info_dict["active"] = "deno"
    elif info_dict["node"]["available"]:
        info_dict["active"] = "node"

    return info_dict


def cookies_diagnostic() -> dict:
    """Diagnostic info about current YouTube cookie configuration."""
    f = config.YT_COOKIES
    browser = config.get_yt_browser()
    if f.is_file():
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
            has_yt = any("youtube.com" in l for l in lines)
            return {
                "status": "loaded" if has_yt else "invalid",
                "label": f"Loaded ({len(lines)} cookies)" if has_yt else "Invalid cookie format",
                "count": len(lines),
                "browser": None,
                "has_youtube": has_yt,
                "updated": int(f.stat().st_mtime),
            }
        except Exception:
            return {"status": "error", "label": "Error reading cookie file", "count": 0, "browser": None}
    elif browser:
        return {
            "status": "browser",
            "label": f"Browser fallback: {browser.title()}",
            "count": 0,
            "browser": browser,
        }
    return {
        "status": "not_set",
        "label": "Not configured",
        "count": 0,
        "browser": None,
    }


def _ydl(opts, with_cookies=True):
    import yt_dlp
    c_opts = cookie_opts() if with_cookies else {}
    runtimes = find_js_runtimes()
    base_extractor_args = {
        "youtube": {
            "player_client": ["default", "android", "web_safari", "tv_embedded", "android_vr"]
        }
    }
    merged_extractor_args = {**base_extractor_args, **opts.pop("extractor_args", {})}

    final_opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _Quiet(),
        "extract_flat": "in_playlist",
        "playlistend": 1,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "extractor_args": merged_extractor_args,
        **c_opts,
        **opts,
    }
    if runtimes:
        final_opts["js_runtimes"] = runtimes
    return yt_dlp.YoutubeDL(final_opts)


def test_youtube_link(url: str) -> dict:
    """Test format extraction on a link without downloading video data."""
    with _youtube_guard(url):
        i = None
        has_cookies = bool(cookie_opts())
        used_cookies = has_cookies
        try:
            with _ydl({"quiet": True}, with_cookies=True) as ydl:
                i = ydl.extract_info(url, download=False)
        except Exception as e:
            if has_cookies and ("Requested format is not available" in str(e) or "cookie" in str(e).lower()):
                with _ydl({"quiet": True}, with_cookies=False) as ydl:
                    i = ydl.extract_info(url, download=False)
                used_cookies = False
            else:
                raise

        formats = i.get("formats") or []
        video_formats = [f for f in formats if f.get("vcodec") and f.get("vcodec") != "none"]
        audio_formats = [f for f in formats if f.get("acodec") and f.get("acodec") != "none"]
        return {
            "title": i.get("title") or "Unknown Title",
            "duration": float(i.get("duration") or 0),
            "uploader": i.get("uploader") or i.get("channel") or "",
            "is_live": bool(i.get("is_live")),
            "thumbnail": i.get("thumbnail") or "",
            "total_formats": len(formats),
            "video_formats": len(video_formats),
            "audio_formats": len(audio_formats),
            "client_used": i.get("extractor_key") or "YouTube",
            "cookies_used": used_cookies,
        }


def _video_format(height):
    return f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"


def _biggest(job_dir: Path, stem: str) -> Path:
    files = sorted((p for p in job_dir.glob(stem + ".*") if p.suffix not in (".part", ".ytdl")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return files[0]


def info(url: str) -> dict:
    """Title, channel and length, without downloading anything."""
    with _youtube_guard(url):
        i = None
        last_err = None
        has_cookies = bool(cookie_opts())

        # Attempt 1: Standard extraction with configured cookies
        try:
            with _ydl({}, with_cookies=True) as ydl:
                i = ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e

        # Attempt 2: If cookies caused format or auth issue, retry without cookies
        if i is None and has_cookies and ("Requested format is not available" in str(last_err) or "cookie" in str(last_err).lower()):
            try:
                with _ydl({}, with_cookies=False) as ydl:
                    i = ydl.extract_info(url, download=False)
            except Exception as e2:
                if _BOT_CHECK.search(str(e2)):
                    last_err = e2

        if i is None and last_err:
            raise last_err

    return {"title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or "",
            "duration": float(i.get("duration") or 0), "is_live": bool(i.get("is_live")),
            "thumbnail": i.get("thumbnail") or "", "kind": i.get("_type") or "video"}




def youtube_words(url: str):
    """YouTube's own transcript of a video, word by word: ([{'w', 's', 'e'}, ...], language), or None if the video
    has none. Only the transcript in the language that was spoken is used, never a translation."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        auto = i.get("automatic_captions") or {}
        lang = str(i.get("language") or "").strip().lower()
        base = lang.split("-")[0] if lang else ""
        candidates = [f"{lang}-orig", lang, f"{base}-orig", base]
        key = next((c for c in candidates if c and c in auto), None)
        if not key:
            key = next((k for k in auto if k.endswith("-orig")), None)
        track = next((f for f in auto.get(key) or [] if f.get("ext") == "json3" and f.get("url")), None) if key else None
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    return _caption_words(json.loads(raw)), key.replace("-orig", "")


def youtube_english_words(url: str):
    """YouTube's English captions of a video spoken in another language: ones a person wrote if there are any,
    otherwise YouTube's own translation of its transcript. Word list like youtube_words, or None."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        tracks = []
        for group in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
            for k in ("en", "en-US", "en-GB"):
                tracks.append(next((f for f in group.get(k) or [] if f.get("ext") == "json3" and f.get("url")), None))
        track = next((t for t in tracks if t), None)
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    words = _caption_words(json.loads(raw))
    return words or None


def _caption_words(data: dict) -> list:
    out = []
    for ev in data.get("events") or []:
        segs = [x for x in ev.get("segs") or [] if (x.get("utf8") or "").strip()]
        if not segs:
            continue
        t0, dur = float(ev.get("tStartMs") or 0), float(ev.get("dDurationMs") or 0)
        timed = len(segs) > 1 or "tOffsetMs" in segs[0]
        for x in segs:
            parts = x["utf8"].split()
            for n, w in enumerate(parts):
                if re.fullmatch(r"[\[(].*[\])]", w) or w in ("[", "]", "__"):
                    continue                      # [Music], [Applause], and words YouTube blanked out
                # a line with no word timings: its words are spread evenly over the time the line was on screen
                at = t0 + float(x.get("tOffsetMs") or 0) if timed else t0 + dur * n / max(1, len(parts))
                out.append({"w": w, "s": round(at / 1000, 2)})
    out.sort(key=lambda w: w["s"])
    for n, w in enumerate(out):
        nxt = out[n + 1]["s"] if n + 1 < len(out) else w["s"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, w["s"] + 0.6)), 2)
    return out


_PREVIEWS: dict = {}


def preview(url: str) -> dict:
    """What the user will get from a link, checked before any work starts. The thumbnail comes back inline
    (the page may only load images from this site). Cached, so retyping a link costs nothing."""
    url = url.strip()
    if url in _PREVIEWS:
        return _PREVIEWS[url]
    i = info(url)
    if i["kind"] != "video" or not i["title"]:
        raise ValueError("That link is a channel or playlist, not a single video.")
    thumb = ""
    t = i.get("thumbnail") or ""
    if t.startswith("https://"):
        import base64
        import urllib.request
        try:
            req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(600_000 + 1)
            if ctype in ("image/jpeg", "image/png", "image/webp") and len(data) <= 600_000:
                thumb = f"data:{ctype};base64," + base64.b64encode(data).decode()
        except Exception:
            thumb = ""
    out = {"title": i["title"][:200], "uploader": i["uploader"][:120], "duration": i["duration"],
           "is_live": i["is_live"], "thumb": thumb}
    if len(_PREVIEWS) > 300:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    _PREVIEWS[url] = out
    return out


def save_thumb(job_dir: Path, url: str = "", video: Path = None) -> bool:
    """A small picture for the video list: the link's own thumbnail, or a frame from an uploaded file."""
    import base64
    out = Path(job_dir) / "clips" / "thumb.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if url:
            p = _PREVIEWS.get(url.strip()) or preview(url)
            if not p.get("thumb"):
                return False
            out.write_bytes(base64.b64decode(p["thumb"].split(",", 1)[1]))
            return True
        if video:
            d = probe(video).get("duration") or 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(3.0, d / 3):.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)])
            return out.is_file()
    except Exception:
        return False
    return False


def download_audio(url: str, job_dir: Path, on_progress=None) -> Path:
    """Audio only: a few seconds even for long videos, and all the AI needs to pick clips."""
    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if on_progress and d.get("status") == "downloading" and total:
            on_progress(d.get("downloaded_bytes", 0) / total)

    opts = {"format": "ba[ext=m4a]/ba/worst", "outtmpl": str(job_dir / "audio_src.%(ext)s"),
            "progress_hooks": [hook]}
    has_cookies = bool(cookie_opts())
    with _youtube_guard(url):
        try:
            with _ydl(opts, with_cookies=True) as ydl:
                ydl.download([url])
        except Exception as e:
            if has_cookies and ("Requested format is not available" in str(e) or "cookie" in str(e).lower()):
                with _ydl(opts, with_cookies=False) as ydl:
                    ydl.download([url])
            else:
                raise
    return _biggest(job_dir, "audio_src")


def download(url: str, job_dir: Path, height=720) -> Path:
    """The whole video. Used for shorter videos, where this is quicker than fetching pieces."""
    opts = {"format": _video_format(height), "merge_output_format": "mp4",
            "outtmpl": str(job_dir / "source.%(ext)s")}
    has_cookies = bool(cookie_opts())
    with _youtube_guard(url):
        try:
            with _ydl(opts, with_cookies=True) as ydl:
                ydl.download([url])
        except Exception as e:
            if has_cookies and ("Requested format is not available" in str(e) or "cookie" in str(e).lower()):
                with _ydl(opts, with_cookies=False) as ydl:
                    ydl.download([url])
            else:
                raise
    return _biggest(job_dir, "source")


def download_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720, exact=False) -> Path:
    """Just one stretch of the video. The cut lands on a keyframe, so find_offset() pins the true start; with
    exact=True the cut is made at exactly `start` instead (slower), for when there is nothing to match against."""
    from yt_dlp.utils import download_range_func
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {"format": _video_format(height), "merge_output_format": "mp4", "outtmpl": str(out_dir / (stem + ".%(ext)s")),
            "download_ranges": download_range_func(None, [(max(0.0, start), end)])}
    if exact:
        opts["force_keyframes_at_cuts"] = True
    has_cookies = bool(cookie_opts())
    with _youtube_guard(url):
        try:
            with _ydl(opts, with_cookies=True) as ydl:
                ydl.download([url])
        except Exception as e:
            if has_cookies and ("Requested format is not available" in str(e) or "cookie" in str(e).lower()):
                with _ydl(opts, with_cookies=False) as ydl:
                    ydl.download([url])
            else:
                raise
    return _biggest(out_dir, stem)


def find_offset(full: np.ndarray, part: np.ndarray, expected: float, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded piece really begins, by matching the sound itself."""
    n = min(len(part), 30 * 16000)
    a0 = max(0, int((expected - search) * 16000))
    hay = full[a0: int((expected + search) * 16000) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if n < 16000 or len(hay) < n or float(np.abs(needle).mean()) < 20:
        return expected  # too short or silent to match: trust the requested time
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / 16000


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    if not any(s["codec_type"] == "audio" for s in data["streams"]):
        raise RuntimeError("This file has no audio track, so there is nothing to transcribe.")
    w, h = int(v["width"]), int(v["height"])
    if w * h > 3840 * 2160 or w < 16 or h < 16:
        raise RuntimeError("This video's resolution isn't supported (4K is the largest).")
    rot = str((v.get("side_data_list") or [{}])[0].get("rotation", "0"))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    return {"width": w, "height": h, "duration": float(data["format"]["duration"])}


def extract_audio(src: Path, wav: Path):
    # first_pts=0 pads any gap before the audio starts, so second 0 of the wav is second 0 of the file
    # -t: never more audio than the longest video allowed, whatever the file claims about itself
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", str(int(config.MAX_VIDEO_HOURS * 3600) + 60),
         "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(wav)])


def load_audio(wav: Path) -> np.ndarray:
    data, sr = sf.read(str(wav), dtype="int16")
    assert sr == 16000
    return data


LOUDNESS_PIECE = 600       # seconds of audio worked out at a time


def loudness_per_second(audio: np.ndarray) -> np.ndarray:
    """RMS level in dB for each second of audio.

    Worked out ten minutes at a time. All at once, a day-long video needs a table of several gigabytes (one
    float per sample), which most computers cannot find room for; this way it needs about 40 MB, whatever the length."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    out = np.empty(n, dtype=np.float32)
    for a in range(0, n, LOUDNESS_PIECE):
        b = min(n, a + LOUDNESS_PIECE)
        x = audio[a * 16000: b * 16000].reshape(b - a, 16000).astype(np.float32) / 32768.0
        out[a:b] = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1) + 1e-10))
    return out
