# Roblox Studio Live Link — 1.3.1-beta

> ⚠️ **Beta — still being tested.** Expect bugs, and behavior may change between versions.
> Save your place and your `.blend` file before working. Bug reports are very welcome
> (ideally with a screenshot plus the Roblox Studio Output and the Blender console text):
> [GitHub Issues](https://github.com/Gametron303/roblox-blender-livelink/issues).

Live link between **Roblox Studio** and **Blender**. Parts, models, meshes, materials, decals and
SurfaceGui text from Studio appear in Blender and update in real time. Moving, rotating and scaling
objects in Blender sends the change back to Studio.

The extension runs a small local server (`127.0.0.1:28888`). A companion Roblox Studio plugin
(installed separately, from the Roblox Creator Store) sends the scene to it and applies changes coming back from Blender.

## Features

- **Live sync** of parts, wedges, balls, cylinders, MeshParts and SpecialMeshes (meshes are
  downloaded from Roblox and cached).
- **All Roblox materials** (Brick, Wood, WoodPlanks, Grass, Concrete, Slate, Cobblestone, Metal, Neon...)
  as **procedural** shader-node materials tinted by the part color: no bundled images, close to Roblox but
  not identical. **MaterialVariants** and MaterialService overrides use their own textures from your place.
- **Decals, Textures and SurfaceGuis on the right faces**, any number per part, layered like in Roblox.
  SurfaceGuis are rendered completely: Frames, TextLabels, ImageLabels, UIStroke, UICorner,
  TextScaled, word wrap and sprite sheets (ImageRect), using the fonts of your Roblox Studio install.
  Non-Latin text such as Cyrillic is supported.
- **Merged models**: buildings and props arrive as one mesh each to keep the scene light.
  Any model can be split into separate parts with one click.
- **Folders become collections**, and moving an object to another collection moves it in Studio too.
- **One Ctrl+Z in Studio** undoes a change made from Blender.
- **Match Roblox Colors**: switches the view transform to *Standard*, since AgX washes out part colors.

## What syncs in which direction

| Direction | What is synced |
|---|---|
| Studio → Blender | Everything: parts, models, meshes, materials, colors, transparency, decals, textures, text, objects being added or deleted |
| Blender → Studio | Move, rotate and scale, moving between collections (folders), and new parts made with **Add Primitive** |

**Not** sent from Blender to Studio: deleting, colors/materials/textures, geometry edits
(Edit Mode, modifiers), and regular Blender objects not made with Add Primitive.

## Requirements

- Blender **4.2** or newer
- Roblox Studio on **Windows** or **macOS**. Windows is tested; the macOS builds are provided but
  have **not been tested yet**.
- **Allow Online Access** enabled in Blender (*Preferences → System → Network*) to download Roblox
  meshes and textures. Without it the sync still works, but meshes appear as boxes and downloaded
  images are missing.

## Installation

### 1. Blender extension

1. Install the extension (drag the `.zip` into Blender, or *Edit → Preferences → Get Extensions →
   ⌄ → Install from Disk…*). Pick the zip for your system: `windows_x64`, `macos_arm64` (Apple
   Silicon) or `macos_x64` (Intel).
2. If you used an older non-extension version of this add-on, disable and remove it first.
   Otherwise both try to use port 28888. Assets it already downloaded are moved over automatically.

### 2. Roblox Studio plugin

**Recommended — Roblox Creator Store:** open [Blender Live Link (Beta)](https://create.roblox.com/store/asset/113147868123756), click **Get** and
install it in Studio. Updates arrive automatically.

**Manual (fallback):** copy `RobloxLiveLink.lua` from the
[GitHub release](https://github.com/Gametron303/roblox-blender-livelink/releases/latest) into the local plugins folder:
- Windows: `%LOCALAPPDATA%\Roblox\Plugins\`
- macOS: `~/Documents/Roblox/Plugins/`
> Keep the `.lua` extension: Studio does not load `.luau` files from this folder.

> ⚠️ Don't install both: if you use the Creator Store version, delete a local `RobloxLiveLink.lua`
> from the Plugins folder, otherwise two copies load and conflict.

Then restart Studio. A **Blender Bridge** toolbar with three buttons appears on the **Plugins** tab.
On first connect Studio asks for permission to send HTTP requests to `127.0.0.1`: click **Allow**.

## Quick start

1. In Blender press `N` in the 3D Viewport → **Roblox** tab → **Start Server**.
2. In Studio select what you want to bring over (a part, model or folder).
3. Click **Live Sync** on the Blender Bridge toolbar. Objects appear in Blender in the
   `Roblox_LiveLink` collection; everything you select afterwards is added too.

### Studio toolbar

- **Live Sync**: turns syncing on and off.
- **Sync Mode**: what gets synced. *Selection* (default) is everything you selected since turning sync
  on. *BlenderSync* is only the `Workspace.BlenderSync` folder. *Workspace* is the whole Workspace,
  which is heavy on big places.
- **Separate**: sends the selected models as individual parts instead of one merged mesh, so their parts
  can be edited one by one from Blender. It sets the `LiveLink_SeparateParts` attribute on the object,
  so it is saved with the place and visible to Team Create collaborators.

### Studio attributes

| Attribute | On | Effect |
|---|---|---|
| `LiveLinkIgnore = true` | a part | Not synced |
| `LiveLink_SeparateParts = true` | a model or part | Same as the Separate button |
| `LiveLinkPort` | Workspace | Use when Blender's server runs on a port other than 28888 |
| `LiveLinkSync = true/false` | Workspace | Start or stop syncing from a script or the command bar |

## Known limitations

- **UnionOperations** arrive as boxes (the plugin cannot read union geometry). **Terrain** is not synced.
- Some Roblox assets can only be downloaded while signed in (HTTP 401). Such meshes stay boxes; images
  usually still load through Roblox's public thumbnails (up to 420×420).
- Lighting and post effects (Bloom, ColorCorrection, Atmosphere, sun shadows) are not transferred, so the
  picture differs from Studio in lighting, not in materials.
- Decals, Textures and SurfaceGuis are applied per face on block parts only. Balls, cylinders, wedges and
  MeshParts get a single texture.
- Procedural materials approximate Roblox's look; they are not pixel-identical.
- Semi-transparent parts look grainy in Material Preview (EEVEE dithering).
- A failed download is only retried after restarting Blender.

## Permissions and privacy

- **Network.** A local server on `127.0.0.1` that only Roblox Studio on the same computer talks to.
  Downloads of meshes and textures from Roblox's asset servers happen only while *Allow Online Access*
  is on. Nothing else is sent anywhere.
- **Files.** Reads fonts and textures from your Roblox Studio installation and caches downloaded assets
  in the extension's user folder.

## Textures and copyright

The extension **ships no Roblox textures or any other third-party images**. The surface pattern of
every built-in material (brick, wood, grass, concrete, stone...) is **generated procedurally** with
Blender shader nodes at sync time, to avoid any copyright infringement. That is why materials resemble
Roblox's but are not identical.

Images used by your own game (decals, textures, MaterialVariants, text) are downloaded from Roblox's
servers to your computer only while you work, and kept in a local cache. They are never part of the
extension package.

## License

GPL-3.0-or-later.

*Not affiliated with, endorsed or sponsored by Roblox Corporation. Roblox and Roblox Studio are
trademarks of Roblox Corporation.*

---

## About

This Blender extension and the Roblox Studio plugin were made entirely with AI. The first versions
(up to 1.2) were written in Google Antigravity; versions 1.3.0-beta and later (bug review and fixes, texture and
SurfaceGui import, procedural materials, the move to the Extension format, icons, documentation and
release packaging) were made with **Claude** (Anthropic's AI).
