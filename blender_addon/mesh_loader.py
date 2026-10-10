"""
Roblox Mesh Loader for Blender Live Link
Supports:
- Roblox .mesh format version 1.00 / 1.01 (ASCII)
- Roblox .mesh format version 2.00 (Binary)
- Roblox .mesh format version 3.00 / 3.01 (Binary + LODs)
- Roblox .mesh format version 4.00 / 4.01 / 5.00 (Binary + LODs + skinning)
- Roblox .mesh format version 6.00 / 7.00 (chunked, uncompressed COREMESH only)
- Local Roblox client/studio content folder mesh cache
- Async asset downloading and caching
"""

import glob
import gzip
import os
import re
import struct
import bpy

from . import paths
from . import downloads

# Downloaded .mesh files (extension user folder)
MESH_CACHE_DIR = paths.cache_dir("meshes")

# Asset IDs that failed to download (private asset, 401/403, not a mesh) - never retried this session
_failed_mesh_ids = set()
_created_mesh_cache = {}
# Set to True whenever get_or_load_mesh() had to start/await a download.
# sync_manager resets and reads it to know an object must be re-synced later.
missing_hit = False

# Map of well-known standard Roblox mesh names to local files
_local_mesh_paths = {}


def _find_local_roblox_meshes():
    global _local_mesh_paths
    if _local_mesh_paths:
        return _local_mesh_paths

    content = paths.studio_content_dir()
    if not content:
        return {}

    pattern = os.path.join(content, "**", "*.mesh")
    for fpath in glob.glob(pattern, recursive=True):
        fname = os.path.basename(fpath).lower()
        if fname not in _local_mesh_paths:
            _local_mesh_paths[fname] = fpath

    return _local_mesh_paths


def parse_roblox_mesh_v1(data_bytes):
    """
    Parses Roblox ASCII mesh format (version 1.00 / 1.01).
    Format:
    version 1.00
    <num_faces>
    [vx,vy,vz][nx,ny,nz][u,v,w] ...
    """
    text = data_bytes.decode("ascii", errors="ignore")
    lines = text.splitlines()
    if len(lines) < 3:
        return None

    try:
        int(lines[1].strip())  # face count line: must be a number in a valid version 1 mesh
    except ValueError:
        return None

    # version 1.00 stores positions at 2x scale (fixed in 1.01)
    pos_scale = 0.5 if lines[0].strip().startswith("version 1.00") else 1.0

    coord_pattern = re.compile(r"\[([^\]]+)\]")
    bracket_groups = coord_pattern.findall(lines[2])
    total_verts = len(bracket_groups) // 3
    if total_verts == 0:
        return None

    verts = []
    normals = []
    uvs = []

    for i in range(total_verts):
        pos_str = bracket_groups[i * 3]
        norm_str = bracket_groups[i * 3 + 1]
        uv_str = bracket_groups[i * 3 + 2]

        pos = [float(p.strip()) for p in pos_str.split(",")]
        norm = [float(n.strip()) for n in norm_str.split(",")]
        uv = [float(u.strip()) for u in uv_str.split(",")]

        # Roblox (X, Y, Z) -> Blender (X, -Z, Y)
        verts.append((pos[0] * pos_scale, -pos[2] * pos_scale, pos[1] * pos_scale))
        normals.append((norm[0], -norm[2], norm[1]))
        uvs.append((uv[0], 1.0 - uv[1]))

    faces = []
    for f in range(total_verts // 3):
        faces.append((f * 3, f * 3 + 1, f * 3 + 2))

    return {
        "verts": verts,
        "faces": faces,
        "normals": normals,
        "uvs": uvs,
    }


def parse_roblox_mesh_v2(data_bytes):
    """
    Parses Roblox binary mesh format (version 2.00).
    Header:
    'version 2.00\\n'
    uint16 cbHeader
    uint8 cbVertex
    uint8 cbFace
    uint32 numVertices
    uint32 numFaces
    ... vertex buffer (cbVertex bytes per vertex)
    ... face buffer (cbFace bytes per face)
    """
    first_newline = data_bytes.find(b"\n")
    if first_newline == -1:
        return None

    idx = first_newline + 1
    if len(data_bytes) < idx + 12:
        return None

    cb_header, cb_vertex, cb_face, num_verts, num_faces = struct.unpack(
        "<HBBII", data_bytes[idx : idx + 12]
    )

    offset = idx + cb_header
    verts = []
    normals = []
    uvs = []

    for _ in range(num_verts):
        if offset + 36 > len(data_bytes):
            break
        vx, vy, vz, nx, ny, nz, u, v, w = struct.unpack("<9f", data_bytes[offset : offset + 36])
        verts.append((vx, -vz, vy))
        normals.append((nx, -nz, ny))
        uvs.append((u, 1.0 - v))
        offset += cb_vertex

    faces = []
    for _ in range(num_faces):
        if offset + 12 > len(data_bytes):
            break
        f1, f2, f3 = struct.unpack("<3I", data_bytes[offset : offset + 12])
        faces.append((f1, f2, f3))
        offset += cb_face

    if not verts or not faces:
        return None

    return {
        "verts": verts,
        "faces": faces,
        "normals": normals,
        "uvs": uvs,
    }


def _read_vertices(data_bytes, offset, count, stride):
    """Reads `count` vertices (pos, normal, uv) with the given byte stride. Returns (verts, normals, uvs, new_offset)."""
    verts = []
    normals = []
    uvs = []
    end = offset + count * stride
    if stride < 32 or end > len(data_bytes):
        return None
    unpack = struct.Struct("<8f").unpack_from
    for i in range(count):
        vx, vy, vz, nx, ny, nz, u, v = unpack(data_bytes, offset + i * stride)
        verts.append((vx, -vz, vy))
        normals.append((nx, -nz, ny))
        uvs.append((u, 1.0 - v))
    return verts, normals, uvs, end


def _read_faces(data_bytes, offset, count, stride, num_verts):
    end = offset + count * stride
    if stride < 12 or end > len(data_bytes):
        return None
    faces = []
    unpack = struct.Struct("<3I").unpack_from
    for i in range(count):
        f = unpack(data_bytes, offset + i * stride)
        if f[0] < num_verts and f[1] < num_verts and f[2] < num_verts:
            faces.append(f)
    return faces, end


def _lod0_faces(faces, lods):
    """Keeps only the highest-detail LOD (faces[lods[0]:lods[1]])."""
    if lods and len(lods) >= 2 and 0 <= lods[0] < lods[1] <= len(faces):
        return faces[lods[0]:lods[1]]
    return faces


def parse_roblox_mesh_v3(data_bytes):
    """
    Binary mesh v3.00 / 3.01:
    uint16 cbHeader, uint8 cbVertex, uint8 cbFace, uint16 cbLOD, uint16 numLODs, uint32 numVerts, uint32 numFaces
    then vertices, faces, LOD offsets (uint32).
    """
    idx = data_bytes.find(b"\n") + 1
    if idx <= 0 or len(data_bytes) < idx + 16:
        return None
    cb_header, cb_vertex, cb_face, cb_lod, num_lods, num_verts, num_faces = struct.unpack_from("<HBBHHII", data_bytes, idx)
    offset = idx + cb_header

    res = _read_vertices(data_bytes, offset, num_verts, cb_vertex)
    if not res:
        return None
    verts, normals, uvs, offset = res
    fres = _read_faces(data_bytes, offset, num_faces, cb_face, num_verts)
    if not fres:
        return None
    faces, offset = fres

    lods = []
    if cb_lod >= 4 and offset + num_lods * cb_lod <= len(data_bytes):
        for i in range(num_lods):
            lods.append(struct.unpack_from("<I", data_bytes, offset + i * cb_lod)[0])
    faces = _lod0_faces(faces, lods)
    if not verts or not faces:
        return None
    return {"verts": verts, "faces": faces, "normals": normals, "uvs": uvs}


def parse_roblox_mesh_v4(data_bytes):
    """
    Binary mesh v4.00 / 4.01 / 5.00:
    uint16 cbHeader, uint16 lodType, uint32 numVerts, uint32 numFaces, uint16 numLODs, uint16 numBones,
    uint32 cbBoneNames, uint16 numSubsets, uint8 numHighQualityLODs, uint8 pad  (+ v5: uint32 facsFormat, uint32 facsSize)
    then vertices (40 bytes), envelopes (8 bytes per vertex if numBones > 0), faces (12 bytes), LOD offsets (uint32).
    """
    idx = data_bytes.find(b"\n") + 1
    if idx <= 0 or len(data_bytes) < idx + 24:
        return None
    cb_header, _lod_type, num_verts, num_faces, num_lods, num_bones, _cb_names, _num_subsets, _hq, _pad = \
        struct.unpack_from("<HHIIHHIHBB", data_bytes, idx)
    offset = idx + cb_header

    res = _read_vertices(data_bytes, offset, num_verts, 40)
    if not res:
        return None
    verts, normals, uvs, offset = res
    if num_bones > 0:
        offset += num_verts * 8  # skinning envelopes
    fres = _read_faces(data_bytes, offset, num_faces, 12, num_verts)
    if not fres:
        return None
    faces, offset = fres

    lods = []
    if offset + num_lods * 4 <= len(data_bytes):
        lods = list(struct.unpack_from(f"<{num_lods}I", data_bytes, offset))
    faces = _lod0_faces(faces, lods)
    if not verts or not faces:
        return None
    return {"verts": verts, "faces": faces, "normals": normals, "uvs": uvs}


def parse_roblox_mesh_v6(data_bytes):
    """
    Chunked mesh v6.00 / 7.00: sequence of [char[8] type][uint32 version][uint32 size][data].
    Only uncompressed COREMESH (chunk version 1) is supported; Draco-compressed (version 2) returns None.
    """
    offset = data_bytes.find(b"\n") + 1
    if offset <= 0:
        return None
    verts = normals = uvs = None
    faces = None
    lods = []
    while offset + 16 <= len(data_bytes):
        ctype = data_bytes[offset:offset + 8].rstrip(b"\x00 ")
        cver, csize = struct.unpack_from("<II", data_bytes, offset + 8)
        cdata = offset + 16
        if ctype == b"COREMESH" and cver == 1:
            num_verts = struct.unpack_from("<I", data_bytes, cdata)[0]
            res = _read_vertices(data_bytes, cdata + 4, num_verts, 40)
            if not res:
                return None
            verts, normals, uvs, p = res
            num_faces = struct.unpack_from("<I", data_bytes, p)[0]
            fres = _read_faces(data_bytes, p + 4, num_faces, 12, num_verts)
            if not fres:
                return None
            faces = fres[0]
        elif ctype == b"LODS" and cver == 1:
            num_offsets = struct.unpack_from("<I", data_bytes, cdata + 3)[0]
            if cdata + 7 + num_offsets * 4 <= len(data_bytes):
                lods = list(struct.unpack_from(f"<{num_offsets}I", data_bytes, cdata + 7))
        offset = cdata + csize

    if not verts or not faces:
        return None
    faces = _lod0_faces(faces, lods)
    return {"verts": verts, "faces": faces, "normals": normals, "uvs": uvs}


def parse_roblox_mesh(data_bytes):
    if not data_bytes:
        return None
    if data_bytes.startswith(b"\x1f\x8b"):
        try:
            data_bytes = gzip.decompress(data_bytes)
        except Exception:
            pass

    idx = data_bytes.find(b"version ", 0, 40)
    if idx == -1:
        return None
    data_bytes = data_bytes[idx:]
    parsers = (
        (b"version 1.0", parse_roblox_mesh_v1),
        (b"version 2.0", parse_roblox_mesh_v2),
        (b"version 3.0", parse_roblox_mesh_v3),
        (b"version 4.0", parse_roblox_mesh_v4),
        (b"version 5.0", parse_roblox_mesh_v4),
        (b"version 6.0", parse_roblox_mesh_v6),
        (b"version 7.0", parse_roblox_mesh_v6),
    )
    for prefix, parser in parsers:
        if data_bytes.startswith(prefix):
            try:
                return parser(data_bytes)
            except (struct.error, ValueError, IndexError):
                return None
    return None


def async_download_mesh(clean_id):
    """Downloads a .mesh in a separate process (see downloads.py); never blocks Blender."""
    if clean_id in _failed_mesh_ids:
        return
    if not clean_id.isdigit():
        _failed_mesh_ids.add(clean_id)
        return
    cache_file = os.path.join(MESH_CACHE_DIR, f"{clean_id}.mesh")
    if os.path.exists(cache_file):
        return

    def on_done(ok):
        if not ok:
            print(f"[Roblox Live Link] Mesh {clean_id} could not be downloaded")
            _failed_mesh_ids.add(clean_id)

    downloads.start("mesh", clean_id, cache_file, on_done)


def extract_clean_asset_id(raw_str):
    if not raw_str:
        return ""
    clean = str(raw_str).strip()
    match = re.search(r"[?&]id=(\d+)", clean)
    if not match:
        match = re.search(r"rbxassetid://(\d+)", clean)
    if match:
        return match.group(1)
    digits = re.findall(r"\d+", clean)
    return max(digits, key=len) if digits else clean


def stamp_geometry(mesh):
    """Remembers vertex count and first vertex, to notice later if the mesh was edited in Blender."""
    co = list(mesh.vertices[0].co) if len(mesh.vertices) else []
    mesh["rbx_geo"] = [float(len(mesh.vertices))] + co


def geometry_intact(mesh):
    geo = mesh.get("rbx_geo")
    if geo is None:
        return True
    geo = list(geo)
    if len(mesh.vertices) != int(geo[0]):
        return False
    if len(geo) < 4 or not len(mesh.vertices):
        return True
    co = mesh.vertices[0].co
    return all(abs(co[i] - geo[1 + i]) < 1e-4 for i in range(3))


def protect_shared(mesh):
    """
    Gives a shared mesh a hidden second user (an object outside the scene, kept by a fake user).
    Blender then treats the mesh as multi-user, so "Apply Scale" or similar on one part makes a copy
    for that part instead of changing the mesh of every part that shares it.
    """
    holder_name = "RbxHolder_" + mesh.name
    holder = bpy.data.objects.get(holder_name)
    if holder is None:
        holder = bpy.data.objects.new(holder_name, mesh)
        holder.use_fake_user = True
    elif holder.data != mesh:
        holder.data = mesh


def copy_geometry(dst, src):
    """Replaces dst's geometry (incl. UVs) with src's, keeping the dst datablock and its users."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(src)
    bm.to_mesh(dst)
    bm.free()


def _fill_mesh(mesh, mesh_dict):
    verts = mesh_dict["verts"]
    faces = mesh_dict["faces"]
    uvs = mesh_dict.get("uvs", [])
    mesh.clear_geometry()
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    if verts:
        min_x, max_x = min(v[0] for v in verts), max(v[0] for v in verts)
        min_y, max_y = min(v[1] for v in verts), max(v[1] for v in verts)
        min_z, max_z = min(v[2] for v in verts), max(v[2] for v in verts)
        dx = max(0.001, max_x - min_x)
        dy = max(0.001, max_z - min_z)  # Roblox Y (height) is Blender Z
        dz = max(0.001, max_y - min_y)  # Roblox Z (depth) is Blender Y
        mesh["rbx_native_size"] = (dx, dy, dz)
    if uvs and len(uvs) == len(verts):
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            v_idx = loop.vertex_index
            if v_idx < len(uvs):
                uv_layer.data[loop.index].uv = uvs[v_idx]
    stamp_geometry(mesh)


def get_builtin_mesh(name):
    """
    A mesh that ships with Roblox Studio (e.g. "head" for SpecialMesh MeshType Head), read from the
    local Studio install. None when Studio isn't installed on this computer.
    """
    mesh_name = f"RbxBuiltin_{name}"
    existing = bpy.data.meshes.get(mesh_name)
    if existing is not None and len(existing.vertices) > 0:
        if geometry_intact(existing):
            if existing.get("rbx_geo") is None:
                stamp_geometry(existing)
            protect_shared(existing)
            return existing
    path = _find_local_roblox_meshes().get(f"{name}.mesh")
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            mesh_dict = parse_roblox_mesh(f.read())
    except OSError:
        return None
    if not mesh_dict:
        return None
    mesh = existing if existing is not None else bpy.data.meshes.new(mesh_name)
    _fill_mesh(mesh, mesh_dict)
    mesh.use_fake_user = True
    protect_shared(mesh)
    return mesh


def get_or_load_mesh(mesh_id_str, fallback_name=None):
    """
    Returns a bpy.data.meshes instance with parsed Roblox geometry,
    including vertex positions, faces, and UV mapping.
    """
    if not mesh_id_str:
        return None

    asset_id = extract_clean_asset_id(mesh_id_str)
    if not asset_id:
        return None

    mesh_name = f"RbxMesh_{asset_id}"
    existing = None
    if mesh_name in bpy.data.meshes:
        m = bpy.data.meshes[mesh_name]
        try:
            if len(m.vertices) > 0:
                if geometry_intact(m):
                    if m.get("rbx_geo") is None:
                        stamp_geometry(m)
                    protect_shared(m)
                    return m
                # Edited in Blender (e.g. scale applied into it): reload the original geometry in place
                existing = m
        except ReferenceError:
            pass

    # 1. Check disk cache in MESH_CACHE_DIR
    cache_path = os.path.join(MESH_CACHE_DIR, f"{asset_id}.mesh")
    raw_data = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                raw_data = f.read()
        except Exception:
            raw_data = None

    # 2. Check local Roblox installation if not in disk cache
    if not raw_data and fallback_name:
        local_meshes = _find_local_roblox_meshes()
        target_file = f"{fallback_name}.mesh".lower()
        if target_file in local_meshes:
            try:
                with open(local_meshes[target_file], "rb") as f:
                    raw_data = f.read()
            except Exception:
                raw_data = None

    # 3. If still no data, trigger background download and return None
    if not raw_data:
        global missing_hit
        if not paths.online_access_allowed():
            return None  # "Allow Online Access" is off: keep the box fallback
        if asset_id not in _failed_mesh_ids:
            async_download_mesh(asset_id)
            if asset_id not in _failed_mesh_ids:
                missing_hit = True
        return None

    # 4. Parse mesh data
    mesh_dict = parse_roblox_mesh(raw_data)
    if not mesh_dict:
        return None

    # 5. Build Blender Mesh (or restore an edited one in place, so every part using it is fixed)
    new_mesh = existing if existing is not None else bpy.data.meshes.new(mesh_name)
    _fill_mesh(new_mesh, mesh_dict)
    new_mesh.use_fake_user = True
    protect_shared(new_mesh)
    _created_mesh_cache[asset_id] = new_mesh
    return new_mesh
