"""How busy the server really is, measured every second: processor, memory and disk.

Nothing here counts people. New heavy work (another video, another recording) starts only while the machine has
room, waits in line for a moment when it is nearly full, and is let through the second it frees up again. That is
what keeps a rush from slowing everyone down or crashing the server."""
import os
import shutil
import sys
import threading
import time

from . import config

BUSY_CPU = float(config.env("PEAKCUT_BUSY_CPU", "82"))          # percent: above this, no extra videos are started
MIN_FREE_RAM_GB = float(config.env("PEAKCUT_MIN_FREE_RAM_GB", "1.5"))
MIN_FREE_DISK_GB = float(config.env("PEAKCUT_MIN_FREE_DISK_GB", "3"))

_state = {"cpu": 0.0, "ram_free": 99.0, "disk_free": 99.0, "at": 0.0}
_force = None            # tests only: pretend the server is busy (True) or free (False)
_started = False
_lock = threading.Lock()
_mach = None             # macOS: libSystem and this machine's Mach host port, looked up once


def _mach_numbers(flavor: int, words: int, call: str):
    """macOS: one of the kernel's statistics records, as `words` 32-bit numbers.

    macOS has no /proc, so the numbers come from the Mach kernel instead. It checks the length it is handed and
    refuses anything short, so `words` has to be the record's full width even when only the first few are wanted.
    Raises OSError if the kernel will not answer."""
    global _mach
    if _mach is None:
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        _mach = (ctypes, lib, lib.mach_host_self())
    ctypes, lib, host = _mach
    got = (ctypes.c_uint * words)()
    n = ctypes.c_uint(words)
    if getattr(lib, call)(host, flavor, ctypes.byref(got), ctypes.byref(n)) != 0:    # 0 is KERN_SUCCESS
        raise OSError(f"the Mach kernel refused {call}, flavor {flavor}")
    return got


def _cpu_times():
    """(busy, total) processor time so far, in any unit."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        n = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime          # noqa: E731
        total = n(kernel) + n(user)                                        # kernel time includes idle time
        return total - n(idle), total
    if sys.platform == "darwin":
        # HOST_CPU_LOAD_INFO: four running tick counts, in the order user, system, idle, nice
        c = _mach_numbers(3, 4, "host_statistics")
        total = c[0] + c[1] + c[2] + c[3]
        return total - c[2], total
    with open("/proc/stat", encoding="ascii") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)                                    # everything except idle and waiting on disk


def _ram_free_gb() -> float:
    if os.name == "nt":
        import ctypes

        class M(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")]
        m = M()
        m.dwLength = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.avail / 1e9
    if sys.platform == "darwin":
        # HOST_VM_INFO64 is 38 numbers wide; the 1st is pages free and the 3rd pages inactive. Inactive pages
        # hold cached file data the kernel takes back the moment something needs the room, so free + inactive is
        # what can be handed out without swapping: the same thing Linux means by MemAvailable.
        v = _mach_numbers(4, 38, "host_statistics64")
        return (v[0] + v[2]) * os.sysconf("SC_PAGESIZE") / 1e9
    with open("/proc/meminfo", encoding="ascii") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    return 99.0


def _watch():
    try:
        last = _cpu_times()
    except Exception:  # noqa: BLE001 - a system whose processor we cannot measure (a Mac): leave that out, keep the rest
        last = None
    while True:
        time.sleep(1)
        if last is not None:
            try:
                now = _cpu_times()
                used, total = now[0] - last[0], now[1] - last[1]
                last = now
                if total > 0:
                    # smoothed over a few seconds, so one short spike neither holds people up nor lets too much in
                    _state["cpu"] = 0.55 * _state["cpu"] + 0.45 * (100.0 * used / total)
            except Exception:  # noqa: BLE001
                pass
        try:
            _state["ram_free"] = _ram_free_gb()
        except Exception:  # noqa: BLE001
            pass
        try:
            if time.time() - _state["at"] > 15:
                _state["disk_free"] = shutil.disk_usage(config.DATA_DIR).free / 1e9
                _state["at"] = time.time()
        except Exception:  # noqa: BLE001
            pass


def start():
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_watch, daemon=True, name="load").start()


def busy() -> bool:
    """True while the machine is close to full: nothing extra should start right now."""
    if _force is not None:
        return _force
    start()
    return _state["cpu"] >= BUSY_CPU or _state["ram_free"] < MIN_FREE_RAM_GB


def disk_full() -> bool:
    start()
    return _state["disk_free"] < MIN_FREE_DISK_GB


def state() -> dict:
    start()
    return {"cpu": round(_state["cpu"]), "ram_free_gb": round(_state["ram_free"], 1), "disk_free_gb": round(_state["disk_free"], 1),
            "busy": busy(), "busy_cpu": BUSY_CPU}
