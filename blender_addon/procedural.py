"""
Procedural Roblox materials (no image files).

Every built-in Roblox material gets a shader-node pattern (noise, wood grain, bricks, cobbles...) that
modulates the part color slightly, like Roblox's colorable material maps do. Relief comes from a Bump node,
which works for any face orientation (tangent-space normal maps did not on box-projected parts).

Patterns are evaluated in world space through a Mapping node whose scale is the panel's "Texture Scale"
(default 0.0625: 1 mapped unit = 16 studs), so sizes below are given in studs.
"""

STUDS_PER_UNIT = 16.0

# Noise-like materials: size (studs), detail, brightness range (lo, hi), roughness, bump, distortion, metallic
_NOISE = {
    "Concrete": (0.35, 6, 0.90, 1.04, 0.85, 0.15, 0.0, 0.0),
    "Plaster": (0.60, 4, 0.94, 1.03, 0.90, 0.08, 0.0, 0.0),
    "Pavement": (0.50, 5, 0.88, 1.04, 0.85, 0.20, 0.0, 0.0),
    "Asphalt": (0.20, 6, 0.82, 1.08, 0.90, 0.25, 0.0, 0.0),
    "Limestone": (1.20, 6, 0.88, 1.05, 0.85, 0.20, 0.0, 0.0),
    "Sandstone": (1.00, 6, 0.85, 1.05, 0.90, 0.20, 0.5, 0.0),
    "Slate": (2.00, 8, 0.82, 1.06, 0.80, 0.30, 0.6, 0.0),
    "Rock": (2.50, 8, 0.75, 1.08, 0.85, 0.45, 1.0, 0.0),
    "Basalt": (1.50, 8, 0.75, 1.05, 0.80, 0.35, 0.4, 0.0),
    "Granite": (0.25, 6, 0.80, 1.08, 0.60, 0.10, 0.0, 0.0),
    "Marble": (1.50, 4, 0.85, 1.05, 0.25, 0.05, 3.0, 0.0),
    "Sand": (0.15, 4, 0.90, 1.05, 0.95, 0.15, 0.0, 0.0),
    "Salt": (0.12, 3, 0.92, 1.04, 0.90, 0.10, 0.0, 0.0),
    "Ground": (0.80, 6, 0.85, 1.06, 0.95, 0.20, 0.0, 0.0),
    "Mud": (1.00, 5, 0.85, 1.05, 0.60, 0.15, 0.0, 0.0),
    "Snow": (0.80, 4, 0.95, 1.02, 0.80, 0.10, 0.0, 0.0),
    "Fabric": (0.06, 2, 0.93, 1.03, 0.95, 0.10, 0.0, 0.0),
    "Carpet": (0.08, 3, 0.90, 1.04, 0.98, 0.15, 0.0, 0.0),
    "Leather": (0.30, 4, 0.90, 1.04, 0.50, 0.10, 0.0, 0.0),
    "Rubber": (0.50, 2, 0.96, 1.02, 0.90, 0.05, 0.0, 0.0),
    "Cardboard": (0.40, 3, 0.92, 1.03, 0.90, 0.05, 0.0, 0.0),
    "Metal": (0.80, 3, 0.94, 1.04, 0.35, 0.05, 0.0, 0.90),
    "DiamondPlate": (0.30, 3, 0.92, 1.04, 0.40, 0.20, 0.0, 0.90),
    "CorrodedMetal": (0.60, 7, 0.70, 1.10, 0.80, 0.30, 0.0, 0.50),
    "Foil": (0.30, 4, 0.90, 1.06, 0.20, 0.20, 0.0, 1.00),
}

# Grass: two noise layers (blades + patches)
_GRASS = {"Grass", "LeafyGrass"}

# Masonry (2D pattern, projected on 3 axes): brick width, row height, mortar (studs), offset,
# brick colors (two multipliers), mortar multiplier, roughness, bump
_BRICK = {
    "Brick": (2.0, 0.7, 0.06, 0.5, 0.95, 0.82, 1.18, 0.85, 0.30),
    "ClayRoofTiles": (1.0, 0.5, 0.05, 0.5, 0.95, 0.85, 0.70, 0.70, 0.30),
    "RoofShingles": (1.5, 0.6, 0.05, 0.5, 0.92, 0.80, 0.60, 0.90, 0.25),
    "CeramicTiles": (1.5, 1.5, 0.05, 0.0, 1.00, 0.97, 0.85, 0.25, 0.15),
    "WoodPlanks": (4.0, 1.0, 0.03, 0.5, 1.00, 0.90, 0.55, 0.70, 0.15),
}

# Wood grain (also under the WoodPlanks seams): band spacing (studs), brightness range, roughness
_WOOD = (0.4, 0.80, 1.05, 0.70)

# Cells: cell size (studs), roughness, bump
_CELLS = {
    "Cobblestone": (1.2, 0.85, 0.40),
    "Pebble": (0.5, 0.85, 0.35),
    "CrackedLava": (2.0, 0.85, 0.35),
}

_PLAIN_ROUGHNESS = {"Glass": 0.05, "Ice": 0.10, "Glacier": 0.20, "ForceField": 0.30}


def has_pattern(rbx_material):
    return rbx_material in _NOISE or rbx_material in _GRASS or rbx_material in _BRICK \
        or rbx_material in _CELLS or rbx_material == "Wood"


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------

def _new(nt, node_type, **props):
    node = nt.nodes.new(node_type)
    for k, v in props.items():
        setattr(node, k, v)
    return node


def _math(nt, op, a, b=None):
    node = _new(nt, 'ShaderNodeMath', operation=op)
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            node.inputs[i].default_value = v
        else:
            nt.links.new(v, node.inputs[i])
    return node.outputs[0]


def _map_range(nt, value, from_min, from_max, to_min, to_max):
    node = _new(nt, 'ShaderNodeMapRange')
    nt.links.new(value, node.inputs['Value'])
    node.inputs['From Min'].default_value = from_min
    node.inputs['From Max'].default_value = from_max
    node.inputs['To Min'].default_value = to_min
    node.inputs['To Max'].default_value = to_max
    return node.outputs['Result']


def _vec_math(nt, op, a, b=None, scale=None):
    node = _new(nt, 'ShaderNodeVectorMath', operation=op)
    nt.links.new(a, node.inputs[0])
    if b is not None:
        nt.links.new(b, node.inputs[1])
    if scale is not None:
        if isinstance(scale, (int, float)):
            node.inputs['Scale'].default_value = scale
        else:
            nt.links.new(scale, node.inputs['Scale'])
    return node.outputs['Vector']


def _noise(nt, vec, size, detail, roughness=0.55, distortion=0.0):
    node = _new(nt, 'ShaderNodeTexNoise')
    nt.links.new(vec, node.inputs['Vector'])
    node.inputs['Scale'].default_value = STUDS_PER_UNIT / size
    node.inputs['Detail'].default_value = detail
    node.inputs['Roughness'].default_value = roughness
    node.inputs['Distortion'].default_value = distortion
    return node.outputs['Fac']


def _triplanar(nt, vec, make_pattern):
    """
    Runs a 2D pattern (e.g. Brick) on the three world planes and blends them by the surface normal,
    so rows stay horizontal on walls and the pattern isn't smeared on faces parallel to an axis.
    make_pattern(vector_socket) -> (color_socket, fac_socket)
    """
    geom = _new(nt, 'ShaderNodeNewGeometry')
    n_abs = _vec_math(nt, 'ABSOLUTE', geom.outputs['Normal'])
    n_sep = _new(nt, 'ShaderNodeSeparateXYZ')
    nt.links.new(n_abs, n_sep.inputs[0])
    weights = [_math(nt, 'POWER', n_sep.outputs[i], 8.0) for i in range(3)]
    total = _math(nt, 'ADD', _math(nt, 'ADD', weights[0], weights[1]), weights[2])
    weights = [_math(nt, 'DIVIDE', w, total) for w in weights]

    p_sep = _new(nt, 'ShaderNodeSeparateXYZ')
    nt.links.new(vec, p_sep.inputs[0])
    px, py, pz = p_sep.outputs['X'], p_sep.outputs['Y'], p_sep.outputs['Z']
    # Faces looking along X use (Y, Z), along Y use (X, Z), along Z use (X, Y): second axis = rows
    planes = ((py, pz), (px, pz), (px, py))
    color_sum = fac_sum = None
    for (u, v), w in zip(planes, weights):
        comb = _new(nt, 'ShaderNodeCombineXYZ')
        nt.links.new(u, comb.inputs['X'])
        nt.links.new(v, comb.inputs['Y'])
        color, fac = make_pattern(comb.outputs['Vector'])
        c = _vec_math(nt, 'SCALE', color, scale=w)
        f = _math(nt, 'MULTIPLY', fac, w)
        color_sum = c if color_sum is None else _vec_math(nt, 'ADD', color_sum, c)
        fac_sum = f if fac_sum is None else _math(nt, 'ADD', fac_sum, f)
    return color_sum, fac_sum


def _brick_pattern(nt, spec):
    width, row, mortar, offset, c1, c2, cm = spec[:7]

    def make(vec):
        node = _new(nt, 'ShaderNodeTexBrick', offset=offset, offset_frequency=2)
        nt.links.new(vec, node.inputs['Vector'])
        # Scale = 16 makes one brick-space unit equal one stud
        node.inputs['Scale'].default_value = STUDS_PER_UNIT
        node.inputs['Brick Width'].default_value = width
        node.inputs['Row Height'].default_value = row
        node.inputs['Mortar Size'].default_value = mortar
        node.inputs['Mortar Smooth'].default_value = 0.2
        node.inputs['Bias'].default_value = 0.0
        node.inputs['Color1'].default_value = (c1, c1, c1, 1.0)
        node.inputs['Color2'].default_value = (c2, c2, c2, 1.0)
        node.inputs['Mortar'].default_value = (cm, cm, cm, 1.0)
        return node.outputs['Color'], node.outputs['Fac']
    return make


def _wood(nt, vec):
    band, lo, hi, _rough = _WOOD
    wave = _new(nt, 'ShaderNodeTexWave', wave_type='BANDS', bands_direction='X')
    nt.links.new(vec, wave.inputs['Vector'])
    # Band period of the Wave node is (2*pi/20) / Scale texture units
    wave.inputs['Scale'].default_value = 0.314 * STUDS_PER_UNIT / band
    wave.inputs['Distortion'].default_value = 4.0
    wave.inputs['Detail'].default_value = 2.0
    fac = wave.outputs['Fac']
    return _map_range(nt, fac, 0.0, 1.0, lo, hi), fac


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply(nt, bsdf, rbx_material, rgb, tex_scale):
    """
    Builds the procedural version of a built-in Roblox material into the node tree.
    Returns False for materials without a pattern (Plastic, SmoothPlastic, Neon...): the caller handles them.
    """
    if not has_pattern(rbx_material):
        if rbx_material in _PLAIN_ROUGHNESS:
            bsdf.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
            bsdf.inputs["Roughness"].default_value = _PLAIN_ROUGHNESS[rbx_material]
            return True
        return False

    geom = _new(nt, 'ShaderNodeNewGeometry')
    mapping = _new(nt, 'ShaderNodeMapping')
    mapping.inputs['Scale'].default_value = (tex_scale, tex_scale, tex_scale)
    nt.links.new(geom.outputs['Position'], mapping.inputs['Vector'])
    vec = mapping.outputs['Vector']

    color_node = _new(nt, 'ShaderNodeRGB')
    color_node.outputs[0].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    part_color = color_node.outputs[0]

    roughness, bump_strength, metallic = 0.8, 0.2, 0.0
    emission = None

    if rbx_material in _NOISE:
        size, detail, lo, hi, roughness, bump_strength, distortion, metallic = _NOISE[rbx_material]
        fac = _noise(nt, vec, size, detail, distortion=distortion)
        mult = _map_range(nt, fac, 0.3, 0.7, lo, hi)
        base = _vec_math(nt, 'SCALE', part_color, scale=mult)
        height = fac
    elif rbx_material in _GRASS:
        blades = _noise(nt, vec, 0.18, 8, roughness=0.7)
        patches = _noise(nt, vec, 3.0, 3)
        fac = _math(nt, 'ADD', _math(nt, 'MULTIPLY', blades, 0.65), _math(nt, 'MULTIPLY', patches, 0.35))
        # Steep range: clearly visible blades and darker patches
        mult = _map_range(nt, fac, 0.35, 0.65, 0.55, 1.20)
        base = _vec_math(nt, 'SCALE', part_color, scale=mult)
        height, roughness, bump_strength = blades, 0.9, 0.45
    elif rbx_material == "Wood":
        mult, fac = _wood(nt, vec)
        base = _vec_math(nt, 'SCALE', part_color, scale=mult)
        height, roughness, bump_strength = fac, _WOOD[3], 0.1
    elif rbx_material in _BRICK:
        spec = _BRICK[rbx_material]
        color, mortar_fac = _triplanar(nt, vec, _brick_pattern(nt, spec))
        base = _vec_math(nt, 'MULTIPLY', part_color, color)
        if rbx_material == "WoodPlanks":
            grain, _ = _wood(nt, vec)
            base = _vec_math(nt, 'SCALE', base, scale=grain)
        # Mortar is recessed
        height = _math(nt, 'SUBTRACT', 1.0, mortar_fac)
        roughness, bump_strength = spec[7], spec[8]
    else:  # cells
        size, roughness, bump_strength = _CELLS[rbx_material]
        edges = _new(nt, 'ShaderNodeTexVoronoi', feature='DISTANCE_TO_EDGE')
        nt.links.new(vec, edges.inputs['Vector'])
        edges.inputs['Scale'].default_value = STUDS_PER_UNIT / size
        cells = _new(nt, 'ShaderNodeTexVoronoi', feature='F1')
        nt.links.new(vec, cells.inputs['Vector'])
        cells.inputs['Scale'].default_value = STUDS_PER_UNIT / size
        # Gaps between stones are darker, each stone slightly different
        gap = _map_range(nt, edges.outputs['Distance'], 0.0, 0.06, 0.55, 1.0)
        bw = _new(nt, 'ShaderNodeRGBToBW')
        nt.links.new(cells.outputs['Color'], bw.inputs[0])
        variation = _map_range(nt, bw.outputs[0], 0.0, 1.0, 0.88, 1.05)
        base = _vec_math(nt, 'SCALE', _vec_math(nt, 'SCALE', part_color, scale=gap), scale=variation)
        height = _map_range(nt, edges.outputs['Distance'], 0.0, 0.08, 0.0, 1.0)
        if rbx_material == "CrackedLava":
            # Glowing cracks
            emission = _map_range(nt, edges.outputs['Distance'], 0.0, 0.04, 3.0, 0.0)

    nt.links.new(base, bsdf.inputs['Base Color'])
    bsdf.inputs['Roughness'].default_value = roughness
    bsdf.inputs['Metallic'].default_value = metallic
    if bump_strength > 0:
        bump = _new(nt, 'ShaderNodeBump')
        bump.inputs['Strength'].default_value = bump_strength
        bump.inputs['Distance'].default_value = 0.02
        nt.links.new(height, bump.inputs['Height'])
        nt.links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
    if emission is not None:
        bsdf.inputs['Emission Color'].default_value = (1.0, 0.35, 0.05, 1.0)
        nt.links.new(emission, bsdf.inputs['Emission Strength'])
    return True
