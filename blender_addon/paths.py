"""
Where the add-on keeps its files.

Everything downloaded or generated at runtime (Roblox textures, meshes, baked faces) goes to the
extension's user folder: extensions must not write into their own directory.
"""

import glob
import os
import shutil
import sys

import bpy

ADDON_DIR = os.path.dirname(os.path.realpath(__file__))
# Cache location when running from a source checkout or as a legacy add-on
_FALLBACK_CACHE_DIR = os.path.join(ADDON_DIR, "material_cache")

_cache_root = None


def cache_dir(*sub):
    """Writable cache folder (created on demand)."""
    global _cache_root
    if _cache_root is None:
        try:
            _cache_root = bpy.utils.extension_path_user(__package__, path="cache", create=True)
        except Exception:
            # Running from a source checkout or as a legacy add-on: keep the cache next to the code
            _cache_root = _FALLBACK_CACHE_DIR
    path = os.path.join(_cache_root, *sub)
    os.makedirs(path, exist_ok=True)
    return path


def migrate_legacy_cache():
    """
    Copies downloads from an older 'scripts/addons' install into the extension cache.
    Some Roblox assets can no longer be downloaded without signing in (HTTP 401), so keep what we have.
    """
    legacy = bpy.utils.user_resource('SCRIPTS', path=os.path.join("addons", "roblox_blender_livelink", "material_cache"))
    if not legacy or not os.path.isdir(legacy):
        return 0
    if os.path.realpath(legacy) == os.path.realpath(cache_dir()):
        return 0
    copied = 0
    for sub in ("custom_textures", "meshes"):
        src_dir = os.path.join(legacy, sub)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = cache_dir(sub)
        for name in os.listdir(src_dir):
            src = os.path.join(src_dir, name)
            dst = os.path.join(dst_dir, name)
            if os.path.isfile(src) and not name.endswith(".part") and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
    return copied


_studio_content = None


def studio_content_dir():
    """The newest Roblox Studio install's "content" folder (read-only use), or None if Studio isn't found."""
    global _studio_content
    if _studio_content is not None:
        return _studio_content or None
    candidates = []
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        if base:
            exes = glob.glob(os.path.join(base, "Roblox", "Versions", "*", "RobloxStudioBeta.exe"))
            exes.sort(key=os.path.getmtime, reverse=True)
            candidates = [os.path.join(os.path.dirname(exe), "content") for exe in exes]
    elif sys.platform == "darwin":
        for apps in ("/Applications", os.path.expanduser("~/Applications")):
            candidates.append(os.path.join(apps, "RobloxStudio.app", "Contents", "Resources", "content"))
    _studio_content = next((c for c in candidates if os.path.isdir(c)), "")
    return _studio_content or None


def online_access_allowed():
    """Blender's "Allow Online Access" preference: no internet downloads when it is off."""
    return getattr(bpy.app, "online_access", True)
