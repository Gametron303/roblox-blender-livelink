"""
Where the add-on keeps its files.

Everything downloaded or generated at runtime (Roblox textures, meshes, baked faces) goes to the
extension's user folder: extensions must not write into their own directory.
"""

import os
import shutil

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


def online_access_allowed():
    """Blender's "Allow Online Access" preference: no internet downloads when it is off."""
    return getattr(bpy.app, "online_access", True)
