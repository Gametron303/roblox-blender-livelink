"""
Roblox Studio <-> Blender Live-Link: Sync Manager
High-performance non-blocking synchronization engine with authentic Roblox PBR textures,
exact Roblox face-aligned UV projection, custom Decals & Textures, and chunked ingestion.
"""

import collections
import hashlib
import math
import os
import time
import json
import uuid
import bmesh
import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector
from . import paths
from . import server
from . import mesh_loader
from . import surfaces
from . import procedural
from . import downloads

COLLECTION_NAME = "Roblox_LiveLink"
_id_to_obj = {}
_mat_cache = {}
_img_cache = {}
_is_timer_running = False

_failed_tex_ids = set()
# Set to True when a texture/mesh had to be downloaded while building an object;
# such objects get re-applied automatically once downloads finish (see _retry_instances).
_assets_missing = False
# Set to True when a texture needed by a material couldn't be provided (downloading or failed)
_tex_unavailable = False

_outgoing_changes = {}
# rbx_id -> {"matrix": Matrix (world matrix after last sync), "scale": Vector, "size": [x, y, z] (Roblox studs),
#            "offset": Vector | None (SpecialMesh offset, Blender local), "is_model": bool}
_sync_state = {}
_is_applying_roblox_update = False

CHUNK_SIZE = 200
_pending_instances = collections.deque()
_pending_stale_ids = collections.deque()

# rbx_id -> [inst_data, attempts]: objects built with missing textures/meshes, re-applied after downloads
_retry_instances = {}
_RETRY_INTERVAL = 1.0
_RETRY_MAX_ATTEMPTS = 15
_last_retry_time = 0.0

# Chunked full sync: collects ids from every chunk, prunes stale objects after the last one
_full_sync_ids = None
_full_sync_prune = True

_sync_total = 0
_sync_current = 0
_sync_processed = 0
_sync_in_progress = False

ADDON_DIR = paths.ADDON_DIR
# Downloaded Roblox images and generated text textures (extension user folder)
CUSTOM_TEX_DIR = paths.cache_dir("custom_textures")
# Baked per-face images (Decals + SurfaceGuis composited by surfaces.bake_face_overlay)
FACE_TEX_DIR = os.path.join(CUSTOM_TEX_DIR, "faces")

CSG_COLLECTION_NAME = "Roblox_CSG_Operations"

# Bumped when the look of generated materials changes: older ones saved in a .blend get rebuilt
MATERIAL_VERSION = 4

# Base shared meshes indexed by (shape, decal_face)
_base_meshes = {}


_path_to_collection = {}


def get_or_create_collection():
    if COLLECTION_NAME in bpy.data.collections:
        col = bpy.data.collections[COLLECTION_NAME]
    else:
        col = bpy.data.collections.new(COLLECTION_NAME)
        bpy.context.scene.collection.children.link(col)
    return col


def get_or_create_csg_collection():
    parent_col = get_or_create_collection()
    if CSG_COLLECTION_NAME in bpy.data.collections:
        return bpy.data.collections[CSG_COLLECTION_NAME]
    csg_col = bpy.data.collections.new(CSG_COLLECTION_NAME)
    parent_col.children.link(csg_col)
    return csg_col


def get_or_create_collection_hierarchy(folder_path=None):
    """
    Returns the leaf Blender collection for the given folder_path list.
    E.g. ['Scene', 'Vegetation'] -> Roblox_LiveLink -> Scene -> Vegetation
    Collections are nested hierarchically under Roblox_LiveLink.
    """
    root_col = get_or_create_collection()
    if not folder_path or not isinstance(folder_path, (list, tuple)):
        return root_col

    current_parent = root_col
    current_key_parts = []

    for segment in folder_path:
        seg_clean = str(segment).strip()
        if not seg_clean:
            continue
        current_key_parts.append(seg_clean)
        full_key = "/".join(current_key_parts)

        col = _path_to_collection.get(full_key)
        col_valid = False
        if col:
            try:
                col_valid = (col.name in bpy.data.collections) and (col.name in current_parent.children)
            except Exception:
                col_valid = False

        if not col_valid:
            # Check if current_parent already has a child collection with this name
            col = current_parent.children.get(seg_clean)
            if not col:
                col = bpy.data.collections.new(seg_clean)
                current_parent.children.link(col)
            _path_to_collection[full_key] = col

        current_parent = col

    return current_parent


def get_sync_status():
    obj_count = len(_id_to_obj)
    pending = len(_pending_instances)
    in_prog = _sync_in_progress and (pending > 0 or (_sync_total > 0 and _sync_processed < _sync_total))

    if in_prog and _sync_total > 0:
        total = _sync_total
        if pending > 0:
            current = min(total, max(0, total - pending))
        else:
            current = min(total, _sync_processed)
        pending_count = pending
        pct = (current / total * 100.0) if total > 0 else 0.0
    else:
        total = obj_count
        current = obj_count
        pending_count = 0
        pct = 100.0

    return {
        "in_progress": in_prog,
        "total": total,
        "current": current,
        "pending": pending_count,
        "pct": min(100.0, max(0.0, pct)),
        "total_objects": obj_count,
    }


def rebuild_id_map():
    _id_to_obj.clear()
    _path_to_collection.clear()
    if COLLECTION_NAME in bpy.data.collections:
        col = bpy.data.collections[COLLECTION_NAME]
        seen_ids = set()
        for obj in list(col.all_objects):
            rbx_id = obj.get("rbx_id")
            if not rbx_id:
                continue
            if rbx_id in seen_ids:
                # Blender duplicate (Shift+D) of a synced object: keep the user's copy, just unlink it from sync
                _detach_duplicate(obj)
            else:
                seen_ids.add(rbx_id)
                _id_to_obj[rbx_id] = obj


def build_cube_mesh(name, target_face=None):
    """
    Builds a unit cube with mathematically exact Roblox face-aligned UVs.
    If target_face is specified ('Front', 'Top', etc.), only that face receives [0, 1] UVs,
    while other faces are set to (-10, -10) to cleanly isolate the decal.
    """
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    uv_layer = bm.loops.layers.uv.new("UVMap")

    for face in bm.faces:
        norm = face.normal
        is_front = norm.y > 0.9    # Roblox Front (-Z in rbx -> +Y in bl)
        is_top = norm.z > 0.9      # Roblox Top (+Y in rbx -> +Z in bl)
        is_back = norm.y < -0.9    # Roblox Back (+Z in rbx -> -Y in bl)
        is_bottom = norm.z < -0.9  # Roblox Bottom (-Y in rbx -> -Z in bl)
        is_left = norm.x < -0.9    # Roblox Left (-X in rbx -> -X in bl)
        is_right = norm.x > 0.9    # Roblox Right (+X in rbx -> +X in bl)

        # Check if this face should show the decal
        show_decal = True
        if target_face == "Front" and not is_front:
            show_decal = False
        elif target_face == "Top" and not is_top:
            show_decal = False
        elif target_face == "Back" and not is_back:
            show_decal = False
        elif target_face == "Bottom" and not is_bottom:
            show_decal = False
        elif target_face == "Left" and not is_left:
            show_decal = False
        elif target_face == "Right" and not is_right:
            show_decal = False

        for loop in face.loops:
            if not show_decal:
                loop[uv_layer].uv = (-10.0, -10.0)
                continue

            v = loop.vert.co
            if is_front:
                # Roblox Front face: U goes with -X (viewer's left to right), V with +Z (up)
                u = 0.5 - v.x
                val_v = v.z + 0.5
            elif is_top:
                # Roblox Top face: U goes with +X, V goes with +Y (Roblox -Z / Forward)
                u = v.x + 0.5
                val_v = v.y + 0.5
            elif is_back:
                # Roblox Back face: U goes with +X (viewer's left to right), V with +Z (up)
                u = v.x + 0.5
                val_v = v.z + 0.5
            elif is_bottom:
                # Roblox Bottom face: U goes with +X, V goes with -Y (Roblox +Z / Back)
                u = v.x + 0.5
                val_v = 0.5 - v.y
            elif is_left:
                # Roblox Left face: U goes with -Y (Roblox +Z / Back), V with +Z (up)
                u = 0.5 - v.y
                val_v = v.z + 0.5
            elif is_right:
                # Roblox Right face: U goes with +Y (Roblox -Z / Front), V with +Z (up)
                u = v.y + 0.5
                val_v = v.z + 0.5
            else:
                u, val_v = 0.0, 0.0

            loop[uv_layer].uv = (u, val_v)

    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(None)
    return mesh


def is_mesh_valid(mesh):
    if not mesh:
        return False
    try:
        _ = mesh.name
        return mesh.name in bpy.data.meshes
    except (ReferenceError, Exception):
        return False


def is_material_valid(mat):
    if not mat:
        return False
    try:
        _ = mat.name
        return mat.name in bpy.data.materials
    except (ReferenceError, Exception):
        return False


def is_image_valid(img):
    if not img:
        return False
    try:
        _ = img.name
        return img.name in bpy.data.images
    except (ReferenceError, Exception):
        return False


_SHARED_MESH_NAMES = {
    ("Block", None): "RbxShared_Cube",
    **{("Block", f): f"RbxShared_Cube_{f}" for f in ("Front", "Top", "Back", "Bottom", "Left", "Right")},
    ("Sphere", None): "RbxShared_Sphere",
    ("Ball", None): "RbxShared_Sphere",
    ("Cylinder", None): "RbxShared_Cylinder",
    ("Wedge", None): "RbxShared_Wedge",
}


def _build_shared(canonical, mesh_name):
    """Builds the geometry of a shared primitive mesh ("RbxShared_Cube", "RbxShared_Cube_Top"...) into a new mesh."""
    if canonical == "RbxShared_Cube":
        return build_cube_mesh(mesh_name)
    if canonical.startswith("RbxShared_Cube_"):
        return build_cube_mesh(mesh_name, target_face=canonical.rsplit("_", 1)[1])
    mesh = bpy.data.meshes.new(mesh_name)
    bm = bmesh.new()
    if canonical == "RbxShared_Sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=14, radius=0.5)
    elif canonical == "RbxShared_Cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=20, radius1=0.5, radius2=0.5,
                              depth=1.0, matrix=Matrix.Rotation(math.radians(90.0), 4, 'Y'))
    else:  # RbxShared_Wedge
        rbx_verts = [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, -0.5, 0.5),
                     (-0.5, -0.5, 0.5), (-0.5, 0.5, 0.5), (0.5, 0.5, 0.5)]
        v = [bm.verts.new(Vector((x, -z, y))) for x, y, z in rbx_verts]
        bm.faces.new([v[0], v[1], v[2], v[3]])
        bm.faces.new([v[3], v[2], v[5], v[4]])
        bm.faces.new([v[0], v[3], v[4]])
        bm.faces.new([v[1], v[5], v[2]])
        bm.faces.new([v[0], v[4], v[5], v[1]])
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(None)
    return mesh


def _unit_extent(mesh):
    """Shared primitives fit exactly in a unit box centered on the origin."""
    if len(mesh.vertices) == 0:
        return False
    extent = max(abs(c) for v in mesh.vertices for c in v.co)
    return 0.499 < extent < 0.501


def _repair_shared(canonical, mesh):
    """
    The shared mesh was edited in Blender (e.g. "Apply Scale" on a part changes the shared mesh in place,
    because its other user is only the fake user). Rebuild it in place so every part using it is fixed at once.
    """
    fresh = _build_shared(canonical, canonical + "_rebuild")
    mesh_loader.copy_geometry(mesh, fresh)
    bpy.data.meshes.remove(fresh)
    mesh_loader.stamp_geometry(mesh)
    print(f"[Roblox Live Link] Restored edited shared mesh {canonical}")


def init_shared_meshes():
    for key, canonical in _SHARED_MESH_NAMES.items():
        mesh = _base_meshes.get(key)
        if not is_mesh_valid(mesh):
            # Reuse meshes already in the .blend (after "Reload Addon" or reopening a file) instead of ".001" copies
            mesh = bpy.data.meshes.get(canonical)
            if mesh is None or len(mesh.vertices) == 0:
                mesh = _build_shared(canonical, canonical)
                mesh.use_fake_user = True
                mesh_loader.stamp_geometry(mesh)
            elif mesh.get("rbx_geo") is None:
                # Mesh from an older version: check it once, then remember its geometry
                if not _unit_extent(mesh):
                    _repair_shared(canonical, mesh)
                mesh_loader.stamp_geometry(mesh)
            _base_meshes[key] = mesh
            mesh_loader.protect_shared(mesh)
        if not mesh_loader.geometry_intact(mesh):
            _repair_shared(canonical, mesh)


def get_shared_mesh(shape_type, target_face=None):
    init_shared_meshes()
    shape = (shape_type or "Block").capitalize()
    key = (shape, target_face)
    mesh = _base_meshes.get(key)
    if is_mesh_valid(mesh):
        return mesh
    return _base_meshes.get((shape, None), _base_meshes[("Block", None)])


def load_cached_image(file_path, image_name):
    if not os.path.exists(file_path):
        return None
    cached = _img_cache.get(image_name)
    if is_image_valid(cached):
        return cached
    _img_cache.pop(image_name, None)

    if image_name in bpy.data.images:
        img = bpy.data.images[image_name]
        if is_image_valid(img):
            img.use_fake_user = True
            _img_cache[image_name] = img
            return img

    try:
        img = bpy.data.images.load(file_path)
        img.name = image_name
        img.use_fake_user = True
        _img_cache[image_name] = img
        return img
    except Exception:
        return None


def save_and_enhance_texture(raw_bytes, cache_path):
    # Write to a temp file and rename: the main thread checks os.path.exists(cache_path)
    # and must never load a half-written PNG
    tmp_path = cache_path + ".part"
    scale = 1
    try:
        from PIL import Image, ImageFilter
        import io
        pil_img = Image.open(io.BytesIO(raw_bytes))
        w, h = pil_img.size
        # Upscale low-res/thumbnail textures (< 1024) to crisp high-res
        if w < 1024 or h < 1024:
            scale = max(2, min(4, 1024 // max(1, min(w, h))))
            if scale > 1:
                pil_img = pil_img.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
                pil_img = pil_img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=140, threshold=2))
        pil_img.save(tmp_path, "PNG")
    except Exception:
        scale = 1
        with open(tmp_path, "wb") as f:
            f.write(raw_bytes)
    # The upscale factor is needed later to cut sprite-sheet regions (ImageRectOffset/Size in original pixels)
    try:
        with open(cache_path + ".json", "w", encoding="utf-8") as f:
            json.dump({"scale": scale}, f)
    except OSError:
        pass
    os.replace(tmp_path, cache_path)


def async_download_custom_texture(asset_id_str):
    """Downloads a Roblox image in a separate process (see downloads.py); never blocks Blender."""
    if not asset_id_str or asset_id_str in _failed_tex_ids:
        return
    if not asset_id_str.isdigit():
        # Non-numeric ids (rbxasset://, local paths) can't be downloaded
        _failed_tex_ids.add(asset_id_str)
        return
    cache_path = os.path.join(CUSTOM_TEX_DIR, f"{asset_id_str}.png")
    if os.path.exists(cache_path):
        return
    raw_path = cache_path + ".download"

    def on_done(ok):
        try:
            if ok:
                with open(raw_path, "rb") as f:
                    save_and_enhance_texture(f.read(), cache_path)
        except OSError as e:
            print(f"[Roblox Live Link] Texture {asset_id_str} could not be saved: {e}")
        finally:
            if os.path.exists(raw_path):
                os.remove(raw_path)
            if not os.path.exists(cache_path):
                _failed_tex_ids.add(asset_id_str)

    downloads.start("texture", asset_id_str, raw_path, on_done)


def _clean_texture_id(asset_id_str):
    clean_id = str(asset_id_str).strip()
    if "spawnlocation" in clean_id.lower():
        return "SpawnLocation"
    import re
    match = re.search(r"[?&]id=(\d+)", clean_id)
    if not match:
        match = re.search(r"rbxassetid://(\d+)", clean_id)
    if match:
        return match.group(1)
    digits = re.findall(r"\d+", clean_id)
    return max(digits, key=len) if digits else clean_id


def texture_file(content):
    """
    Local file for a Roblox image (rbxassetid://, asset URL, numeric id or rbxasset:// Studio content).
    Starts a background download and flags _assets_missing when the file isn't there yet.
    """
    if not content:
        return None
    local = surfaces.rbxasset_path(str(content).strip())
    if local:
        return local
    clean_id = _clean_texture_id(content)
    path = os.path.join(CUSTOM_TEX_DIR, f"{clean_id}.png")
    if os.path.exists(path):
        return path
    global _tex_unavailable, _assets_missing
    _tex_unavailable = True
    if not paths.online_access_allowed():
        return None  # "Allow Online Access" is off: no downloads, no retries
    if clean_id not in _failed_tex_ids:
        async_download_custom_texture(clean_id)
        if clean_id not in _failed_tex_ids:
            _assets_missing = True
    return None


def get_custom_texture_image(asset_id_str):
    path = texture_file(asset_id_str)
    if not path:
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    return load_cached_image(path, f"RbxTex_{name}")


def generate_text_texture_image(text, text_color=(1, 1, 1), bg_color=(1, 1, 1), bg_trans=1.0, aspect_ratio=2.0):
    from PIL import Image, ImageDraw, ImageFont
    import hashlib

    text_clean = str(text).strip()
    tc_tuple = tuple(round(c, 2) for c in text_color)
    bg_tuple = tuple(round(c, 2) for c in bg_color)
    text_hash = hashlib.md5(f"{text_clean}_{tc_tuple}_{bg_tuple}_{round(bg_trans, 2)}".encode()).hexdigest()[:10]
    img_name = f"RbxText_{text_hash}"
    file_path = os.path.join(CUSTOM_TEX_DIR, f"{img_name}.png")

    if not os.path.exists(file_path):
        width = 1024
        height = 512

        if bg_trans >= 0.99:
            bg_rgba = (0, 0, 0, 0)
        else:
            alpha = int((1.0 - bg_trans) * 255)
            bg_rgba = (int(bg_color[0]*255), int(bg_color[1]*255), int(bg_color[2]*255), alpha)

        img = Image.new("RGBA", (width, height), bg_rgba)
        draw = ImageDraw.Draw(img)

        font_size = 200
        try:
            font = ImageFont.truetype("arialbd.ttf", font_size)
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text_clean, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        if text_w > width * 0.88 and text_w > 0:
            scale_factor = (width * 0.88) / text_w
            font_size = max(16, int(font_size * scale_factor))
            try:
                font = ImageFont.truetype("arialbd.ttf", font_size)
            except Exception:
                try:
                    font = ImageFont.truetype("arial.ttf", font_size)
                except Exception:
                    font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text_clean, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

        x = (width - text_w) // 2 - bbox[0]
        y = (height - text_h) // 2 - bbox[1]

        tc_rgba = (int(text_color[0]*255), int(text_color[1]*255), int(text_color[2]*255), 255)
        draw.text((x, y), text_clean, font=font, fill=tc_rgba)
        img.save(file_path, "PNG")

    return load_cached_image(file_path, img_name)


def get_configured_tex_scale():
    try:
        if hasattr(bpy.context, "scene"):
            if hasattr(bpy.context.scene, "roblox_livelink_settings") and hasattr(bpy.context.scene.roblox_livelink_settings, "tex_scale"):
                return float(bpy.context.scene.roblox_livelink_settings.tex_scale)
            if hasattr(bpy.context.scene, "roblox_livelink_tex_scale"):
                return float(bpy.context.scene.roblox_livelink_tex_scale)
    except Exception:
        pass
    return 0.0625


def _lacks_expected_texture(mat, tex_type, tex_id):
    """True for image-based materials that were saved without their image node (built while downloading)."""
    if not tex_id or tex_type not in ("Decal", "Face", "MeshTexture", "Clothing", "Shirt", "Pants"):
        return False
    try:
        return not any(n.type == 'TEX_IMAGE' and n.image for n in mat.node_tree.nodes)
    except Exception:
        return False


def _text_signature(texture_info):
    """Everything that changes a generated SurfaceGui text texture (text and its colors)."""
    if not texture_info or not texture_info.get("text"):
        return ""
    return json.dumps([str(texture_info.get("text")), texture_info.get("textColor"),
                       texture_info.get("bgColor"), texture_info.get("bgTrans")], default=str)


# Blender (before 5.0) keeps at most 63 characters of an ID name. A longer name gets truncated,
# so bpy.data.materials.get(full_name) never finds the existing material and a copy is made each session.
_MAX_ID_NAME = 63


def _fit_id_name(name):
    if len(name) <= _MAX_ID_NAME - 8:  # leave room for "_pending" / "_notex" suffixes
        return name
    return name[:_MAX_ID_NAME - 18] + "_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:9]


def _material_key(r, g, b, transparency, rbx_material, texture_info, material_variant):
    tex_id = str(texture_info.get("id") or texture_info.get("textureId") or "") if texture_info else ""
    tex_type = texture_info.get("type") if texture_info else ""
    text_sig = _text_signature(texture_info)
    mv_sig = json.dumps(material_variant, sort_keys=True, default=str) if material_variant else ""
    return (round(r, 2), round(g, 2), round(b, 2), round(transparency, 2), rbx_material, tex_type, tex_id, text_sig, mv_sig)


# key -> material built while some of its textures were still downloading
_pending_mats = {}


def get_or_create_material(r, g, b, transparency=0.0, rbx_material="Plastic", reflectance=0.0, texture_info=None, material_variant=None):
    """
    Returns a cached material. If a texture is still downloading, returns a temporary "_pending"
    material (not cached under the final name) and flags _assets_missing so the object is re-applied later.
    """
    global _assets_missing
    key = _material_key(r, g, b, transparency, rbx_material, texture_info, material_variant)

    pending = _pending_mats.get(key)
    if pending is not None:
        if is_material_valid(pending) and downloads.busy():
            _assets_missing = True
            return pending
        _pending_mats.pop(key, None)

    global _tex_unavailable
    prev_missing = _assets_missing
    _assets_missing = False
    _tex_unavailable = False
    try:
        mat = _build_material(r, g, b, transparency, rbx_material, reflectance, texture_info, material_variant, key)
        missing = _assets_missing
        unavailable = _tex_unavailable
    finally:
        _assets_missing = prev_missing or _assets_missing

    if mat is not None and mat.get("rbx_built") is None:
        # Freshly built material: never let a texture-less build occupy the final name,
        # otherwise it would be reused forever (even in later sessions when the texture is available)
        mat["rbx_built"] = True
        if missing:
            if _mat_cache.get(key) is mat:
                _mat_cache.pop(key, None)
            mat.name = mat.name + "_pending"
            _pending_mats[key] = mat
        elif unavailable:
            mat.name = mat.name + "_notex"
    return mat


def _build_material(r, g, b, transparency, rbx_material, reflectance, texture_info, material_variant, key):
    tex_id = str(texture_info.get("id") or texture_info.get("textureId") or "") if texture_info else ""
    tex_type = texture_info.get("type") if texture_info else ""
    text_content = texture_info.get("text", "") if texture_info else ""
    mv_name = material_variant.get("name", "") if material_variant else ""

    cached_mat = _mat_cache.get(key)
    if is_material_valid(cached_mat):
        return cached_mat
    _mat_cache.pop(key, None)

    trans_suffix = f"_a{int((1.0 - transparency)*100)}" if transparency > 0.0 else "_opaque"
    mat_name = f"Rbx_{rbx_material}_{int(r*255)}_{int(g*255)}_{int(b*255)}{trans_suffix}"
    if mv_name:
        mat_name += f"_mv_{mv_name}"
    if tex_id:
        mat_name += f"_{tex_type}_{tex_id}"
    if text_content:
        # Old plugin versions sent the same id for every Cyrillic text: name by the text itself
        mat_name += "_txt" + hashlib.md5(_text_signature(texture_info).encode("utf-8")).hexdigest()[:8]
    mat_name = _fit_id_name(mat_name)

    existing = bpy.data.materials.get(mat_name)
    if (is_material_valid(existing) and not _lacks_expected_texture(existing, tex_type, tex_id)
            and existing.get("rbx_mat_version") == MATERIAL_VERSION):
        _mat_cache[key] = existing
        return existing
    if is_material_valid(existing):
        # Legacy material built before its texture was downloaded: free the name and rebuild
        existing.name = mat_name + "_stale"

    mat = bpy.data.materials.new(name=mat_name)
    mat["rbx_mat_version"] = MATERIAL_VERSION
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    if not bsdf:
        _mat_cache[key] = mat
        return mat

    alpha = max(0.0, min(1.0, 1.0 - transparency))
    bsdf.inputs["Alpha"].default_value = alpha

    # 0. Custom MaterialVariant (e.g. custom PBR Grass on Baseplate from MaterialService)
    if material_variant and isinstance(material_variant, dict):
        cm_id = material_variant.get("colorMap", "")
        if cm_id:
            cm_img = get_custom_texture_image(cm_id)
            if cm_img:
                geom = nodes.new('ShaderNodeNewGeometry')
                mapping = nodes.new('ShaderNodeMapping')
                # MaterialVariant.StudsPerTile: one texture repeat per N studs
                studs_per_tile = float(material_variant.get("studsPerTile") or 0)
                tex_scale = 1.0 / studs_per_tile if studs_per_tile > 0 else get_configured_tex_scale()
                mapping.inputs['Scale'].default_value = (tex_scale, tex_scale, tex_scale)
                links.new(geom.outputs['Position'], mapping.inputs['Vector'])

                tex_color = nodes.new('ShaderNodeTexImage')
                tex_color.image = cm_img
                tex_color.projection = 'BOX'
                tex_color.projection_blend = 0.0
                links.new(mapping.outputs['Vector'], tex_color.inputs['Vector'])

                mix = nodes.new('ShaderNodeMix')
                mix.data_type = 'RGBA'
                mix.blend_type = 'MULTIPLY'
                mix.inputs['Factor'].default_value = 1.0
                links.new(tex_color.outputs['Color'], mix.inputs[6])
                mix.inputs[7].default_value = (r, g, b, 1.0)
                links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])

                nm_id = material_variant.get("normalMap", "")
                if nm_id:
                    nm_img = get_custom_texture_image(nm_id)
                    if nm_img:
                        nm_img.colorspace_settings.name = 'Non-Color'
                        tex_norm = nodes.new('ShaderNodeTexImage')
                        tex_norm.image = nm_img
                        tex_norm.projection = 'BOX'
                        tex_norm.projection_blend = 0.0
                        links.new(mapping.outputs['Vector'], tex_norm.inputs['Vector'])

                        norm_node = nodes.new('ShaderNodeNormalMap')
                        norm_node.inputs['Strength'].default_value = 0.5
                        links.new(tex_norm.outputs['Color'], norm_node.inputs['Color'])
                        links.new(norm_node.outputs['Normal'], bsdf.inputs['Normal'])

                rm_id = material_variant.get("roughnessMap", "")
                if rm_id:
                    rm_img = get_custom_texture_image(rm_id)
                    if rm_img:
                        rm_img.colorspace_settings.name = 'Non-Color'
                        tex_rough = nodes.new('ShaderNodeTexImage')
                        tex_rough.image = rm_img
                        tex_rough.projection = 'BOX'
                        tex_rough.projection_blend = 0.0
                        links.new(mapping.outputs['Vector'], tex_rough.inputs['Vector'])
                        links.new(tex_rough.outputs['Color'], bsdf.inputs['Roughness'])
                else:
                    bsdf.inputs['Roughness'].default_value = 0.8

                mm_id = material_variant.get("metalnessMap", "")
                mm_img = get_custom_texture_image(mm_id) if mm_id else None
                if mm_img:
                    mm_img.colorspace_settings.name = 'Non-Color'
                    tex_metal = nodes.new('ShaderNodeTexImage')
                    tex_metal.image = mm_img
                    tex_metal.projection = 'BOX'
                    tex_metal.projection_blend = 0.0
                    links.new(mapping.outputs['Vector'], tex_metal.inputs['Vector'])
                    links.new(tex_metal.outputs['Color'], bsdf.inputs['Metallic'])

                _mat_cache[key] = mat
                return mat

    # 1. Custom Mesh Texture / Clothing (Shirt, Pants, Hair, Accessories, Model textures)
    if texture_info and tex_id and tex_type in ("MeshTexture", "Clothing", "Shirt", "Pants"):
        img = get_custom_texture_image(tex_id)
        if img:
            tex_coord = nodes.new('ShaderNodeTexCoord')
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.interpolation = 'Cubic'
            links.new(tex_coord.outputs['UV'], tex_node.inputs['Vector'])

            # Mix Part Base Color with Texture Color using Texture Alpha
            # This ensures transparent textures (like face decals on Head or torn clothing) blend with skin color
            mix = nodes.new('ShaderNodeMix')
            mix.data_type = 'RGBA'
            mix.blend_type = 'MIX'
            links.new(tex_node.outputs['Alpha'], mix.inputs['Factor'])
            mix.inputs[6].default_value = (r, g, b, 1.0)         # Base (Part Color)
            links.new(tex_node.outputs['Color'], mix.inputs[7]) # Overlay (Texture Image)
            links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])

            bsdf.inputs['Roughness'].default_value = 0.5
            _mat_cache[key] = mat
            return mat

    # 1b. Custom Decals and Face decals
    if texture_info and (tex_type in ("Decal", "Face")) and tex_id:
        img = get_custom_texture_image(tex_id)
        if img:
            tex_coord = nodes.new('ShaderNodeTexCoord')
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.interpolation = 'Cubic'
            tex_node.extension = 'CLIP'
            links.new(tex_coord.outputs['UV'], tex_node.inputs['Vector'])

            # Mix Part Base Color with Decal Color using Decal Alpha
            mix = nodes.new('ShaderNodeMix')
            mix.data_type = 'RGBA'
            mix.blend_type = 'MIX'
            links.new(tex_node.outputs['Alpha'], mix.inputs['Factor'])
            mix.inputs[6].default_value = (r, g, b, 1.0)         # Base (Part Color)
            links.new(tex_node.outputs['Color'], mix.inputs[7]) # Overlay (Decal Image)
            links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])

            bsdf.inputs['Roughness'].default_value = 0.5
            _mat_cache[key] = mat
            return mat

    # 1b. SurfaceGui with TextLabel (e.g. FOR SALE signs, book titles)
    if texture_info and texture_info.get("type") == "SurfaceGui" and texture_info.get("text"):
        text = texture_info.get("text")
        tc = texture_info.get("textColor", [1.0, 1.0, 1.0])
        bg = texture_info.get("bgColor", [1.0, 1.0, 1.0])
        bgt = float(texture_info.get("bgTrans", 1.0))
        try:
            img = generate_text_texture_image(text, text_color=tc, bg_color=bg, bg_trans=bgt)
        except Exception as e:
            # PIL missing or font error: fall back to a plain colored part instead of failing the whole object
            print(f"[Roblox Live Link] Text texture failed ({e}), using plain color")
            img = None
        if img:
            tex_coord = nodes.new('ShaderNodeTexCoord')
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.extension = 'CLIP'
            links.new(tex_coord.outputs['UV'], tex_node.inputs['Vector'])

            mix = nodes.new('ShaderNodeMix')
            mix.data_type = 'RGBA'
            mix.blend_type = 'MIX'
            links.new(tex_node.outputs['Alpha'], mix.inputs['Factor'])
            mix.inputs[6].default_value = (r, g, b, 1.0)
            links.new(tex_node.outputs['Color'], mix.inputs[7])
            links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])

            mat.blend_method = 'CLIP'
            _mat_cache[key] = mat
            return mat

    # 2. Custom Texture with tiling (Baseplate grid, doors, etc.)
    tex_trans = float(texture_info.get("transparency", 0.0)) if texture_info else 0.0
    if texture_info and texture_info.get("type") == "Texture" and tex_id and tex_trans < 0.75:
        img = get_custom_texture_image(tex_id)
        if img:
            studs_u = max(0.2, float(texture_info.get("studsU", 2.0)))
            studs_v = max(0.2, float(texture_info.get("studsV", 2.0)))

            geom = nodes.new('ShaderNodeNewGeometry')
            mapping = nodes.new('ShaderNodeMapping')
            # World-aligned per-stud scaling: 1 repetition every studs_u / studs_v studs
            mapping.inputs['Scale'].default_value = (1.0 / studs_u, 1.0 / studs_v, 1.0 / studs_v)
            links.new(geom.outputs['Position'], mapping.inputs['Vector'])

            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.projection = 'BOX'
            tex_node.projection_blend = 0.0
            links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

            mix = nodes.new('ShaderNodeMix')
            mix.data_type = 'RGBA'
            mix.blend_type = 'MULTIPLY'
            mix.inputs['Factor'].default_value = 1.0
            links.new(tex_node.outputs['Color'], mix.inputs[6])
            mix.inputs[7].default_value = (r, g, b, 1.0)
            links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])
            _mat_cache[key] = mat
            return mat

    # 3. Built-in Roblox materials: procedural patterns tinted by the part color (no image files)
    if procedural.apply(mat.node_tree, bsdf, rbx_material, (r, g, b), get_configured_tex_scale()):
        _mat_cache[key] = mat
        return mat

    # 4. Neon Shader
    if rbx_material == "Neon":
        bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
        bsdf.inputs["Emission Color"].default_value = (r, g, b, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 4.0
        bsdf.inputs["Roughness"].default_value = 0.1
        _mat_cache[key] = mat
        return mat

    # 5. SmoothPlastic / Plastic Fallback
    bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.15 if rbx_material == "SmoothPlastic" else 0.5
    _mat_cache[key] = mat
    return mat


def rbx_cframe_to_blender_matrix(pos, rot):
    bl_pos = Vector((pos[0], -pos[2], pos[1]))
    r00, r01, r02 = rot[0], rot[1], rot[2]
    r10, r11, r12 = rot[3], rot[4], rot[5]
    r20, r21, r22 = rot[6], rot[7], rot[8]

    mat = Matrix([
        [r00,  -r02,  r01, bl_pos.x],
        [-r20,  r22, -r21, bl_pos.y],
        [r10,  -r12,  r11, bl_pos.z],
        [0.0,   0.0,  0.0, 1.0]
    ])
    return mat


def blender_matrix_to_rbx_cframe(mat):
    bx, by, bz = mat.to_translation()
    rx = bx
    ry = bz
    rz = -by

    rot_mat = mat.to_3x3()
    rot_mat.normalize()

    r00 = rot_mat[0][0]
    r01 = rot_mat[0][2]
    r02 = -rot_mat[0][1]

    r10 = rot_mat[2][0]
    r11 = rot_mat[2][2]
    r12 = -rot_mat[2][1]

    r20 = -rot_mat[1][0]
    r21 = -rot_mat[1][2]
    r22 = rot_mat[1][1]

    rot = [r00, r01, r02, r10, r11, r12, r20, r21, r22]
    pos = [rx, ry, rz]
    return pos, rot


def blender_dimensions_to_rbx_size(obj):
    dim = obj.dimensions
    sx = max(0.05, float(dim.x))
    sy = max(0.05, float(dim.z))
    sz = max(0.05, float(dim.y))
    return [sx, sy, sz]


def _strip_blender_suffix(name):
    """'Lights.001' -> 'Lights' (Blender makes collection names globally unique, Roblox doesn't)."""
    if len(name) > 4 and name[-4] == "." and name[-3:].isdigit():
        return name[:-4]
    return name


def get_object_folder_path(obj):
    for col in obj.users_collection:
        path = []
        cur = col
        found_root = False
        while cur:
            if cur.name == COLLECTION_NAME:
                found_root = True
                break
            path.insert(0, _strip_blender_suffix(cur.name))
            parent = None
            for p in bpy.data.collections:
                if cur.name in p.children:
                    parent = p
                    break
            cur = parent
        if found_root:
            return path
    return []


def _leaf_collection_name(obj):
    cols = obj.users_collection
    return cols[0].name if cols else ""


def record_sync_state(rbx_id, obj, size=None, offset=None, is_model=False, matrix=None, ratio_size=False):
    """
    Remembers the object's world matrix right after it was synced, to detect user edits later.
    Pass `matrix` when it was just assigned (obj.matrix_world may not reflect loc/scale edits until depsgraph update).
    ratio_size: the visible mesh doesn't match Part.Size (SpecialMesh), so size follows scale changes instead.
    """
    m = matrix.copy() if matrix is not None else obj.matrix_world.copy()
    _sync_state[rbx_id] = {
        "matrix": m,
        "scale": m.to_scale(),
        "size": list(size) if size is not None else blender_dimensions_to_rbx_size(obj),
        "offset": offset,
        "is_model": is_model,
        "ratio_size": ratio_size,
        "collection": _leaf_collection_name(obj),
    }


def _matrices_differ(a, b, eps=1e-4):
    for i in range(4):
        for j in range(4):
            if abs(a[i][j] - b[i][j]) > eps:
                return True
    return False


def _object_to_rbx_part(obj, state):
    """
    Converts the object's current Blender transform back into the Roblox part's CFrame + Size,
    undoing SpecialMesh offset/scale and mesh native-size normalization.
    Returns (pos, rot, size_or_None).
    """
    mw = obj.matrix_world
    rot3 = mw.to_3x3()
    rot3.normalize()
    loc = mw.to_translation()
    offset = state.get("offset") if state else None
    if offset is not None:
        # Object origin = part origin + R @ offset (offset is in unscaled part-local studs)
        loc = loc - rot3 @ offset
    part_mat = rot3.to_4x4()
    part_mat.translation = loc
    pos, rot = blender_matrix_to_rbx_cframe(part_mat)

    if state and state.get("is_model"):
        return pos, rot, None

    if state and state.get("ratio_size"):
        # SpecialMesh: the mesh isn't the part box, so Size follows the scale change
        # (Blender axes -> Roblox axes: X->X, Z->Y, Y->Z)
        old_scale = state["scale"]
        new_scale = mw.to_scale()
        old_size = state["size"]
        fx = new_scale.x / old_scale.x if abs(old_scale.x) > 1e-9 else 1.0
        fy = new_scale.y / old_scale.y if abs(old_scale.y) > 1e-9 else 1.0
        fz = new_scale.z / old_scale.z if abs(old_scale.z) > 1e-9 else 1.0
        size = [max(0.05, old_size[0] * fx), max(0.05, old_size[1] * fz), max(0.05, old_size[2] * fy)]
    else:
        # Real object size: stays correct after "Apply Scale" (Ctrl+A) or editing the mesh,
        # where the scale ratio would shrink the part to a tiny cube
        size = blender_dimensions_to_rbx_size(obj)
    return pos, rot, size


def queue_outgoing_transform(obj, action="update"):
    rbx_id = obj.get("rbx_id")
    if not rbx_id:
        return
    state = _sync_state.get(rbx_id)
    pos, rot, size = _object_to_rbx_part(obj, state)
    shape = obj.get("rbx_shape", "Block")
    if shape == "Ball":
        shape = "Sphere"
    is_model = bool(state and state.get("is_model")) or bool(obj.get("is_compound_model"))

    existing_action = _outgoing_changes.get(rbx_id, {}).get("action")
    effective_action = "create" if existing_action == "create" else action

    if is_model:
        record_sync_state(rbx_id, obj, size=(state or {}).get("size", [1, 1, 1]), is_model=True)
        _outgoing_changes[rbx_id] = {
            "action": "update",
            "id": rbx_id,
            "is_model": True,
            "position": pos,
            "rotation": rot,
        }
        return

    change = {
        "action": effective_action,
        "id": rbx_id,
        "position": pos,
        "rotation": rot,
        "size": size,
    }
    if effective_action == "create":
        color = [0.8, 0.8, 0.8]
        if obj.material_slots and obj.material_slots[0].material:
            mat = obj.material_slots[0].material
            if mat.use_nodes and "Principled BSDF" in mat.node_tree.nodes:
                base_col = mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value
                color = [float(base_col[0]), float(base_col[1]), float(base_col[2])]
        change.update({
            "name": obj.name,
            "shape": shape,
            "color": color,
            "material": "SmoothPlastic",
            "folder_path": get_object_folder_path(obj),
        })
    elif state and state.get("collection") and state.get("collection") != _leaf_collection_name(obj):
        # Only re-parent in Studio when the user really moved the object to another collection.
        # (Blender collection names are globally unique, so names may differ from Studio folders.)
        change["folder_path"] = get_object_folder_path(obj)

    record_sync_state(rbx_id, obj, size=size, offset=(state or {}).get("offset"),
                      ratio_size=bool((state or {}).get("ratio_size")))
    _outgoing_changes[rbx_id] = change


def pop_outgoing_changes():
    """Called by the HTTP request handler (on Blender's main thread, see server.poll)."""
    if not _outgoing_changes:
        return []
    changes = list(_outgoing_changes.values())
    _outgoing_changes.clear()
    return changes


def create_primitive(shape_type="Block", location=None):
    init_shared_meshes()

    root_col = get_or_create_collection()
    active_col = bpy.context.collection

    def is_descendant_of_root(col):
        if col == root_col:
            return True
        for c in bpy.data.collections:
            if col.name in c.children:
                if is_descendant_of_root(c):
                    return True
        return False

    if active_col and is_descendant_of_root(active_col) and active_col != root_col:
        target_col = active_col
    else:
        target_col = get_or_create_collection_hierarchy(["Primitives"])

    mesh_key = (shape_type, None)
    if shape_type == "Ball":
        mesh_key = ("Sphere", None)
    base_m = _base_meshes.get(mesh_key)
    if not base_m:
        init_shared_meshes()
        base_m = _base_meshes.get(mesh_key) or _base_meshes.get(("Block", None))

    unique_id = str(uuid.uuid4())
    obj_name = f"Rbx_{shape_type}"
    obj = bpy.data.objects.new(obj_name, base_m)

    if location is None:
        loc = bpy.context.scene.cursor.location
    else:
        loc = Vector(location)

    obj.location = loc
    if shape_type == "Block":
        obj.scale = (4.0, 2.0, 2.0)
    elif shape_type in ("Sphere", "Ball"):
        obj.scale = (3.0, 3.0, 3.0)
    elif shape_type == "Cylinder":
        obj.scale = (4.0, 2.0, 2.0)
    elif shape_type == "Wedge":
        obj.scale = (4.0, 2.0, 2.0)
    else:
        obj.scale = (2.0, 2.0, 2.0)

    target_col.objects.link(obj)
    obj["rbx_id"] = unique_id
    obj["rbx_shape"] = "Ball" if shape_type == "Sphere" else shape_type

    default_mat = get_or_create_material(0.65, 0.65, 0.68, rbx_material="SmoothPlastic")
    if len(obj.material_slots) == 0:
        if len(obj.data.materials) == 0:
            obj.data.materials.append(None)
    if len(obj.material_slots) > 0:
        obj.material_slots[0].link = 'OBJECT'
        obj.material_slots[0].material = default_mat

    _id_to_obj[unique_id] = obj

    for o in bpy.context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()

    queue_outgoing_transform(obj, action="create")
    return obj


def apply_csg_boolean(obj, csg_components, shape="Block"):
    if not csg_components or not isinstance(csg_components, list):
        return
    neg_comps = [c for c in csg_components if c.get("isNegative")]
    if not neg_comps:
        return

    # Check cache by positions and sizes to avoid recalculating every frame
    cache_key = str([(c.get("position"), c.get("size")) for c in neg_comps])
    if obj.get("csg_cache_key") == cache_key:
        return

    # 1. Create unique mesh for obj based on shape
    mesh_name = f"RbxCSG_{obj.get('rbx_id', obj.name)}"
    mesh = bpy.data.meshes.new(mesh_name)
    bm = bmesh.new()
    if shape == "Cylinder":
        bmesh.ops.create_cone(
            bm,
            cap_ends=True,
            cap_tris=False,
            segments=20,
            radius1=0.5,
            radius2=0.5,
            depth=1.0,
            matrix=Matrix.Rotation(math.radians(90.0), 4, 'Y')
        )
    elif shape in ("Sphere", "Ball"):
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=14, radius=0.5)
    else:
        bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()

    old_mesh = obj.data
    obj.data = mesh

    # 2. For each negative cutter, apply boolean difference
    for idx, neg in enumerate(neg_comps):
        cutter_mesh = bpy.data.meshes.new(f"Temp_Cutter_{idx}")
        bm_c = bmesh.new()
        bmesh.ops.create_cube(bm_c, size=1.0)
        bm_c.to_mesh(cutter_mesh)
        bm_c.free()

        cutter = bpy.data.objects.new(f"Temp_Cutter_{idx}", cutter_mesh)
        c_pos = neg.get("position", [0, 0, 0])
        c_rot = neg.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
        c_size = neg.get("size", [1, 1, 1])

        cutter.matrix_world = rbx_cframe_to_blender_matrix(c_pos, c_rot)
        cutter.scale = (c_size[0], c_size[2], c_size[1])
        bpy.context.scene.collection.objects.link(cutter)

        mod = obj.modifiers.new(name=f"CSG_Diff_{idx}", type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.object = cutter
        for s_candidate in ('MANIFOLD', 'FLOAT', 'FAST', 'EXACT'):
            try:
                mod.solver = s_candidate
                break
            except Exception:
                continue

        try:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass
        finally:
            try:
                bpy.data.objects.remove(cutter, do_unlink=True)
                bpy.data.meshes.remove(cutter_mesh, do_unlink=True)
            except Exception:
                pass

    if old_mesh and old_mesh.name.startswith("RbxCSG_") and old_mesh != mesh:
        try:
            bpy.data.meshes.remove(old_mesh, do_unlink=True)
        except Exception:
            pass

    obj["csg_cache_key"] = cache_key


def apply_r15_clothing_uvs(obj, part_name):
    if not obj or not obj.data or not hasattr(obj.data, 'uv_layers') or len(obj.data.vertices) == 0:
        return

    clean_name = part_name.split('.')[0]
    limb_type = None
    sub_part = None
    if "UpperTorso" in clean_name:
        limb_type, sub_part = 'Torso', 'Upper'
    elif "LowerTorso" in clean_name:
        limb_type, sub_part = 'Torso', 'Lower'
    elif "RightUpperArm" in clean_name:
        limb_type, sub_part = 'RightArm', 'Upper'
    elif "RightLowerArm" in clean_name:
        limb_type, sub_part = 'RightArm', 'Lower'
    elif "RightHand" in clean_name:
        limb_type, sub_part = 'RightArm', 'Hand'
    elif "LeftUpperArm" in clean_name:
        limb_type, sub_part = 'LeftArm', 'Upper'
    elif "LeftLowerArm" in clean_name:
        limb_type, sub_part = 'LeftArm', 'Lower'
    elif "LeftHand" in clean_name:
        limb_type, sub_part = 'LeftArm', 'Hand'
    elif "RightUpperLeg" in clean_name:
        limb_type, sub_part = 'RightLeg', 'Upper'
    elif "RightLowerLeg" in clean_name:
        limb_type, sub_part = 'RightLeg', 'Lower'
    elif "RightFoot" in clean_name:
        limb_type, sub_part = 'RightLeg', 'Foot'
    elif "LeftUpperLeg" in clean_name:
        limb_type, sub_part = 'LeftLeg', 'Upper'
    elif "LeftLowerLeg" in clean_name:
        limb_type, sub_part = 'LeftLeg', 'Lower'
    elif "LeftFoot" in clean_name:
        limb_type, sub_part = 'LeftLeg', 'Foot'

    if not limb_type:
        return

    # Already remapped and still using its own copy -> nothing to do
    if obj.get("_r15_uv_mapped") == f"{limb_type}_{sub_part}" and obj.data.users == 1:
        return

    # Ensure this object has its own unique mesh copy so remapping doesn't affect shared meshes
    if obj.data.users > 1:
        obj.data = obj.data.copy()

    mesh = obj.data
    uv_layer = mesh.uv_layers.active.data if mesh.uv_layers.active else mesh.uv_layers.new(name="UVMap").data

    xs = [v.co.x for v in mesh.vertices]
    ys = [v.co.y for v in mesh.vertices]
    zs = [v.co.z for v in mesh.vertices]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    dx = max(0.001, max_x - min_x)
    dy = max(0.001, max_y - min_y)
    dz = max(0.001, max_z - min_z)

    W, H = 585.0, 559.0

    if limb_type == 'Torso':
        if sub_part == 'Upper':
            y_start, y_end = 74.0, 176.0
        else:
            y_start, y_end = 176.0, 201.0

        for poly in mesh.polygons:
            n = poly.normal
            if abs(n.y) >= abs(n.x) and abs(n.y) >= abs(n.z):
                if n.y > 0:  # Front
                    bx1, bx2 = 231.0, 358.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Back
                    bx1, bx2 = 423.0, 550.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.x - min_x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            elif abs(n.x) >= abs(n.y) and abs(n.x) >= abs(n.z):
                if n.x > 0:  # Left (+X)
                    bx1, bx2 = 359.0, 422.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.y - min_y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Right (-X)
                    bx1, bx2 = 165.0, 230.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_y - v.y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            else:
                if n.z > 0 and sub_part == 'Upper':  # Top
                    bx1, bx2, by1, by2 = 231.0, 358.0, 9.0, 73.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_y - v.y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                elif n.z < 0 and sub_part == 'Lower':  # Bottom
                    bx1, bx2, by1, by2 = 231.0, 358.0, 202.0, 266.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (v.y - min_y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)

    elif limb_type in ('RightArm', 'RightLeg'):
        if sub_part == 'Upper':
            y_start, y_end = 353.0, 420.0
        elif sub_part == 'Lower':
            y_start, y_end = 420.0, 470.0
        else:
            y_start, y_end = 470.0, 480.0

        for poly in mesh.polygons:
            n = poly.normal
            if abs(n.y) >= abs(n.x) and abs(n.y) >= abs(n.z):
                if n.y > 0:  # Front
                    bx1, bx2 = 219.0, 282.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Back
                    bx1, bx2 = 83.0, 146.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.x - min_x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            elif abs(n.x) >= abs(n.y) and abs(n.x) >= abs(n.z):
                if n.x > 0:  # Inner (+X)
                    bx1, bx2 = 19.0, 82.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.y - min_y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Outer (-X)
                    bx1, bx2 = 147.0, 218.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_y - v.y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            else:
                if n.z > 0 and sub_part == 'Upper':  # Top
                    bx1, bx2, by1, by2 = 219.0, 282.0, 289.0, 352.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_y - v.y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                elif n.z < 0 and sub_part in ('Hand', 'Foot'):  # Bottom
                    bx1, bx2, by1, by2 = 219.0, 282.0, 481.0, 549.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (v.y - min_y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)

    elif limb_type in ('LeftArm', 'LeftLeg'):
        if sub_part == 'Upper':
            y_start, y_end = 353.0, 420.0
        elif sub_part == 'Lower':
            y_start, y_end = 420.0, 470.0
        else:
            y_start, y_end = 470.0, 480.0

        for poly in mesh.polygons:
            n = poly.normal
            if abs(n.y) >= abs(n.x) and abs(n.y) >= abs(n.z):
                if n.y > 0:  # Front
                    bx1, bx2 = 307.0, 370.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Back
                    bx1, bx2 = 435.0, 498.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.x - min_x) / dx
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            elif abs(n.x) >= abs(n.y) and abs(n.x) >= abs(n.z):
                if n.x > 0:  # Outer (+X)
                    bx1, bx2 = 371.0, 434.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (v.y - min_y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                else:  # Inner (-X)
                    bx1, bx2 = 499.0, 570.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_y - v.y) / dy
                        v_frac = (max_z - v.z) / dz
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = y_start + v_frac * (y_end - y_start)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
            else:
                if n.z > 0 and sub_part == 'Upper':  # Top
                    bx1, bx2, by1, by2 = 307.0, 370.0, 289.0, 352.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (max_y - v.y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)
                elif n.z < 0 and sub_part in ('Hand', 'Foot'):  # Bottom
                    bx1, bx2, by1, by2 = 307.0, 370.0, 481.0, 549.0
                    for li in poly.loop_indices:
                        v = mesh.vertices[mesh.loops[li].vertex_index].co
                        u_frac = (max_x - v.x) / dx
                        v_frac = (v.y - min_y) / dy
                        px = bx1 + u_frac * (bx2 - bx1)
                        py = by1 + v_frac * (by2 - by1)
                        uv_layer[li].uv = (px / W, 1.0 - py / H)

    obj["_r15_uv_mapped"] = f"{limb_type}_{sub_part}"


def _schedule_retry(inst_id, inst_data, incomplete):
    """Remember objects built while textures/meshes were still downloading, so they get re-applied later."""
    if not incomplete:
        _retry_instances.pop(inst_id, None)
        return
    entry = _retry_instances.get(inst_id)
    attempts = entry[1] + 1 if entry else 0
    if attempts > _RETRY_MAX_ATTEMPTS:
        _retry_instances.pop(inst_id, None)
    else:
        _retry_instances[inst_id] = [inst_data, attempts]


def _begin_asset_tracking():
    global _assets_missing
    _assets_missing = False
    mesh_loader.missing_hit = False


def _assets_were_missing():
    return _assets_missing or mesh_loader.missing_hit


def _move_to_collection(obj, target_col):
    current_cols = list(obj.users_collection)
    if target_col not in current_cols:
        target_col.objects.link(obj)
    for c in current_cols:
        if c != target_col:
            try:
                c.objects.unlink(obj)
            except Exception:
                pass


def _get_live_object(inst_id):
    obj = _id_to_obj.get(inst_id)
    if obj is not None:
        try:
            _ = obj.name
        except ReferenceError:
            # Undo / file reload re-created the datablocks: look the object up again instead of
            # creating a duplicate next to the restored one
            rebuild_id_map()
            obj = _id_to_obj.get(inst_id)
    return obj


# ---------------------------------------------------------------------------
# Surface layers on block faces: Decals, Textures and SurfaceGuis (see surfaces.py)
# ---------------------------------------------------------------------------

STUDS_UV = "RbxStuds"
_DECAL_TYPES = ("Decal", "Texture", "SurfaceGui")
_face_mat_cache = {}


def _uses_surface_layers(shape, surfaces_list, mesh_id=None, mesh_scale=None):
    """Per-face layers are applied to box parts; other shapes keep the single texture_info path."""
    return bool(surfaces_list) and (shape or "Block") == "Block" and not mesh_id and not mesh_scale


def add_layered_box(bm, matrix, size, face_mat_idx, base_idx, uv_layer, studs_layer):
    """
    Adds a unit box transformed by `matrix` (part transform incl. scale). Every face gets its own vertices,
    decal UVs 0..1 oriented the way Roblox draws decals, and a second UV map measured in studs (tiled Textures).
    """
    for face, f in surfaces.FACES.items():
        n, r, u = Vector(f["n"]), Vector(f["right"]), Vector(f["up"])
        center = n * 0.5
        corners = [center + r * (sx * 0.5) + u * (sy * 0.5) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        bf = bm.faces.new([bm.verts.new(matrix @ c) for c in corners])
        bf.material_index = face_mat_idx.get(face, base_idx)
        fw, fh = surfaces.face_size_studs(face, size)
        for loop, (uu, vv) in zip(bf.loops, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))):
            loop[uv_layer].uv = (uu, vv)
            # Roblox tiles Textures from the top-left corner of the face
            loop[studs_layer].uv = (uu * fw, (vv - 1.0) * fh)


def _find_principled(nt):
    return next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def _mix_rgba(nt, a_socket, b_socket, factor_socket=None, factor=1.0, blend='MIX'):
    mix = nt.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.blend_type = blend
    if factor_socket is not None:
        nt.links.new(factor_socket, mix.inputs[0])
    else:
        mix.inputs[0].default_value = factor
    nt.links.new(a_socket, mix.inputs[6])
    if isinstance(b_socket, (tuple, list)):
        mix.inputs[7].default_value = tuple(b_socket)
    else:
        nt.links.new(b_socket, mix.inputs[7])
    return mix.outputs[2]  # Result (Color)


def _face_material(base_mat, tex_layers, overlay_path):
    """
    Material of one face: a copy of the part material with tiled Textures and the baked
    Decal/SurfaceGui overlay mixed into its Base Color. Returns (material or None, complete).
    """
    tex_files = []
    complete = True
    for layer in tex_layers:
        path = texture_file(layer.get("img"))
        if path:
            tex_files.append((layer, path))
        else:
            complete = False
    if not tex_files and not overlay_path:
        return None, complete

    key = surfaces.signature({
        "v": MATERIAL_VERSION,
        "base": base_mat.name,
        "tex": [(p, l.get("su"), l.get("sv"), l.get("ou"), l.get("ov"), l.get("c"), l.get("t")) for l, p in tex_files],
        "overlay": overlay_path,
    })
    cached = _face_mat_cache.get(key)
    if is_material_valid(cached):
        return cached, complete
    name = _fit_id_name(f"RbxFace_{key[:12]}")
    existing = bpy.data.materials.get(name)
    if is_material_valid(existing) and existing.get("rbx_face_key") == key:
        _face_mat_cache[key] = existing
        return existing, complete

    mat = base_mat.copy()
    mat.name = name
    mat["rbx_built"] = True
    mat["rbx_face_key"] = key
    nt = mat.node_tree
    bsdf = _find_principled(nt)
    if bsdf is None:
        return mat, complete
    base_link = next((l for l in nt.links if l.to_socket == bsdf.inputs["Base Color"]), None)
    if base_link:
        current = base_link.from_socket
        nt.links.remove(base_link)
    else:
        rgb = nt.nodes.new('ShaderNodeRGB')
        rgb.outputs[0].default_value = bsdf.inputs["Base Color"].default_value
        current = rgb.outputs[0]

    for layer, path in tex_files:
        img = load_cached_image(path, f"RbxTex_{os.path.splitext(os.path.basename(path))[0]}")
        if not img:
            continue
        su = max(0.01, float(layer.get("su") or 2.0))
        sv = max(0.01, float(layer.get("sv") or 2.0))
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = STUDS_UV
        mapping = nt.nodes.new('ShaderNodeMapping')
        mapping.inputs['Scale'].default_value = (1.0 / su, 1.0 / sv, 1.0)
        mapping.inputs['Location'].default_value = (float(layer.get("ou") or 0) / su, -float(layer.get("ov") or 0) / sv, 0.0)
        nt.links.new(uv.outputs['UV'], mapping.inputs['Vector'])
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = img
        tex.extension = 'REPEAT'
        nt.links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
        c = layer.get("c") or [1, 1, 1]
        tinted = _mix_rgba(nt, tex.outputs['Color'], (c[0], c[1], c[2], 1.0), blend='MULTIPLY')
        alpha = nt.nodes.new('ShaderNodeMath')
        alpha.operation = 'MULTIPLY'
        nt.links.new(tex.outputs['Alpha'], alpha.inputs[0])
        alpha.inputs[1].default_value = 1.0 - float(layer.get("t") or 0)
        current = _mix_rgba(nt, current, tinted, factor_socket=alpha.outputs[0])

    if overlay_path:
        img = load_cached_image(overlay_path, os.path.splitext(os.path.basename(overlay_path))[0])
        if img:
            uv = nt.nodes.new('ShaderNodeUVMap')
            uv.uv_map = "UVMap"
            tex = nt.nodes.new('ShaderNodeTexImage')
            tex.image = img
            tex.extension = 'EXTEND'
            tex.interpolation = 'Cubic'
            nt.links.new(uv.outputs['UV'], tex.inputs['Vector'])
            current = _mix_rgba(nt, current, tex.outputs['Color'], factor_socket=tex.outputs['Alpha'])
            base_alpha = bsdf.inputs["Alpha"].default_value
            if base_alpha < 1.0 and not bsdf.inputs["Alpha"].is_linked:
                # Decals and SurfaceGuis stay visible on transparent parts
                mx = nt.nodes.new('ShaderNodeMath')
                mx.operation = 'MAXIMUM'
                mx.inputs[0].default_value = base_alpha
                nt.links.new(tex.outputs['Alpha'], mx.inputs[1])
                nt.links.new(mx.outputs[0], bsdf.inputs["Alpha"])

    nt.links.new(current, bsdf.inputs["Base Color"])
    _face_mat_cache[key] = mat
    return mat, complete


def _layered_face_materials(base_mat, surfaces_list, size):
    """face name -> material for faces that carry layers. Flags _assets_missing while images download."""
    global _assets_missing
    face_mats = {}
    if base_mat is None:
        return face_mats
    for face, spec in surfaces.split_layers(surfaces_list).items():
        fw, fh = surfaces.face_size_studs(face, size)
        try:
            overlay_path, ok = surfaces.bake_face_overlay(spec["overlay"], fw, fh, texture_file, FACE_TEX_DIR)
        except Exception as e:
            print(f"[Roblox Live Link] Baking {face} face failed: {e}")
            overlay_path, ok = None, True
        mat, ok_tex = _face_material(base_mat, spec["textures"], overlay_path)
        if not (ok and ok_tex):
            _assets_missing = True
        if mat is not None:
            face_mats[face] = mat
    return face_mats


def _is_unit_box(mesh):
    """False when the box was edited in Blender (e.g. scale applied into the mesh): then it is rebuilt."""
    verts = mesh.vertices
    if len(verts) != 24:
        return False
    extent = max(abs(c) for v in verts for c in v.co)
    return 0.499 < extent < 0.501


def _layered_mesh(inst_id, base_mat, face_mats, size):
    """Per-object box mesh with per-face materials/UVs; rebuilt only when size, layers or materials change."""
    sig = surfaces.signature({
        "size": [round(float(v), 3) for v in size],
        "base": base_mat.name if base_mat else "",
        "faces": sorted((f, m.name) for f, m in face_mats.items()),
    })
    name = _fit_id_name(f"RbxLayered_{inst_id}")
    mesh = bpy.data.meshes.get(name)
    if mesh is not None and mesh.get("rbx_layer_sig") == sig and _is_unit_box(mesh):
        return mesh
    if mesh is None:
        mesh = bpy.data.meshes.new(name)
    materials = [base_mat]
    for f in surfaces.FACES:
        if f in face_mats and face_mats[f] not in materials:
            materials.append(face_mats[f])
    face_idx = {f: materials.index(face_mats[f]) for f in face_mats}
    # Materials first: materials.clear() resets the material index of existing faces
    mesh.materials.clear()
    for m in materials:
        mesh.materials.append(m)
    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new("UVMap")
    studs_layer = bm.loops.layers.uv.new(STUDS_UV)
    add_layered_box(bm, Matrix.Identity(4), size, face_idx, 0, uv_layer, studs_layer)
    bm.to_mesh(mesh)
    bm.free()
    mesh["rbx_layer_sig"] = sig
    return mesh


def update_or_create_instance(inst_data, col):
    inst_id = inst_data.get("id")
    if not inst_id:
        return
    _begin_asset_tracking()

    name = inst_data.get("name", "RbxPart")
    shape = inst_data.get("shape", "Block")
    pos = inst_data.get("position", [0, 0, 0])
    rot = inst_data.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
    size = inst_data.get("size", [2, 1, 4])
    color = inst_data.get("color", [0.7, 0.7, 0.7])
    transparency = inst_data.get("transparency", 0.0)
    rbx_material = inst_data.get("material", "Plastic")
    reflectance = inst_data.get("reflectance", 0.0)
    texture_info = inst_data.get("texture_info")
    try:
        pos = [float(v) for v in pos]
        rot = [float(v) for v in rot]
        size = [float(v) for v in size]
        color = [float(v) for v in color]
        transparency = float(transparency)
        if len(pos) != 3 or len(rot) != 9 or len(size) != 3 or len(color) < 3:
            raise ValueError("wrong vector length")
    except (TypeError, ValueError) as e:
        # Validate before creating anything, otherwise a half-built object is left at the origin
        print(f"[Roblox Live Link] Skipped {name}: invalid transform/color data ({e})")
        return
    if texture_info is not None and not isinstance(texture_info, dict):
        texture_info = None

    is_csg = inst_data.get("isOperation", False) or (inst_data.get("className") in ("IntersectOperation", "UnionOperation", "NegateOperation"))
    mesh_id = inst_data.get("mesh_id")
    # mesh_scale / mesh_offset are only sent for parts with a SpecialMesh
    mesh_scale = inst_data.get("mesh_scale")
    mesh_offset = inst_data.get("mesh_offset")
    fallback_mesh = inst_data.get("fallback_mesh")

    # Box parts with Decals / Textures / SurfaceGuis get per-face materials (all layers, every face)
    surfaces_list = inst_data.get("surfaces")
    layered = not is_csg and _uses_surface_layers(shape, surfaces_list, mesh_id, mesh_scale)
    if layered and texture_info and texture_info.get("type") in _DECAL_TYPES:
        texture_info = None  # drawn per face instead of over the whole part

    target_face = None
    if texture_info and texture_info.get("type") in ("Decal", "SurfaceGui"):
        target_face = texture_info.get("face", "Front")
    folder_path = inst_data.get("folder_path")
    if is_csg and not folder_path:
        target_col = get_or_create_csg_collection()
    else:
        target_col = get_or_create_collection_hierarchy(folder_path)

    obj = _get_live_object(inst_id)

    mat = get_or_create_material(
        color[0], color[1], color[2],
        transparency=transparency,
        rbx_material=rbx_material,
        reflectance=reflectance,
        texture_info=texture_info,
        material_variant=inst_data.get("materialVariant")
    )

    expected_mesh = None
    if mesh_id:
        expected_mesh = mesh_loader.get_or_load_mesh(mesh_id, fallback_name=fallback_mesh)
    is_file_mesh = expected_mesh is not None

    if layered:
        face_mats = _layered_face_materials(mat, surfaces_list, size)
        expected_mesh = _layered_mesh(inst_id, mat, face_mats, size)
    elif not expected_mesh:
        expected_mesh = get_shared_mesh(shape, target_face)

    if obj is None:
        obj = bpy.data.objects.new(name, expected_mesh)
        obj["rbx_id"] = inst_id
        if is_csg:
            obj["is_csg"] = True
        target_col.objects.link(obj)
        _id_to_obj[inst_id] = obj
    else:
        if obj.get("rbx_name") != name:
            obj.name = name
        _move_to_collection(obj, target_col)
        # Swap mesh only when the *source* mesh changed (keeps per-object copies, e.g. R15 clothing UVs)
        # Swap when the source mesh changed, or when the object got another mesh in Blender
        # (e.g. "Apply Scale" on a shared mesh makes a single-user copy with the scale baked in).
        # Per-object R15 clothing copies are kept.
        r15_copy = bool(obj.get("_r15_uv_mapped")) and obj.get("rbx_src_mesh") == expected_mesh.name
        if obj.data != expected_mesh and not obj.get("csg_cache_key") and not r15_copy:
            old_mesh = obj.data
            obj.data = expected_mesh
            if "_r15_uv_mapped" in obj:
                del obj["_r15_uv_mapped"]
            if old_mesh and old_mesh.users == 0 and old_mesh.name.startswith("Rbx"):
                bpy.data.meshes.remove(old_mesh)

    obj["rbx_name"] = name
    obj["rbx_shape"] = shape
    obj["rbx_src_mesh"] = expected_mesh.name
    if mesh_id:
        obj["rbx_mesh_id"] = str(mesh_id)

    if layered:
        # Own mesh with per-face materials: slots follow the mesh
        for slot in obj.material_slots:
            if slot.link != 'DATA':
                slot.link = 'DATA'
    else:
        # Material slot linked to the object, so shared meshes are never modified per part
        if len(obj.data.materials) == 0:
            obj.data.materials.append(None)
        if len(obj.material_slots) > 0 and obj.material_slots[0].link != 'OBJECT':
            obj.material_slots[0].link = 'OBJECT'

    global _is_applying_roblox_update
    _is_applying_roblox_update = True
    try:
        part_matrix = rbx_cframe_to_blender_matrix(pos, rot)
        if is_file_mesh and mesh_scale and len(mesh_scale) == 3:
            # SpecialMesh FileMesh renders at native mesh size * Scale, independent of Part.Size
            sx, sy, sz = float(mesh_scale[0]), float(mesh_scale[2]), float(mesh_scale[1])
        else:
            native_size = (1.0, 1.0, 1.0)
            if is_file_mesh and "rbx_native_size" in expected_mesh:
                native_size = expected_mesh["rbx_native_size"]
            sx = size[0] / native_size[0]
            sy = size[2] / native_size[2]
            sz = size[1] / native_size[1]
            if mesh_scale and len(mesh_scale) == 3:
                # SpecialMesh Brick/Cylinder/Sphere types scale relative to Part.Size
                sx *= float(mesh_scale[0])
                sy *= float(mesh_scale[2])
                sz *= float(mesh_scale[1])

        local_off = None
        final_matrix = part_matrix.copy()
        if mesh_offset and len(mesh_offset) == 3 and any(abs(float(v)) > 1e-6 for v in mesh_offset):
            # SpecialMesh.Offset is in part-local studs (rotation only, not scaled)
            local_off = Vector((float(mesh_offset[0]), -float(mesh_offset[2]), float(mesh_offset[1])))
            final_matrix.translation = part_matrix.translation + part_matrix.to_3x3() @ local_off
        final_matrix = final_matrix @ Matrix.Diagonal((sx, sy, sz, 1.0))
        obj.matrix_world = final_matrix
        record_sync_state(inst_id, obj, size=size, offset=local_off, matrix=final_matrix,
                          ratio_size=bool(mesh_scale and len(mesh_scale) == 3))
    finally:
        _is_applying_roblox_update = False

    csg_components = inst_data.get("csg_components")
    if is_csg and csg_components:
        apply_csg_boolean(obj, csg_components, shape)

    if not layered and mat and len(obj.material_slots) > 0 and obj.material_slots[0].material != mat:
        obj.material_slots[0].material = mat

    # Check if this object is an R15 character limb with clothing (Shirt or Pants)
    p_name = inst_data.get("name", obj.name)
    tex_type = texture_info.get("type", "") if texture_info else ""
    if not layered and (tex_type in ("Shirt", "Pants") or any(k in p_name for k in ("Torso", "Arm", "Leg", "Hand", "Foot"))):
        apply_r15_clothing_uvs(obj, p_name)

    _schedule_retry(inst_id, inst_data, _assets_were_missing())


def add_wedge_geometry(bm, matrix, uv_layer, mat_idx):
    raw_v = [
        Vector((-0.5,  0.5, -0.5)),
        Vector(( 0.5,  0.5, -0.5)),
        Vector(( 0.5, -0.5, -0.5)),
        Vector((-0.5, -0.5, -0.5)),
        Vector((-0.5, -0.5,  0.5)),
        Vector(( 0.5, -0.5,  0.5)),
    ]
    verts = [bm.verts.new(matrix @ v) for v in raw_v]
    faces = [
        bm.faces.new((verts[0], verts[1], verts[2], verts[3])),
        bm.faces.new((verts[2], verts[5], verts[4], verts[3])),
        bm.faces.new((verts[0], verts[4], verts[5], verts[1])),
        bm.faces.new((verts[0], verts[3], verts[4])),
        bm.faces.new((verts[1], verts[5], verts[2])),
    ]
    for f in faces:
        f.material_index = mat_idx
        for loop in f.loops:
            loop[uv_layer].uv = (loop.vert.co.x + 0.5, loop.vert.co.y + 0.5)
    return faces


def append_mesh_to_bmesh(bm, src_mesh, matrix, mat_idx, uv_layer):
    src_uvs = src_mesh.uv_layers.active.data if (src_mesh.uv_layers and src_mesh.uv_layers.active) else None
    new_verts = [bm.verts.new(matrix @ v.co) for v in src_mesh.vertices]
    bm.verts.ensure_lookup_table()
    for poly in src_mesh.polygons:
        p_verts = [new_verts[vi] for vi in poly.vertices]
        try:
            face = bm.faces.new(p_verts)
            face.material_index = mat_idx
            if src_uvs:
                for i, loop in enumerate(face.loops):
                    uv = src_uvs[poly.loop_start + i].uv
                    loop[uv_layer].uv = uv
        except (ValueError, IndexError):
            pass
    bm.faces.ensure_lookup_table()


def _compound_parts_hash(parts):
    try:
        # MATERIAL_VERSION: models saved with older materials get rebuilt
        return hashlib.md5(json.dumps([MATERIAL_VERSION, parts], sort_keys=True, default=str).encode("utf-8")).hexdigest()
    except Exception:
        return str(len(parts))


def update_or_create_compound_model(model_data, col):
    inst_id = model_data.get("id")
    if not inst_id:
        return
    _begin_asset_tracking()

    name = model_data.get("name", "CompoundModel")
    folder_path = model_data.get("folder_path")
    target_col = get_or_create_collection_hierarchy(folder_path)

    pos = model_data.get("position", [0, 0, 0])
    rot = model_data.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
    parts = model_data.get("parts", [])

    obj = _get_live_object(inst_id)

    # Rebuild geometry whenever any part changed (relative transform, color, material, mesh...),
    # not only when the number of parts changed. Moving the whole model only updates the matrix.
    parts_hash = _compound_parts_hash(parts)
    needs_rebuild = (obj is None) or (obj.get("parts_hash") != parts_hash)
    incomplete = False

    if needs_rebuild:
        bm = bmesh.new()
        # Explicit name: face materials sample "UVMap" (verify() names the layer differently in Blender 5)
        uv_layer = bm.loops.layers.uv.new("UVMap")
        studs_layer = bm.loops.layers.uv.new(STUDS_UV)
        mat_slots = []
        mat_to_idx = {}

        def slot_index(m):
            m_name = m.name if m else ""
            if m_name not in mat_to_idx:
                mat_to_idx[m_name] = len(mat_slots)
                mat_slots.append(m)
            return mat_to_idx[m_name]

        for p in parts:
            p_trans = p.get("transparency", 0.0)
            if p_trans >= 0.999 and not p.get("texture_info") and not p.get("surfaces"):
                continue  # fully invisible parts (hitboxes, root parts) add nothing visible

            p_shape = p.get("shape", "Block")
            p_rel_pos = p.get("rel_pos", [0, 0, 0])
            p_rel_rot = p.get("rel_rot", [1, 0, 0, 0, 1, 0, 0, 0, 1])
            p_size = p.get("size", [1, 1, 1])
            p_col = p.get("color", [0.7, 0.7, 0.7])
            p_mat = p.get("material", "Plastic")
            p_refl = p.get("reflectance", 0.0)
            p_tex = p.get("texture_info")
            p_mesh_id = p.get("mesh_id")
            p_mesh_scale = p.get("mesh_scale")
            p_mesh_offset = p.get("mesh_offset")
            p_surfaces = p.get("surfaces")
            p_layered = _uses_surface_layers(p.get("shape", "Block"), p_surfaces, p_mesh_id, p_mesh_scale)
            if p_layered and isinstance(p_tex, dict) and p_tex.get("type") in _DECAL_TYPES:
                p_tex = None  # drawn per face

            mat = get_or_create_material(
                p_col[0], p_col[1], p_col[2],
                transparency=p_trans,
                rbx_material=p_mat,
                reflectance=p_refl,
                texture_info=p_tex,
                material_variant=p.get("materialVariant")
            )
            mat_idx = slot_index(mat)

            p_matrix = rbx_cframe_to_blender_matrix(p_rel_pos, p_rel_rot)
            sx, sy, sz = p_size[0], p_size[2], p_size[1]
            s_mat = Matrix.Diagonal((sx, sy, sz, 1.0))
            part_mat = p_matrix @ s_mat

            if p_layered:
                face_mats = _layered_face_materials(mat, p_surfaces, p_size)
                face_idx = {f: slot_index(m) for f, m in face_mats.items()}
                add_layered_box(bm, part_mat, p_size, face_idx, mat_idx, uv_layer, studs_layer)
                continue

            # MeshPart or Part with SpecialMesh FileMesh: real geometry first, primitive as fallback
            if p_mesh_id:
                sub_m = mesh_loader.get_or_load_mesh(p_mesh_id, fallback_name=p.get("fallback_mesh"))
                if sub_m:
                    native_size = sub_m.get("rbx_native_size", (1.0, 1.0, 1.0))
                    if p_mesh_scale and len(p_mesh_scale) == 3:
                        off_mat = Matrix.Identity(4)
                        if p_mesh_offset and len(p_mesh_offset) == 3:
                            off_mat = Matrix.Translation(Vector((float(p_mesh_offset[0]), -float(p_mesh_offset[2]), float(p_mesh_offset[1]))))
                        mesh_mat = p_matrix @ off_mat @ Matrix.Diagonal((float(p_mesh_scale[0]), float(p_mesh_scale[2]), float(p_mesh_scale[1]), 1.0))
                    else:
                        s_comp = Matrix.Diagonal((1.0 / native_size[0], 1.0 / native_size[2], 1.0 / native_size[1], 1.0))
                        mesh_mat = part_mat @ s_comp
                    append_mesh_to_bmesh(bm, sub_m, mesh_mat, mat_idx, uv_layer)
                    continue

            if p_shape == "Wedge":
                add_wedge_geometry(bm, part_mat, uv_layer, mat_idx)

            elif p_shape == "Cylinder":
                rot_y = Matrix.Rotation(math.radians(90.0), 4, 'Y')
                res = bmesh.ops.create_cone(
                    bm, segments=16, cap_ends=True, cap_tris=False,
                    radius1=0.5, radius2=0.5, depth=1.0, matrix=part_mat @ rot_y
                )
                for v in res["verts"]:
                    for f in v.link_faces:
                        f.material_index = mat_idx
                        for loop in f.loops:
                            loop[uv_layer].uv = (loop.vert.co.x + 0.5, loop.vert.co.z + 0.5)

            elif p_shape in ("Ball", "Sphere"):
                res = bmesh.ops.create_uvsphere(
                    bm, u_segments=16, v_segments=12, radius=0.5, matrix=part_mat
                )
                for v in res["verts"]:
                    for f in v.link_faces:
                        f.material_index = mat_idx

            else:
                # Block, MeshPart without a loaded mesh, CornerWedge, Truss... -> box
                res = bmesh.ops.create_cube(bm, size=1.0, matrix=part_mat)
                for v in res["verts"]:
                    for f in v.link_faces:
                        f.material_index = mat_idx
                        for loop in f.loops:
                            loop[uv_layer].uv = (loop.vert.co.x + 0.5, loop.vert.co.y + 0.5)

        new_mesh = bpy.data.meshes.new(f"RbxCompound_{name}")
        bm.to_mesh(new_mesh)
        bm.free()

        for m in mat_slots:
            new_mesh.materials.append(m)

        if obj is None:
            obj = bpy.data.objects.new(name, new_mesh)
            obj["rbx_id"] = inst_id
            obj["is_compound_model"] = True
            target_col.objects.link(obj)
            _id_to_obj[inst_id] = obj
        else:
            old_mesh = obj.data
            obj.data = new_mesh
            if old_mesh and old_mesh.name.startswith("RbxCompound_") and old_mesh.users == 0:
                try:
                    bpy.data.meshes.remove(old_mesh, do_unlink=True)
                except Exception:
                    pass

        incomplete = _assets_were_missing()
        obj["parts_count"] = len(parts)
        # Incomplete builds keep an empty hash so the retry rebuilds them once downloads finish
        obj["parts_hash"] = "" if incomplete else parts_hash

    if obj.get("rbx_name") != name:
        obj.name = name
    obj["rbx_name"] = name
    _move_to_collection(obj, target_col)

    global _is_applying_roblox_update
    _is_applying_roblox_update = True
    try:
        model_matrix = rbx_cframe_to_blender_matrix(pos, rot)
        obj.matrix_world = model_matrix
        record_sync_state(inst_id, obj, size=[1, 1, 1], is_model=True, matrix=model_matrix)
    finally:
        _is_applying_roblox_update = False

    _schedule_retry(inst_id, model_data, incomplete)


def _remove_object(obj):
    old_mesh = obj.data
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass
    try:
        if old_mesh and old_mesh.users == 0 and old_mesh.name.startswith(("RbxCSG_", "RbxCompound_", "RbxLayered_")):
            bpy.data.meshes.remove(old_mesh, do_unlink=True)
    except ReferenceError:
        pass


def remove_instances(id_list):
    id_set = set(id_list)
    missing = False
    for inst_id in id_set:
        _sync_state.pop(inst_id, None)
        _retry_instances.pop(inst_id, None)
        obj = _id_to_obj.pop(inst_id, None)
        if obj is None:
            missing = True
            continue
        try:
            _ = obj.name
        except ReferenceError:
            missing = True
            continue
        _remove_object(obj)

    # Slow path (full collection scan) only if some ids weren't in the map (e.g. duplicates after file reload)
    if missing and COLLECTION_NAME in bpy.data.collections:
        col = bpy.data.collections[COLLECTION_NAME]
        for obj in list(col.all_objects):
            if obj.get("rbx_id") in id_set:
                _remove_object(obj)


def clear_all_linked_objects():
    global _sync_in_progress, _sync_total, _sync_processed, _full_sync_ids
    _sync_in_progress = False
    _sync_total = 0
    _sync_processed = 0
    _pending_instances.clear()
    _pending_stale_ids.clear()
    _path_to_collection.clear()
    _sync_state.clear()
    _retry_instances.clear()
    _pending_mats.clear()
    _full_sync_ids = None
    _outgoing_changes.clear()
    if COLLECTION_NAME in bpy.data.collections:
        col = bpy.data.collections[COLLECTION_NAME]
        all_objs = list(col.all_objects)
        if hasattr(bpy.data, "batch_remove") and all_objs:
            try:
                bpy.data.batch_remove(all_objs)
            except Exception:
                for obj in all_objs:
                    try:
                        bpy.data.objects.remove(obj, do_unlink=True)
                    except Exception:
                        pass
        else:
            for obj in all_objs:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass

        def _remove_subcollections(parent_c):
            for child in list(parent_c.children):
                _remove_subcollections(child)
                try:
                    bpy.data.collections.remove(child)
                except Exception:
                    pass
        _remove_subcollections(col)

    _id_to_obj.clear()
    # Only meshes nothing uses anymore: user objects outside Roblox_LiveLink that reuse
    # an Rbx mesh (e.g. a duplicated part moved to another collection) must keep their data.
    rbx_meshes = [m for m in bpy.data.meshes
                  if m.name.startswith("Rbx") and m.users - (1 if m.use_fake_user else 0) <= 0]
    if hasattr(bpy.data, "batch_remove") and rbx_meshes:
        try:
            bpy.data.batch_remove(rbx_meshes)
        except Exception:
            for mesh in rbx_meshes:
                try:
                    bpy.data.meshes.remove(mesh, do_unlink=True)
                except Exception:
                    pass
    else:
        for mesh in rbx_meshes:
            try:
                bpy.data.meshes.remove(mesh, do_unlink=True)
            except Exception:
                pass
    _base_meshes.clear()

    _mat_cache.clear()
    rbx_mats = [mat for mat in bpy.data.materials if mat.name.startswith("Rbx_") and mat.users == 0]
    if hasattr(bpy.data, "batch_remove") and rbx_mats:
        try:
            bpy.data.batch_remove(rbx_mats)
        except Exception:
            pass
    else:
        for mat in rbx_mats:
            try:
                bpy.data.materials.remove(mat)
            except Exception:
                pass


def apply_roblox_color_management(scene):
    """Roblox shows part colors close to plain sRGB; Blender's default AgX desaturates and brightens them."""
    try:
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
    except (TypeError, AttributeError):
        pass


def set_viewport_material_shading():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.type = 'MATERIAL'
    except Exception:
        pass


def _detach_duplicate(obj):
    """A Blender copy (Shift+D) of a synced object: turn it into a plain object instead of driving the original part."""
    for key in ("rbx_id", "rbx_src_mesh", "parts_hash", "is_compound_model"):
        if key in obj:
            del obj[key]


def _handle_full_sync(item):
    global _sync_total, _sync_processed, _sync_in_progress, _full_sync_ids, _full_sync_prune
    instances = item.get("instances", [])
    total_from_header = item.get("total_scene_objects")
    _sync_total = total_from_header if total_from_header else len(instances)
    _sync_processed = 0
    _sync_in_progress = True

    rebuild_id_map()
    _pending_stale_ids.clear()
    _pending_instances.clear()
    _pending_instances.extend(instances)

    _full_sync_prune = item.get("prune", True) is not False
    _full_sync_ids = {inst["id"] for inst in instances if "id" in inst}
    total_chunks = item.get("total_chunks") or 1
    if total_chunks <= 1:
        _finish_full_sync()


def _finish_full_sync():
    """Removes Blender objects that Studio no longer has (after the last chunk of a full sync)."""
    global _full_sync_ids
    if _full_sync_ids is None:
        return
    if _full_sync_prune:
        stale_ids = [cid for cid in list(_id_to_obj.keys()) if cid not in _full_sync_ids]
        _pending_stale_ids.extend(stale_ids)
    _full_sync_ids = None


def _handle_diff_update(item):
    global _sync_total, _sync_in_progress
    if item.get("total_scene_objects") and not _sync_in_progress:
        _sync_total = item.get("total_scene_objects")
        _sync_in_progress = True
    remove_ids = item.get("remove_ids", [])
    if remove_ids:
        if len(remove_ids) <= 200:
            remove_instances(remove_ids)
        else:
            _pending_stale_ids.extend(remove_ids)
    diff_instances = item.get("instances", [])
    if diff_instances:
        _pending_instances.extend(diff_instances)

    # Follow-up chunk of a chunked full sync
    chunk_index = item.get("chunk_index")
    if chunk_index and _full_sync_ids is not None:
        _full_sync_ids.update(inst["id"] for inst in diff_instances if "id" in inst)
        if chunk_index >= (item.get("total_chunks") or 0):
            _finish_full_sync()


def _requeue_retries():
    """Re-applies objects that were built while their textures/meshes were still downloading."""
    global _last_retry_time
    if not _retry_instances:
        return
    now = time.perf_counter()
    if now - _last_retry_time < _RETRY_INTERVAL:
        return
    if downloads.busy():
        return  # wait until all downloads finish
    _last_retry_time = now
    queued_ids = {inst.get("id") for inst in _pending_instances}
    for inst_id, (inst_data, _attempts) in list(_retry_instances.items()):
        if inst_id not in queued_ids:
            _pending_instances.append(inst_data)


def _check_outgoing_edits():
    """Bidirectional sync: send transforms of synced objects the user moved/rotated/scaled in Blender."""
    sel_objs = bpy.context.selected_objects
    if not sel_objs:
        return
    for obj in sel_objs:
        rbx_id = obj.get("rbx_id")
        if not rbx_id:
            continue
        owner = _id_to_obj.get(rbx_id)
        if owner is not None and owner != obj:
            try:
                _ = owner.name
                _detach_duplicate(obj)
                continue
            except ReferenceError:
                _id_to_obj[rbx_id] = obj
        state = _sync_state.get(rbx_id)
        if state is None:
            # Unknown baseline (e.g. after reopening the .blend): remember it, don't send anything yet
            record_sync_state(rbx_id, obj, is_model=bool(obj.get("is_compound_model")))
            continue
        if _matrices_differ(obj.matrix_world, state["matrix"]) or state.get("collection") != _leaf_collection_name(obj):
            queue_outgoing_transform(obj, action="update")


def process_queue_tick():
    global _sync_processed, _sync_in_progress
    if not _is_timer_running:
        return None

    is_anim_playing = False
    try:
        # Single-threaded: accept waiting HTTP requests and collect finished downloads here
        server.poll()
        downloads.poll()
        while server.sync_queue:
            item = server.sync_queue.popleft()
            if not isinstance(item, dict):
                continue

            event = item.get("event")
            if event == "clear_all":
                clear_all_linked_objects()
            elif event == "remove":
                ids = item.get("ids", [])
                if len(ids) <= 200:
                    remove_instances(ids)
                else:
                    _pending_stale_ids.extend(ids)
            elif event == "full_sync":
                _handle_full_sync(item)
            elif event == "diff_update":
                _handle_diff_update(item)

        start_time = time.perf_counter()
        max_duration = 0.015

        if _pending_stale_ids and (time.perf_counter() - start_time < max_duration):
            stale_chunk = []
            while _pending_stale_ids and len(stale_chunk) < 50 and (time.perf_counter() - start_time < max_duration):
                stale_chunk.append(_pending_stale_ids.popleft())
            if stale_chunk:
                remove_instances(stale_chunk)

        _requeue_retries()

        if _pending_instances and (time.perf_counter() - start_time < max_duration):
            col = get_or_create_collection()
            while _pending_instances and (time.perf_counter() - start_time < max_duration):
                inst = _pending_instances.popleft()
                try:
                    if inst.get("is_compound"):
                        update_or_create_compound_model(inst, col)
                    else:
                        update_or_create_instance(inst, col)
                except Exception as e:
                    print(f"[Roblox Live Link] Error updating instance {inst.get('name')}: {e}")
                if _sync_in_progress:
                    _sync_processed += 1

        if not _pending_instances and not _pending_stale_ids:
            if _sync_in_progress and (_sync_processed >= _sync_total or _sync_total == 0):
                _sync_in_progress = False
                _sync_processed = _sync_total

        try:
            is_anim_playing = any(w.screen.is_animation_playing for w in bpy.context.window_manager.windows if w.screen)
        except Exception:
            pass

        if not _is_applying_roblox_update and not is_anim_playing:
            try:
                _check_outgoing_edits()
            except Exception as e:
                print(f"[Roblox Live Link] Error checking Blender edits: {e}")

    except Exception as e:
        print(f"[Roblox Live Link] Error in process_queue_tick: {e}")

    # If animation is playing, yield more frame time to viewport playback
    if is_anim_playing:
        return 0.100

    return 0.050


@persistent
def _on_undo_redo(*_args):
    """Undo/redo re-creates Blender datablocks, so every Python reference in _id_to_obj goes stale."""
    rebuild_id_map()


@persistent
def _on_load_post(*_args):
    """Another .blend was opened: forget the old scene and ask Studio to re-send everything."""
    global _full_sync_ids
    rebuild_id_map()
    _sync_state.clear()
    _retry_instances.clear()
    _pending_instances.clear()
    _pending_stale_ids.clear()
    _pending_mats.clear()
    _full_sync_ids = None
    _outgoing_changes.clear()
    server.new_session()


_HANDLERS = (
    (bpy.app.handlers.undo_post, _on_undo_redo),
    (bpy.app.handlers.redo_post, _on_undo_redo),
    (bpy.app.handlers.load_post, _on_load_post),
)


def _unregister_handlers():
    # Match by name: after "Reload Addon" the handler is a new function object
    names = {fn.__name__ for _, fn in _HANDLERS}
    for handler_list, _ in _HANDLERS:
        for h in [h for h in handler_list if getattr(h, "__name__", "") in names and getattr(h, "__module__", "") == __name__]:
            handler_list.remove(h)


def _register_handlers():
    _unregister_handlers()
    for handler_list, fn in _HANDLERS:
        handler_list.append(fn)


def start_sync_timer():
    global _is_timer_running
    _is_timer_running = True
    rebuild_id_map()
    set_viewport_material_shading()
    _register_handlers()
    if not bpy.app.timers.is_registered(process_queue_tick):
        bpy.app.timers.register(process_queue_tick, persistent=True)


def stop_sync_timer():
    global _is_timer_running
    _is_timer_running = False
    _unregister_handlers()
    if bpy.app.timers.is_registered(process_queue_tick):
        try:
            bpy.app.timers.unregister(process_queue_tick)
        except Exception:
            pass
    downloads.cancel_all()
