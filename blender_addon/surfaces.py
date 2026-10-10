"""
Roblox surface layers for Blender Live Link: Decals, Textures and SurfaceGuis on part faces.

The Studio plugin sends every layer of a part ("surfaces"). Decals and SurfaceGuis of one face are
baked here into a single RGBA image (with PIL); tiled Textures stay in the shader (they repeat per stud).
"""

import hashlib
import json
import os

from . import paths

# Faces of a unit part in Blender local axes (Roblox X -> X, Roblox Y -> Z, Roblox Z -> -Y).
# "right"/"up" are the directions of the image U/V axes when looking at the face, as Roblox draws decals.
FACES = {
    "Front": {"n": (0, 1, 0), "right": (-1, 0, 0), "up": (0, 0, 1)},
    "Back": {"n": (0, -1, 0), "right": (1, 0, 0), "up": (0, 0, 1)},
    "Top": {"n": (0, 0, 1), "right": (1, 0, 0), "up": (0, 1, 0)},
    "Bottom": {"n": (0, 0, -1), "right": (1, 0, 0), "up": (0, -1, 0)},
    "Left": {"n": (-1, 0, 0), "right": (0, -1, 0), "up": (0, 0, 1)},
    "Right": {"n": (1, 0, 0), "right": (0, 1, 0), "up": (0, 0, 1)},
}

# Blender local axis index -> Roblox Size component (X -> X, Y -> Z, Z -> Y)
_BL_AXIS_TO_RBX = (0, 2, 1)

BAKE_LONG_SIDE = 1024


def face_size_studs(face, size):
    """(width, height) of a face in studs for a part of Roblox size [x, y, z]."""
    f = FACES[face]
    w_axis = next(i for i in range(3) if f["right"][i] != 0)
    h_axis = next(i for i in range(3) if f["up"][i] != 0)
    return float(size[_BL_AXIS_TO_RBX[w_axis]]), float(size[_BL_AXIS_TO_RBX[h_axis]])


def split_layers(surfaces):
    """face -> {"textures": [...], "overlay": [...]} for visible layers only."""
    faces = {}
    if not isinstance(surfaces, list):
        return faces
    for layer in surfaces:
        if not isinstance(layer, dict) or layer.get("face") not in FACES:
            continue
        kind = layer.get("kind")
        if kind in ("decal", "texture") and (not layer.get("img") or float(layer.get("t", 0)) >= 1.0):
            continue
        if kind == "gui" and not layer.get("elements"):
            continue
        slot = faces.setdefault(layer["face"], {"textures": [], "overlay": []})
        if kind == "texture":
            slot["textures"].append(layer)
        elif kind in ("decal", "gui"):
            slot["overlay"].append(layer)
    for slot in faces.values():
        slot["textures"].sort(key=lambda l: l.get("z", 1))
        # Decals by ZIndex, SurfaceGuis are drawn on top of decals
        slot["overlay"].sort(key=lambda l: (1 if l.get("kind") == "gui" else 0, l.get("z", 1)))
    return faces


def signature(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Roblox Studio content folder (bundled fonts and rbxasset:// textures)
# ---------------------------------------------------------------------------

def studio_content_dir():
    return paths.studio_content_dir()


def rbxasset_path(content):
    """'rbxasset://textures/SpawnLocation.png' -> file in the Studio content folder (or None)."""
    if not isinstance(content, str) or not content.lower().startswith("rbxasset://"):
        return None
    root = studio_content_dir()
    if not root:
        return None
    rel = content[len("rbxasset://"):].replace("/", os.sep)
    # Studio keeps rbxasset:// files in "content" and some textures in "PlatformContent/pc"
    for base in (root, os.path.join(os.path.dirname(root), "PlatformContent", "pc")):
        path = os.path.join(base, rel)
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_font_cache = {}
_glyph_support_cache = {}


def _windows_font(name):
    windir = os.getenv("WINDIR")
    if not windir:
        return None  # not Windows: Studio's own Arimo fonts are used instead
    path = os.path.join(windir, "Fonts", name)
    return path if os.path.exists(path) else None


def _family_faces(family):
    """Font files of a Roblox family JSON, best weight/style match first is decided by the caller."""
    if not isinstance(family, str) or not family.startswith("rbxasset://fonts/families/"):
        return []
    root = studio_content_dir()
    if not root:
        return []
    name = os.path.basename(family)
    path = os.path.join(root, "fonts", "families", name)
    if not os.path.exists(path) and "gotham" in name.lower():
        # Gotham was removed from Studio; Roblox renders it with Montserrat
        path = os.path.join(root, "fonts", "families", "Montserrat.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    faces = []
    for face in data.get("faces", []):
        file_path = rbxasset_path(face.get("assetId", ""))
        if file_path:
            faces.append((int(face.get("weight", 400)), str(face.get("style", "normal")).lower(), file_path))
    return faces


def _supports_text(path, text):
    """True if the font has glyphs for every non-ASCII character of the text (e.g. Cyrillic)."""
    special = sorted({ch for ch in text if ord(ch) > 127 and not ch.isspace()})
    if not special:
        return True
    key = (path, "".join(special[:64]))
    if key in _glyph_support_cache:
        return _glyph_support_cache[key]
    ok = True
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(path, 32)

        def glyph(ch):
            mask = font.getmask(ch)
            return mask.size, bytes(mask)

        # A missing character is drawn with the same ".notdef" box as an unassigned code point
        notdef = glyph("\U0010FFFF")
        for ch in special[:64]:
            if glyph(ch) == notdef:
                ok = False
                break
    except Exception:
        ok = False
    _glyph_support_cache[key] = ok
    return ok


def resolve_font(font_info, text=""):
    font_info = font_info if isinstance(font_info, dict) else {}
    weight = int(font_info.get("w") or 400)
    italic = str(font_info.get("s", "Normal")).lower() == "italic"
    cache_key = (font_info.get("f"), weight, italic, text if any(ord(c) > 127 for c in text) else "")
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    faces = _family_faces(font_info.get("f"))
    style = "italic" if italic else "normal"
    faces.sort(key=lambda f: (f[1] != style, abs(f[0] - weight)))
    candidates = [f[2] for f in faces]
    bold = weight >= 600
    for name in (("arialbi.ttf" if italic else "arialbd.ttf") if bold else ("ariali.ttf" if italic else "arial.ttf"),
                 "arialbd.ttf" if bold else "arial.ttf", "segoeui.ttf"):
        p = _windows_font(name)
        if p:
            candidates.append(p)
    arimo = rbxasset_path("rbxasset://fonts/Arimo-Bold.ttf" if bold else "rbxasset://fonts/Arimo-Regular.ttf")
    if arimo:
        candidates.append(arimo)

    chosen = next((c for c in candidates if _supports_text(c, text)), candidates[0] if candidates else None)
    _font_cache[cache_key] = chosen
    return chosen


def _load_font(path, size):
    from PIL import ImageFont
    size = max(1, int(round(size)))
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _rgba(color, transparency=0.0):
    c = color if isinstance(color, (list, tuple)) and len(color) >= 3 else (1, 1, 1)
    a = max(0.0, min(1.0, 1.0 - float(transparency or 0)))
    return (int(round(c[0] * 255)), int(round(c[1] * 255)), int(round(c[2] * 255)), int(round(a * 255)))


def _tint(img, color, transparency):
    """Multiply RGB by a Color3 and scale alpha by (1 - transparency)."""
    from PIL import Image, ImageChops
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    tint = _rgba(color)
    if tint[:3] != (255, 255, 255):
        rgb = ImageChops.multiply(Image.merge("RGB", (r, g, b)), Image.new("RGB", img.size, tint[:3]))
        r, g, b = rgb.split()
    alpha = max(0.0, min(1.0, 1.0 - float(transparency or 0)))
    if alpha < 1.0:
        a = a.point(lambda v: int(v * alpha))
    return Image.merge("RGBA", (r, g, b, a))


def _image_scale_factor(path):
    """Downloaded textures may be upscaled; the sidecar keeps the factor so ImageRect pixels still match."""
    try:
        with open(path + ".json", "r", encoding="utf-8") as f:
            return float(json.load(f).get("scale", 1.0)) or 1.0
    except Exception:
        return 1.0


def _layer(size):
    from PIL import Image
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _draw_box(canvas, el, k):
    from PIL import ImageDraw
    x0, y0 = el["x"] * k, el["y"] * k
    x1, y1 = x0 + el["w"] * k, y0 + el["h"] * k
    radius = max(0.0, float(el.get("r") or 0) * k)
    if float(el.get("bgT", 1)) < 1:
        layer = _layer(canvas.size)
        ImageDraw.Draw(layer).rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=_rgba(el.get("bg"), el.get("bgT")))
        canvas.alpha_composite(layer)
    border = el.get("border")
    if isinstance(border, dict):
        th = max(1.0, float(border.get("th", 1)) * k)
        # UIStroke on a frame is drawn outside the frame edge
        layer = _layer(canvas.size)
        ImageDraw.Draw(layer).rounded_rectangle(
            (x0 - th / 2, y0 - th / 2, x1 + th / 2, y1 + th / 2), radius=radius + th / 2 if radius else 0,
            outline=_rgba(border.get("c"), border.get("t")), width=int(round(th)))
        canvas.alpha_composite(layer)


def _draw_image(canvas, el, k, fetch):
    from PIL import Image
    path = fetch(el.get("img"))
    if not path:
        return False
    try:
        src = Image.open(path).convert("RGBA")
    except Exception:
        return True  # broken file: nothing to draw, but not "still downloading"
    rect = el.get("rect")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        s = _image_scale_factor(path)
        box = [int(round(v * s)) for v in (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])]
        src = src.crop(box)
    src = _tint(src, el.get("ic"), el.get("it"))
    w, h = max(1, int(round(el["w"] * k))), max(1, int(round(el["h"] * k)))
    x, y = int(round(el["x"] * k)), int(round(el["y"] * k))
    scale_type = el.get("st", "Stretch")
    layer = _layer(canvas.size)
    if scale_type in ("Fit", "Crop") and src.width and src.height:
        ratio = (min if scale_type == "Fit" else max)(w / src.width, h / src.height)
        fw, fh = max(1, int(round(src.width * ratio))), max(1, int(round(src.height * ratio)))
        fitted = src.resize((fw, fh), Image.LANCZOS)
        if scale_type == "Crop":
            left, top = (fw - w) // 2, (fh - h) // 2
            fitted = fitted.crop((left, top, left + w, top + h))
            layer.alpha_composite(fitted, (x, y))
        else:
            layer.alpha_composite(fitted, (x + (w - fw) // 2, y + (h - fh) // 2))
    elif scale_type == "Tile":
        tile = src.resize((max(1, min(w, src.width)), max(1, min(h, src.height))), Image.LANCZOS)
        for ty in range(y, y + h, tile.height):
            for tx in range(x, x + w, tile.width):
                layer.alpha_composite(tile.crop((0, 0, min(tile.width, x + w - tx), min(tile.height, y + h - ty))), (tx, ty))
    else:  # Stretch, Slice
        layer.alpha_composite(src.resize((w, h), Image.LANCZOS), (x, y))
    canvas.alpha_composite(layer)
    return True


def _wrap_line(text, font, max_w):
    words = text.split(" ")
    lines, cur = [], ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if not cur or font.getlength(trial) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def _layout_text(text, font_path, size, box_w, wrap, line_height):
    font = _load_font(font_path, size)
    lines = []
    for raw in text.split("\n"):
        lines.extend(_wrap_line(raw, font, box_w) if wrap else [raw])
    width = max((font.getlength(l) for l in lines), default=0)
    return font, lines, width, len(lines) * size * line_height


def _draw_text(canvas, el, k):
    from PIL import ImageDraw
    text = str(el.get("text", ""))
    if not text.strip():
        return
    box_x, box_y = el["x"] * k, el["y"] * k
    box_w, box_h = el["w"] * k, el["h"] * k
    line_h = float(el.get("lh") or 1.0)
    wrap = bool(el.get("wrap"))
    font_path = resolve_font(el.get("font"), text)

    if el.get("scaled"):
        hi = min(box_h / line_h, float(el.get("maxTs") or 100) * k)
        lo = max(1.0, float(el.get("minTs") or 1) * k)
        size = lo
        while hi - lo > 0.5:  # biggest size that still fits the box (like TextScaled)
            mid = (lo + hi) / 2
            _, _, w, h = _layout_text(text, font_path, mid, box_w, wrap, line_h)
            if w <= box_w and h <= box_h:
                size = lo = mid
            else:
                hi = mid
    else:
        size = float(el.get("ts") or 14) * k
    font, lines, _, total_h = _layout_text(text, font_path, size, box_w, wrap, line_h)

    ya = el.get("ya", "Center")
    top = box_y if ya == "Top" else (box_y + box_h - total_h if ya == "Bottom" else box_y + (box_h - total_h) / 2)
    xa = el.get("xa", "Center")
    anchor_x, ax = ("l", box_x) if xa == "Left" else (("r", box_x + box_w) if xa == "Right" else ("m", box_x + box_w / 2))

    stroke = el.get("stroke") if isinstance(el.get("stroke"), dict) else None
    stroke_w = int(round(float(stroke.get("th", 1)) * k)) if stroke else 0
    fill = _rgba(el.get("tc"), el.get("tt"))
    layer = _layer(canvas.size)
    draw = ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        cy = top + (i + 0.5) * size * line_h
        if stroke_w > 0:
            # Stroke first (own transparency), then the glyphs on top
            draw.text((ax, cy), line, font=font, anchor=anchor_x + "m", fill=_rgba(stroke.get("c"), stroke.get("t")),
                      stroke_width=stroke_w, stroke_fill=_rgba(stroke.get("c"), stroke.get("t")))
        draw.text((ax, cy), line, font=font, anchor=anchor_x + "m", fill=fill)
    canvas.alpha_composite(layer)


def render_gui(gui, fetch):
    """Renders a SurfaceGui layer to an RGBA image. Returns (image, complete)."""
    from PIL import Image
    gw, gh = float(gui.get("w", 0)), float(gui.get("h", 0))
    if gw <= 0 or gh <= 0:
        return None, True
    k = max(0.25, min(8.0, BAKE_LONG_SIDE / max(gw, gh)))
    canvas = Image.new("RGBA", (max(1, int(round(gw * k))), max(1, int(round(gh * k)))), (0, 0, 0, 0))
    complete = True
    for el in gui.get("elements", []):
        if not isinstance(el, dict):
            continue
        try:
            _draw_box(canvas, el, k)
            if el.get("img"):
                complete = _draw_image(canvas, el, k, fetch) and complete
            if el.get("text"):
                _draw_text(canvas, el, k)
        except Exception as e:
            print(f"[Roblox Live Link] SurfaceGui element skipped: {e}")
    return canvas, complete


def bake_face_overlay(overlay_layers, face_w, face_h, fetch, out_dir):
    """
    Composites the Decals and SurfaceGuis of one face into a single PNG (decals stretched over the face,
    SurfaceGui canvas stretched over the face, as in Roblox). Returns (path or None, complete).
    Incomplete bakes (images still downloading) are not saved, so the next sync retries.
    """
    if not overlay_layers:
        return None, True
    try:
        from PIL import Image
    except ImportError:
        return None, True
    aspect = max(1e-3, face_w) / max(1e-3, face_h)
    key = signature({"layers": overlay_layers, "aspect": round(aspect, 3), "v": 1})
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"RbxFace_{key[:16]}.png")
    if os.path.exists(out_path):
        return out_path, True

    if aspect >= 1:
        size = (BAKE_LONG_SIDE, max(16, int(round(BAKE_LONG_SIDE / aspect))))
    else:
        size = (max(16, int(round(BAKE_LONG_SIDE * aspect))), BAKE_LONG_SIDE)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    complete = True
    for layer in overlay_layers:
        if layer.get("kind") == "decal":
            path = fetch(layer.get("img"))
            if not path:
                complete = False
                continue
            try:
                img = Image.open(path).convert("RGBA").resize(size, Image.LANCZOS)
            except Exception:
                continue
            canvas.alpha_composite(_tint(img, layer.get("c"), layer.get("t")))
        elif layer.get("kind") == "gui":
            img, ok = render_gui(layer, fetch)
            complete = complete and ok
            if img is not None:
                canvas.alpha_composite(img.resize(size, Image.LANCZOS))
    if not complete:
        return None, False
    if canvas.getbbox() is None:
        return None, True  # fully transparent: nothing to show
    tmp = out_path + ".part"
    canvas.save(tmp, "PNG")
    os.replace(tmp, out_path)
    return out_path, True
