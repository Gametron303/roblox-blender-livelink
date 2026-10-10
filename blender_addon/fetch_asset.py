"""
Downloads one Roblox asset. Runs as a separate process started by downloads.py, so Blender itself
never blocks on the network and uses no threads.

    python fetch_asset.py texture|mesh <asset_id> <out_path>

Exit code 0 means <out_path> was written. Textures are saved as downloaded (PNG/JPEG bytes);
the add-on post-processes them in Blender.
"""

import gzip
import json
import os
import sys
import time
import urllib.request

ASSET_URL = "https://assetdelivery.roblox.com/v1/asset/?id={}"
THUMB_URL = "https://thumbnails.roblox.com/v1/assets?assetIds={}&size=420x420&format=Png&isCircular=false"


def _get(url, user_agent, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data


def _write(out_path, data):
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out_path)


def fetch_mesh(asset_id, out_path):
    data = _get(ASSET_URL.format(asset_id), "Roblox/WinInet", 8.0)
    # Don't keep error pages / non-mesh payloads
    if b"version " not in data[:40]:
        return False
    _write(out_path, data)
    return True


def fetch_texture(asset_id, out_path):
    # 1. Full-resolution image from the asset delivery API (many assets now need sign-in: HTTP 401)
    try:
        data = _get(ASSET_URL.format(asset_id), "Roblox/WinInet", 6.0)
        if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8"):
            _write(out_path, data)
            return True
    except Exception:
        pass
    # 2. Public thumbnail (up to 420x420). Roblox renders thumbnails lazily: first answer is often "Pending"
    for attempt in range(4):
        info = json.loads(_get(THUMB_URL.format(asset_id), "Mozilla/5.0", 6.0).decode("utf-8"))
        entries = info.get("data") or [{}]
        url = entries[0].get("imageUrl")
        if url:
            _write(out_path, _get(url, "Mozilla/5.0", 8.0))
            return True
        if entries[0].get("state") != "Pending":
            return False
        time.sleep(1.5 * (attempt + 1))
    return False


def main(argv):
    if len(argv) != 4 or argv[1] not in ("texture", "mesh") or not argv[2].isdigit():
        print("usage: fetch_asset.py texture|mesh <asset_id> <out_path>", file=sys.stderr)
        return 2
    kind, asset_id, out_path = argv[1], argv[2], argv[3]
    try:
        ok = fetch_mesh(asset_id, out_path) if kind == "mesh" else fetch_texture(asset_id, out_path)
    except Exception as e:
        print(f"Download of {kind} {asset_id} failed: {e}", file=sys.stderr)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
