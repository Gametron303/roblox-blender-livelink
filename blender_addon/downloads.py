"""
Roblox asset downloads in separate processes (fetch_asset.py), polled from the add-on's timer.
Keeps Blender free of threads: nothing here blocks, finished downloads are picked up by poll().
"""

import collections
import os
import subprocess
import sys

_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fetch_asset.py")
MAX_PARALLEL = 4

# (kind, asset_id) -> (process, out_path, on_done)
_running = {}
# (kind, asset_id, out_path, on_done) waiting for a free slot
_waiting = collections.deque()


def _spawn(kind, asset_id, out_path, on_done):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window on Windows
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", _SCRIPT, kind, asset_id, out_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError as e:
        print(f"[Roblox Live Link] Can't start download of {kind} {asset_id}: {e}")
        on_done(False)
        return
    _running[(kind, asset_id)] = (proc, out_path, on_done)


def _fill_slots():
    while _waiting and len(_running) < MAX_PARALLEL:
        _spawn(*_waiting.popleft())


def start(kind, asset_id, out_path, on_done):
    """Queues a download; on_done(success: bool) is called from poll() on Blender's main thread."""
    key = (kind, asset_id)
    if key in _running or any((k, a) == key for k, a, _, _ in _waiting):
        return
    _waiting.append((kind, asset_id, out_path, on_done))
    _fill_slots()


def poll():
    """Collects finished downloads and starts waiting ones. Call regularly from a timer."""
    for key, (proc, out_path, on_done) in list(_running.items()):
        code = proc.poll()
        if code is None:
            continue
        del _running[key]
        try:
            on_done(code == 0 and os.path.exists(out_path))
        except Exception as e:
            print(f"[Roblox Live Link] Processing download {key[0]} {key[1]} failed: {e}")
    _fill_slots()


def active_ids(kind):
    """Asset ids of this kind that are downloading or waiting."""
    ids = {a for k, a in _running if k == kind}
    ids.update(a for k, a, _, _ in _waiting if k == kind)
    return ids


def busy():
    return bool(_running or _waiting)


def cancel_all():
    _waiting.clear()
    for proc, _, _ in _running.values():
        try:
            proc.kill()
        except OSError:
            pass
    _running.clear()
