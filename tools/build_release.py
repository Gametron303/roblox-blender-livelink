"""
Builds the release into release/<version>/ with Blender's official extension builder:

  roblox_blender_livelink-<version>-windows_x64.zip   Blender extension, one zip per platform
  roblox_blender_livelink-<version>-macos_*.zip
  README.md (RU), README.en.md (EN, Blender Extensions page), CHANGELOG.md

Materials are procedural, so no textures are packed. The local download cache (material_cache/)
holds assets of the games you synced and is never shipped.

  python tools/build_release.py [--blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"]
"""

import argparse
import ast
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_SRC = os.path.join(ROOT, "blender_addon")
PLUGIN_SRC = os.path.join(ROOT, "roblox_plugin", "RobloxLiveLink.server.luau")


def find_blender(explicit):
    candidates = [explicit, os.getenv("BLENDER")]
    candidates += sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"), reverse=True)
    candidates += [r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe", shutil.which("blender")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    sys.exit("Blender not found: pass --blender or set BLENDER")


def read_versions():
    manifest = open(os.path.join(ADDON_SRC, "blender_manifest.toml"), encoding="utf-8").read()
    init = open(os.path.join(ADDON_SRC, "__init__.py"), encoding="utf-8").read()
    plugin = open(PLUGIN_SRC, encoding="utf-8").read()
    versions = {
        "blender_manifest.toml": re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1),
        "__init__.py VERSION_LABEL": re.search(r'VERSION_LABEL\s*=\s*"([^"]+)"', init).group(1),
        "plugin PLUGIN_VERSION": re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', plugin).group(1),
    }
    if len(set(versions.values())) != 1:
        sys.exit(f"Version mismatch: {versions}")
    return next(iter(versions.values()))


def check_sources():
    for f in glob.glob(os.path.join(ADDON_SRC, "*.py")):
        ast.parse(open(f, encoding="utf-8").read(), f)
    if open(PLUGIN_SRC, "rb").read().startswith(b"\xef\xbb\xbf"):
        sys.exit("Plugin has a UTF-8 BOM: Studio fails to load it")
    for f in glob.glob(os.path.join(ADDON_SRC, "*.py")) + [PLUGIN_SRC]:
        if re.search(r"[A-Za-z]:\\Users\\", open(f, encoding="utf-8").read()):
            sys.exit(f"Personal path found in {f}")


def ensure_wheels():
    """Downloads the Pillow wheels listed in blender_manifest.toml from PyPI (they are not kept in git)."""
    manifest = open(os.path.join(ADDON_SRC, "blender_manifest.toml"), encoding="utf-8").read()
    wheel_dir = os.path.join(ADDON_SRC, "wheels")
    os.makedirs(wheel_dir, exist_ok=True)
    for rel in re.findall(r'"\./wheels/([^"]+\.whl)"', manifest):
        if os.path.isfile(os.path.join(wheel_dir, rel)):
            continue
        # pillow-12.2.0-cp313-cp313-win_amd64.whl -> name, version, python tag, platform tag
        name, version, py_tag, _abi, plat = rel[:-4].split("-")
        py_version = f"{py_tag[2]}.{py_tag[3:]}"
        print(f"  downloading {rel}")
        subprocess.run([sys.executable, "-m", "pip", "download", f"{name}=={version}", "--only-binary=:all:",
                        "--no-deps", "--python-version", py_version, "--platform", plat, "-d", wheel_dir, "-q"],
                       check=True)
        if not os.path.isfile(os.path.join(wheel_dir, rel)):
            sys.exit(f"pip did not produce {rel}")


def stage(dst):
    for f in glob.glob(os.path.join(ADDON_SRC, "*.py")) + [os.path.join(ADDON_SRC, "blender_manifest.toml")]:
        shutil.copy2(f, dst)
    shutil.copytree(os.path.join(ADDON_SRC, "wheels"), os.path.join(dst, "wheels"))
    # The Studio plugin is not packed: it is installed from the Creator Store or the GitHub release


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender")
    args = ap.parse_args()
    blender = find_blender(args.blender)
    check_sources()
    ensure_wheels()
    version = read_versions()
    out_dir = os.path.join(ROOT, "release", version)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "roblox_blender_livelink")
        os.makedirs(src)
        stage(src)
        for cmd in (["validate", src], ["build", "--source-dir", src, "--output-dir", out_dir, "--split-platforms"]):
            print(">", "blender --command extension", " ".join(cmd))
            subprocess.run([blender, "--factory-startup", "--command", "extension", *cmd], check=True)

    for doc in ("README.md", "README.en.md", "CHANGELOG.md"):
        shutil.copyfile(os.path.join(ROOT, doc), os.path.join(out_dir, doc))
    for f in sorted(os.listdir(out_dir)):
        print(f"  {f}  {os.path.getsize(os.path.join(out_dir, f)) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
