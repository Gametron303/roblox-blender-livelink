"""
Roblox Studio <-> Blender Live-Link (Blender extension, metadata in blender_manifest.toml)
Registers operators, UI panel, and manages server lifecycle.
"""

# Keep in sync with "version" in blender_manifest.toml and PLUGIN_VERSION in the Studio plugin
VERSION_LABEL = "1.3.0-beta"

import importlib
import bpy
from bpy.props import IntProperty, FloatProperty, BoolProperty
from bpy.types import Panel, Operator, PropertyGroup

from . import paths
from . import server
from . import mesh_loader
from . import surfaces
from . import procedural
from . import sync_manager

# Ensure hot-reloading submodules when files change
importlib.reload(paths)
importlib.reload(server)
importlib.reload(mesh_loader)
importlib.reload(surfaces)
importlib.reload(procedural)
importlib.reload(sync_manager)


class RobloxLiveLinkSettings(PropertyGroup):
    port: IntProperty(
        name="Port",
        description="Port for the local HTTP sync server. If not 28888, set the same number as the "
                    "Workspace attribute \"LiveLinkPort\" in Roblox Studio",
        default=28888,
        min=1024,
        max=65535
    )
    match_roblox_colors: BoolProperty(
        name="Match Roblox Colors",
        description="Use the 'Standard' view transform: AgX/Filmic wash out part colors compared to Roblox",
        default=True,
    )
    tex_scale: FloatProperty(
        name="Texture Scale",
        description="World-space tiling scale for Roblox PBR materials (default 0.0625 = 1 tile per 16 studs)",
        default=0.0625,
        min=0.005,
        max=1.0,
        precision=4
    )


class ROBLOX_OT_start_livelink(Operator):
    bl_idname = "roblox.start_livelink"
    bl_label = "Start Live Link Server"
    bl_description = "Start listening for incoming scene data from Roblox Studio"

    def execute(self, context):
        settings = context.scene.roblox_livelink_settings
        success, msg = server.start_server(port=settings.port)
        if success:
            sync_manager.start_sync_timer()
            if hasattr(sync_manager, "set_viewport_material_shading"):
                sync_manager.set_viewport_material_shading()
            if settings.match_roblox_colors:
                sync_manager.apply_roblox_color_management(context.scene)
            self.report({'INFO'}, msg)
        else:
            self.report({'ERROR'}, msg)
        return {'FINISHED'}


class ROBLOX_OT_stop_livelink(Operator):
    bl_idname = "roblox.stop_livelink"
    bl_label = "Stop Live Link Server"
    bl_description = "Stop listening for incoming scene data"

    def execute(self, context):
        sync_manager.stop_sync_timer()
        success, msg = server.stop_server()
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ROBLOX_OT_clear_livelink(Operator):
    bl_idname = "roblox.clear_livelink"
    bl_label = "Clear Synced Objects"
    bl_description = "Remove all objects imported via Roblox Live Link"

    def execute(self, context):
        sync_manager.clear_all_linked_objects()
        self.report({'INFO'}, "Cleared all Roblox Live Link objects")
        return {'FINISHED'}


class ROBLOX_OT_set_material_view(Operator):
    bl_idname = "roblox.set_material_view"
    bl_label = "Show Textures & Colors"
    bl_description = "Switch 3D Viewport to Material Preview mode"

    def execute(self, context):
        if context.scene.roblox_livelink_settings.match_roblox_colors:
            sync_manager.apply_roblox_color_management(context.scene)
        if hasattr(sync_manager, "set_viewport_material_shading"):
            sync_manager.set_viewport_material_shading()
        else:
            try:
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        for space in area.spaces:
                            if space.type == 'VIEW_3D':
                                space.shading.type = 'MATERIAL'
            except Exception:
                pass
        self.report({'INFO'}, "Switched to Material Preview mode")
        return {'FINISHED'}


class ROBLOX_OT_reload_addon(Operator):
    bl_idname = "roblox.reload_addon"
    bl_label = "Reload Addon"
    bl_description = "Reload LiveLink python modules from disk"

    def execute(self, context):
        # Stop the running server/timer first: reloading resets module globals, which would
        # otherwise leave an orphaned server thread holding the port.
        was_running = server.is_running()
        port = server.get_port()
        if was_running:
            sync_manager.stop_sync_timer()
            server.stop_server()
        importlib.reload(paths)
        importlib.reload(server)
        importlib.reload(mesh_loader)
        importlib.reload(surfaces)
        importlib.reload(procedural)
        importlib.reload(sync_manager)
        if was_running:
            ok, msg = server.start_server(port=port)
            if ok:
                sync_manager.start_sync_timer()
            else:
                self.report({'ERROR'}, msg)
        for cls in classes:
            if not hasattr(bpy.types, cls.__name__):
                try:
                    bpy.utils.register_class(cls)
                except Exception:
                    pass
        self.report({'INFO'}, "Roblox Live Link modules reloaded successfully")
        return {'FINISHED'}


class ROBLOX_OT_toggle_csg(Operator):
    bl_idname = "roblox.toggle_csg"
    bl_label = "Toggle CSG Slabs"
    bl_description = "Toggle visibility of CSG operations and unclipped floor/ceiling slabs"

    def execute(self, context):
        csg_col = bpy.data.collections.get(sync_manager.CSG_COLLECTION_NAME)
        if csg_col:
            csg_col.hide_viewport = not csg_col.hide_viewport
            state_str = "Hidden" if csg_col.hide_viewport else "Visible"
            self.report({'INFO'}, f"CSG Slabs collection is now {state_str}")
        else:
            self.report({'INFO'}, "No CSG Slabs collection found")
        return {'FINISHED'}


class ROBLOX_OT_refresh_materials(Operator):
    bl_idname = "roblox.refresh_materials"
    bl_label = "Apply Texture Scale"
    bl_description = "Update tiling scale on all Roblox materials"

    def execute(self, context):
        settings = context.scene.roblox_livelink_settings
        scale = settings.tex_scale
        count = 0
        for mat in bpy.data.materials:
            if mat.name.startswith("Rbx_") and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'MAPPING':
                        node.inputs['Scale'].default_value = (scale, scale, scale)
                        count += 1
        self.report({'INFO'}, f"Updated texture scale to {scale:.4f} across {count} materials")
        return {'FINISHED'}


class ROBLOX_OT_optimize_viewport(Operator):
    bl_idname = "roblox.optimize_viewport"
    bl_label = "Boost Viewport & Playback FPS"
    bl_description = "Optimize viewport settings, shaders, and playback mode for 60+ FPS playback"

    def execute(self, context):
        context.scene.sync_mode = 'FRAME_DROP'

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.show_object_outline = False

        if hasattr(context.scene, "eevee"):
            try:
                context.scene.eevee.use_fast_gi = False
                context.scene.eevee.taa_samples = 8
            except Exception:
                pass

        opt_count = 0
        for mat in bpy.data.materials:
            if mat.name.startswith("Rbx_") and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.projection == 'BOX':
                        node.projection_blend = 0.0
                        opt_count += 1

        # Only unused Roblox materials (leftover _pending/_stale builds). A full orphans_purge
        # would also delete the user's own unused data in this .blend.
        unused = [m for m in bpy.data.materials if m.name.startswith("Rbx_") and m.users == 0]
        for mat in unused:
            try:
                bpy.data.materials.remove(mat)
            except Exception:
                pass

        self.report({'INFO'}, f"Viewport optimized! Real-time playback enabled, {opt_count} shaders streamlined.")
        return {'FINISHED'}


class ROBLOX_OT_toggle_collection(Operator):
    bl_idname = "roblox.toggle_collection"
    bl_label = "Toggle Collection"
    bl_description = "Show or hide collection in viewport to boost performance"

    col_name: bpy.props.StringProperty(name="Collection Name")

    def execute(self, context):
        col = bpy.data.collections.get(self.col_name)
        if col:
            col.hide_viewport = not col.hide_viewport
            state = "Hidden" if col.hide_viewport else "Visible"
            self.report({'INFO'}, f"Collection '{col.name}' is now {state}")
        return {'FINISHED'}


class ROBLOX_OT_add_primitive(Operator):
    bl_idname = "roblox.add_primitive"
    bl_label = "Add Primitive"
    bl_description = "Add a primitive part and sync it to Roblox Studio"

    shape: bpy.props.EnumProperty(
        name="Shape",
        items=[
            ("Block", "Block", "Add a rectangular block (Cube)"),
            ("Ball", "Sphere", "Add a spherical part (Ball)"),
            ("Cylinder", "Cylinder", "Add a cylinder part"),
            ("Wedge", "Wedge", "Add a wedge part"),
        ],
        default="Block"
    )

    def execute(self, context):
        obj = sync_manager.create_primitive(self.shape)
        self.report({'INFO'}, f"Added Roblox {self.shape} ({obj.name})")
        return {'FINISHED'}


class VIEW3D_PT_roblox_livelink(Panel):
    bl_label = "Roblox Live Link"
    bl_idname = "VIEW3D_PT_roblox_livelink"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Roblox'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.roblox_livelink_settings
        is_running = server.is_running()

        layout.label(text=f"Beta {VERSION_LABEL} - still being tested", icon='EXPERIMENTAL')
        if not paths.online_access_allowed():
            warn = layout.box()
            warn.label(text="Online access is off:", icon='ERROR')
            warn.label(text="Roblox meshes/textures won't download")
            warn.label(text="(Preferences > System > Network)")

        # Status box
        box = layout.box()
        row = box.row()
        if is_running:
            row.label(text=f"Status: ACTIVE (Port {server.get_port()})", icon='RADIOBUT_ON')
            box.operator("roblox.stop_livelink", text="Stop Server", icon='PAUSE')
        else:
            row.label(text="Status: OFFLINE", icon='RADIOBUT_OFF')
            box.prop(settings, "port")
            box.operator("roblox.start_livelink", text="Start Server", icon='PLAY')

        layout.separator()

        # Display & Performance box (placed prominently at top so visible without scrolling)
        box_view = layout.box()
        box_view.label(text="Viewport & Playback FPS", icon='TIME')
        box_view.operator("roblox.optimize_viewport", text="Boost Playback FPS (60+)", icon='FORWARD')
        box_view.operator("roblox.set_material_view", text="Show Textures & Colors", icon='MATERIAL')
        box_view.prop(settings, "match_roblox_colors")

        # Quick Collection Visibility Toggles for Instant FPS Boost
        root_col = bpy.data.collections.get(sync_manager.COLLECTION_NAME)
        if root_col and len(root_col.children) > 0:
            box_view.separator()
            box_view.label(text="Hide/Show Background (FPS):", icon='HIDE_OFF')
            for c in root_col.children:
                total_c_objs = len(c.objects)
                r = box_view.row(align=True)
                eye_icon = 'HIDE_ON' if c.hide_viewport else 'HIDE_OFF'
                btn_txt = f"{c.name} ({total_c_objs})"
                op = r.operator("roblox.toggle_collection", text=btn_txt, icon=eye_icon)
                op.col_name = c.name

        layout.separator()

        # Sync Progress & Stats Box
        status_info = sync_manager.get_sync_status()
        box_stats = layout.box()
        box_stats.label(text="Sync Progress & Stats", icon='SORTTIME')

        if status_info["in_progress"]:
            row_cnt = box_stats.row()
            row_cnt.label(text=f"Syncing: {status_info['current']} / {status_info['total']}", icon='TIME')
            pct = status_info["pct"]
            row_pct = box_stats.row()
            row_pct.label(text=f"Progress: {pct:.1f}% ({status_info['pending']} left)")
            # Visual ASCII / Block progress bar in UI
            bar_len = 22
            filled = int((pct / 100.0) * bar_len)
            bar_str = "[" + "█" * filled + "░" * (bar_len - filled) + "]"
            row_bar = box_stats.row()
            row_bar.label(text=bar_str)
        else:
            row_done = box_stats.row()
            row_done.label(text=f"Objects in Blender: {status_info['total_objects']}", icon='CHECKMARK')

        csg_col = bpy.data.collections.get(sync_manager.CSG_COLLECTION_NAME)
        if csg_col and len(csg_col.objects) > 0:
            box_csg = layout.box()
            box_csg.label(text=f"CSG / Slabs ({len(csg_col.objects)})", icon='MOD_BOOLEAN')
            btn_text = "Show CSG Slabs" if csg_col.hide_viewport else "Hide CSG Slabs"
            btn_icon = 'HIDE_OFF' if csg_col.hide_viewport else 'HIDE_ON'
            box_csg.operator("roblox.toggle_csg", text=btn_text, icon=btn_icon)

        layout.separator()

        # Add Primitives Box
        box_prim = layout.box()
        box_prim.label(text="Add Primitive (to Studio)", icon='MESH_CUBE')
        col_grid = box_prim.column(align=True)
        row1 = col_grid.row(align=True)
        op_block = row1.operator("roblox.add_primitive", text="Block", icon='MESH_CUBE')
        op_block.shape = "Block"
        op_ball = row1.operator("roblox.add_primitive", text="Sphere", icon='MESH_UVSPHERE')
        op_ball.shape = "Ball"
        row2 = col_grid.row(align=True)
        op_cyl = row2.operator("roblox.add_primitive", text="Cylinder", icon='MESH_CYLINDER')
        op_cyl.shape = "Cylinder"
        op_wedge = row2.operator("roblox.add_primitive", text="Wedge", icon='OUTLINER_OB_MESH')
        op_wedge.shape = "Wedge"

        layout.separator()

        # Material & UV Scale box
        box_mat = layout.box()
        box_mat.label(text="Materials & UV Scale", icon='MATERIAL')
        box_mat.prop(settings, "tex_scale", text="Texture Scale")
        box_mat.operator("roblox.refresh_materials", text="Apply Texture Scale", icon='FILE_REFRESH')

        layout.separator()

        # Tools box
        box_tools = layout.box()
        box_tools.label(text="Scene Management", icon='OUTLINER_COLLECTION')
        box_tools.operator("roblox.clear_livelink", text="Clear Synced Objects", icon='TRASH')
        box_tools.operator("roblox.reload_addon", text="Reload Addon Code", icon='FILE_REFRESH')


classes = (
    RobloxLiveLinkSettings,
    ROBLOX_OT_start_livelink,
    ROBLOX_OT_stop_livelink,
    ROBLOX_OT_clear_livelink,
    ROBLOX_OT_set_material_view,
    ROBLOX_OT_optimize_viewport,
    ROBLOX_OT_toggle_collection,
    ROBLOX_OT_reload_addon,
    ROBLOX_OT_toggle_csg,
    ROBLOX_OT_refresh_materials,
    ROBLOX_OT_add_primitive,
    VIEW3D_PT_roblox_livelink,
)


def register():
    try:
        copied = paths.migrate_legacy_cache()
        if copied:
            print(f"[Roblox Live Link] Moved {copied} cached assets from the old add-on install")
    except Exception as e:
        print(f"[Roblox Live Link] Cache migration skipped: {e}")
    for cls in classes:
        if not hasattr(bpy.types, cls.__name__):
            try:
                bpy.utils.register_class(cls)
            except Exception:
                pass
    if not hasattr(bpy.types.Scene, "roblox_livelink_settings"):
        bpy.types.Scene.roblox_livelink_settings = bpy.props.PointerProperty(type=RobloxLiveLinkSettings)


def unregister():
    sync_manager.stop_sync_timer()
    if server.is_running():
        server.stop_server()

    if hasattr(bpy.types.Scene, "roblox_livelink_settings"):
        del bpy.types.Scene.roblox_livelink_settings
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
