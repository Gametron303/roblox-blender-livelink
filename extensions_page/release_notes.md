Code clean-up to follow the Blender Extensions guidelines, plus a few fixes.

- R6 heads (SpecialMesh of type Head) now arrive as the real Roblox head mesh with the face decal, instead of a box.
- SpecialMesh types Sphere and Wedge are built as a sphere and a wedge; Brick respects SpecialMesh.Scale (also inside merged models).
- No threads: the add-on no longer uses the `threading` or `queue` modules. The local HTTP server is polled from a Blender timer (non-blocking), and Roblox meshes/textures are downloaded in separate processes started with `subprocess`.
- macOS: Roblox Studio fonts and built-in meshes are now also found in `RobloxStudio.app`.
- Downloads identify themselves with the extension's own User-Agent.
- The Roblox Studio plugin is no longer packed inside the extension zip; all plugin source comments are in English.

1.3.0-beta: first public release (beta, still being tested).

- Live two-way sync between Roblox Studio and Blender: Studio → Blender syncs everything; Blender → Studio sends move/rotate/scale, collection moves and Add Primitive parts
- Decals, Textures and fully rendered SurfaceGuis (text, frames, images, strokes) on the correct faces
- Procedural versions of all built-in Roblox materials (no bundled textures), MaterialVariant support
- Meshes downloaded from Roblox and cached; downloads only with *Allow Online Access* enabled
- Pillow bundled for SurfaceGui/text rendering

Get the Roblox Studio plugin from the [Creator Store](https://create.roblox.com/store/asset/113147868123756).
macOS builds are not tested yet. Please report bugs on [GitHub Issues](https://github.com/Gametron303/roblox-blender-livelink/issues).
