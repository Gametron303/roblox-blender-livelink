--!strict
--[[
    Roblox Studio <-> Blender Live-Link Plugin
    High-Performance Real-Time Bridge with:
    - Selection-first sync mode for instant, lag-free model imports (additive: deselecting never deletes in Blender)
    - Full character support: R15/R6 body meshes, Hair & accessories, Shirt, Pants, Face decals
    - MeshPart & SpecialMesh 3D geometry & texture extraction (incl. SurfaceAppearance ColorMap)
    - Merged models stay live: part edits / model moves are re-sent to Blender
    - Bidirectional: parts and merged models moved in Blender update Studio (with Undo support)
    - Zero Studio freezing (no heavy CSG separation, batched change checks)
]]

local PLUGIN_VERSION = "1.3.0-beta"

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local Workspace = game:GetService("Workspace")
local Selection = game:GetService("Selection")
local ChangeHistoryService = game:GetService("ChangeHistoryService")

-- Плагин работает только в Edit-режиме (в Play-тесте Studio запускает копии плагина в клиенте/сервере)
if not RunService:IsEdit() then
    return
end

local DEFAULT_PORT = 28888
-- Порт сервера Blender: по умолчанию 28888, другой можно задать атрибутом Workspace "LiveLinkPort"
local function baseUrl(): string
    local port = Workspace:GetAttribute("LiveLinkPort")
    if typeof(port) ~= "number" or port < 1024 or port > 65535 then
        port = DEFAULT_PORT
    end
    return "http://127.0.0.1:" .. tostring(math.floor(port :: number))
end
local SYNC_INTERVAL = 0.20
local OFFLINE_RETRY_INTERVAL = 1.5
local FULL_SYNC_CHUNK_SIZE = 500
-- Сколько деталей за тик проверяется на "дорогие" изменения (текстуры, папки)
local EXPENSIVE_CHECKS_PER_TICK = 150
-- Минимальный интервал между повторными отправками одной и той же объединённой модели (при перетаскивании)
local MODEL_RESEND_INTERVAL = 0.4

-- Sync Modes: "Selection" (default & fastest), "BlenderSync" (folder only), "Workspace" (entire scene)
local syncMode: "Selection" | "BlenderSync" | "Workspace" = "Selection"

-- Models are merged into single meshes; a model/part marked with the "Separate" button
-- (attribute LiveLink_SeparateParts) is sent as individual parts instead
local importMode: "MergeModels" | "SeparateParts" = "MergeModels"

local isSyncing = false
local isFullSyncing = false
local isPosting = false
local tickBusy = false
local lastSyncTime = 0
local serverAvailable = false
local lastErrorTime = 0
local needInitialFullSync = true
-- Удалять ли в Blender объекты, которых нет в следующей полной синхронизации
local pruneNextFullSync = false
local currentSyncSession = 0
local serverSession: string? = nil

type PartState = {
    cf: CFrame,
    size: Vector3,
    color: Color3,
    transparency: number,
    material: string,
    texKey: string,
    folderKey: string,
}
local partStateCache: { [BasePart]: PartState } = {}
local trackedPartsMap: { [BasePart]: boolean } = {}
local trackedPartsList: { BasePart } = {}
local partByIdMap: { [string]: Instance } = {}

-- Selection mode: everything ever selected while syncing stays tracked until it is deleted in Studio
local selectionRoots: { Instance } = {}
-- Кэш: под каким из selectionRoots лежит деталь (чтобы не перебирать все корни каждый тик)
local rootOfPart: { [BasePart]: Instance } = setmetatable({}, { __mode = "k" }) :: any
-- Parts created from Blender stay tracked regardless of mode/selection
local blenderCreatedParts: { [BasePart]: boolean } = {}
-- MergeModels: which merged model a part belongs to, and models that must be re-sent
local compoundOfPart: { [BasePart]: Model } = {}
local compoundModelIds: { [Model]: string } = {}
local dirtyModels: { [Model]: boolean } = {}
local modelLastSent: { [Model]: number } = {}
local expensiveCursor = 1

-- Иконки кнопок (исходники PNG: roblox_plugin/icons). Встроенные rbxasset://textures/... пропадают
-- при обновлениях Studio, поэтому иконки загружены как собственные ассеты.
local ICONS = {
    syncToggle = "rbxassetid://125947218197808",
    modeSelection = "rbxassetid://137194228353891",
    modeFolder = "rbxassetid://122688952155954",
    modeWorkspace = "rbxassetid://139605180223543",
    separateSelected = "rbxassetid://92058259137021",
}

-- Create Plugin Toolbar & Buttons
local toolbar = plugin:CreateToolbar("Blender Bridge")

local toggleBtn = toolbar:CreateButton(
    "LiveLinkToggle",
    "Toggle Real-Time Sync to Blender",
    ICONS.syncToggle,
    "Live Sync"
)
toggleBtn.ClickableWhenViewportHidden = true

local modeBtn = toolbar:CreateButton(
    "LiveLinkMode",
    "Sync mode: Selection / BlenderSync folder / Workspace (click to switch)",
    ICONS.modeSelection,
    "Sync Mode"
)
modeBtn.ClickableWhenViewportHidden = true

local separateBtn = toolbar:CreateButton(
    "LiveLinkSeparate",
    "Toggle Separate Parts for Selected",
    ICONS.separateSelected,
    "Separate"
)
separateBtn.ClickableWhenViewportHidden = true

local function updateModeButtonLabel()
    if syncMode == "Selection" then
        modeBtn.Icon = ICONS.modeSelection
    elseif syncMode == "BlenderSync" then
        modeBtn.Icon = ICONS.modeFolder
    else
        modeBtn.Icon = ICONS.modeWorkspace
    end
end

local function getOrCreateId(instance: Instance): string
    local id = instance:GetAttribute("LiveLinkId")
    if not id or typeof(id) ~= "string" or id == "" or (partByIdMap[id] and partByIdMap[id] ~= instance and partByIdMap[id].Parent ~= nil) then
        -- Новый объект или копия (Ctrl+D копирует атрибут) — нужен собственный ID
        id = HttpService:GenerateGUID(false)
        instance:SetAttribute("LiveLinkId", id)
    end
    partByIdMap[id :: string] = instance
    return id :: string
end

-- Числовой ID ассета из "rbxassetid://123", "http://www.roblox.com/asset/?id=123", ".../v1/asset?id=123".
-- Простой поиск первой группы цифр здесь не годится: в "/v1/asset?id=123" он вернёт "1".
local function assetIdOf(content: string): string?
    return string.match(content, "[?&]id=(%d+)")
        or string.match(content, "rbxassetid://(%d+)")
        or string.match(content, "rbxthumb://.-id=(%d+)")
        or string.match(content, "^(%d+)$")
end

-- 32-битный FNV-1a: короткий стабильный хеш для любого текста (в т.ч. кириллицы)
local function hashString(s: string): string
    local h = 2166136261
    for i = 1, #s do
        h = bit32.bxor(h, string.byte(s, i))
        -- h * 16777619 по модулю 2^32 без потери точности double
        h = (bit32.lshift(h, 24) + bit32.lshift(h, 8) + bit32.lshift(h, 7) + bit32.lshift(h, 4) + bit32.lshift(h, 1) + h) % 4294967296
    end
    return string.format("%08x", h)
end

local function getPartShape(part: BasePart): string
    if part:IsA("MeshPart") then
        return "MeshPart"
    elseif part:IsA("WedgePart") then
        return "Wedge"
    elseif part:IsA("Part") then
        local p = part :: Part
        return p.Shape.Name
    end
    return "Block"
end

-- GetAppliedDescription() создаёт новую копию HumanoidDescription при каждом вызове — кэшируем
local descCache: { [Humanoid]: { desc: HumanoidDescription?, t: number } } = setmetatable({}, { __mode = "k" }) :: any

local function getAppliedDescriptionCached(hum: Humanoid): HumanoidDescription?
    local now = os.clock()
    local entry = descCache[hum]
    if entry and now - entry.t < 5 then
        return entry.desc
    end
    local ok, desc = pcall(function()
        return hum:GetAppliedDescription()
    end)
    local newDesc: HumanoidDescription? = if ok then desc else nil
    if entry and entry.desc and entry.desc ~= newDesc then
        pcall(function()
            (entry.desc :: HumanoidDescription):Destroy()
        end)
    end
    descCache[hum] = { desc = newDesc, t = now }
    return newDesc
end

local function getMeshTextureId(part: BasePart): string?
    -- SurfaceAppearance перекрывает TextureID при рендере
    local sa = part:FindFirstChildWhichIsA("SurfaceAppearance")
    if sa then
        local ok, colorMap = pcall(function()
            return (sa :: any).ColorMap
        end)
        if ok and typeof(colorMap) == "string" and colorMap ~= "" then
            return colorMap
        end
    end
    if part:IsA("MeshPart") then
        local tid = (part :: MeshPart).TextureID
        if tid ~= "" then
            return tid
        end
    end
    local specialMesh = part:FindFirstChildWhichIsA("SpecialMesh")
    if specialMesh and specialMesh.TextureId ~= "" then
        return specialMesh.TextureId
    end
    return nil
end

local function getPartTextureInfo(part: BasePart, meshTextureId: string?)
    -- 1. Explicit check for SpawnLocation
    if part:IsA("SpawnLocation") then
        return {
            type = "Decal",
            id = "SpawnLocation",
            face = "Top",
        }
    end

    -- 2. Character clothing and face handling
    local character = part:FindFirstAncestorWhichIsA("Model")
    if not character or not character:FindFirstChildWhichIsA("Humanoid") then
        local p = part.Parent
        if p and p:FindFirstChildWhichIsA("Humanoid") then
            character = p :: any
        elseif p and p.Parent and p.Parent:FindFirstChildWhichIsA("Humanoid") then
            character = p.Parent :: any
        end
    end
    if character and character:FindFirstChildWhichIsA("Humanoid") then
        local shirt = character:FindFirstChildWhichIsA("Shirt")
        local pants = character:FindFirstChildWhichIsA("Pants")
        local pName = part.Name

        -- Head face decal
        if pName == "Head" then
            local faceDecal = part:FindFirstChild("face") or part:FindFirstChildWhichIsA("Decal")
            if faceDecal and faceDecal:IsA("Decal") and faceDecal.Texture ~= "" and faceDecal.Transparency < 1 then
                local numId = assetIdOf(faceDecal.Texture)
                return {
                    type = "Face",
                    id = numId or faceDecal.Texture,
                    face = "Front",
                }
            end

            -- Check HumanoidDescription for Face or Dynamic Head
            local hum = character:FindFirstChildOfClass("Humanoid")
            local desc = hum and getAppliedDescriptionCached(hum)
            if desc then
                if desc.Face and desc.Face ~= 0 then
                    return {
                        type = "Face",
                        id = tostring(desc.Face),
                        face = "Front",
                    }
                elseif desc.Head and desc.Head ~= 0 then
                    -- Map dynamic head asset ID to classic face asset ID
                    local dynHeadToFace = {
                        [120354556756689] = "406001052", -- XD 2.0
                    }
                    local faceId = dynHeadToFace[desc.Head]
                    if faceId then
                        return {
                            type = "Face",
                            id = faceId,
                            face = "Front",
                        }
                    end
                end
            end

            if meshTextureId and meshTextureId ~= "" and not string.find(meshTextureId, "123750152452869") then
                local numId = assetIdOf(meshTextureId)
                return {
                    type = "MeshTexture",
                    id = numId or meshTextureId,
                }
            end
        end

        local isAccessory = (part.Parent and part.Parent:IsA("Accessory")) or pName == "Handle"
        if not isAccessory then
            -- Torso and Arms (Shirt)
            local isTorsoOrArm = pName == "UpperTorso" or pName == "Torso" or string.find(pName, "Arm") ~= nil or pName == "LeftHand" or pName == "RightHand"
            if isTorsoOrArm and shirt and shirt.ShirtTemplate ~= "" then
                local numId = assetIdOf(shirt.ShirtTemplate)
                return {
                    type = "Shirt",
                    id = numId or shirt.ShirtTemplate,
                }
            end

            -- LowerTorso / Legs (Pants)
            local isLegOrFoot = pName == "LowerTorso" or string.find(pName, "Leg") ~= nil or string.find(pName, "Foot") ~= nil
            if isLegOrFoot and pants and pants.PantsTemplate ~= "" then
                local numId = assetIdOf(pants.PantsTemplate)
                return {
                    type = "Pants",
                    id = numId or pants.PantsTemplate,
                }
            end

            -- Fallback LowerTorso to shirt if no pants
            if pName == "LowerTorso" and shirt and shirt.ShirtTemplate ~= "" then
                local numId = assetIdOf(shirt.ShirtTemplate)
                return {
                    type = "Shirt",
                    id = numId or shirt.ShirtTemplate,
                }
            end
        end
    end

    -- 3. Check for Texture (like Baseplate or tiled walls/doors)
    local tex = part:FindFirstChildWhichIsA("Texture")
    if tex and tex.Texture ~= "" and tex.Transparency < 1 then
        local numId = assetIdOf(tex.Texture)
        return {
            type = "Texture",
            id = numId or tex.Texture,
            studsU = tex.StudsPerTileU,
            studsV = tex.StudsPerTileV,
            transparency = tex.Transparency,
            face = tex.Face.Name,
        }
    end

    -- 4. Check for visible Decals (must have Transparency < 1)
    for _, ch in ipairs(part:GetChildren()) do
        if ch:IsA("Decal") and ch.Transparency < 1 and ch.Texture ~= "" then
            local numId = assetIdOf(ch.Texture)
            local assetId = numId
            if not assetId or assetId == "" then
                if string.find(ch.Texture, "SpawnLocation") then
                    assetId = "SpawnLocation"
                else
                    assetId = ch.Texture
                end
            end
            return {
                type = "Decal",
                id = assetId,
                face = ch.Face.Name,
            }
        end
    end

    -- 5. Check for MeshPart / SpecialMesh / SurfaceAppearance texture
    if meshTextureId and meshTextureId ~= "" then
        local numId = assetIdOf(meshTextureId)
        return {
            type = "MeshTexture",
            id = numId or meshTextureId,
        }
    end

    -- 6. Check for SurfaceGui with TextLabel or ImageLabel
    for _, ch in ipairs(part:GetChildren()) do
        if ch:IsA("SurfaceGui") and ch.Enabled then
            local textLabel = ch:FindFirstChildWhichIsA("TextLabel", true)
            if textLabel and textLabel.Visible and textLabel.Text ~= "" then
                local textColor = textLabel.TextColor3
                local bgColor = textLabel.BackgroundColor3
                -- Хеш по полному тексту и цветам: gsub("[^%w_]") вырезает кириллицу целиком,
                -- и разные русские надписи получали одинаковый ID (а в Blender — одну текстуру)
                local stableId = "txt_" .. hashString(string.format("%s|%.3f,%.3f,%.3f|%.3f,%.3f,%.3f|%.2f",
                    textLabel.Text, textColor.R, textColor.G, textColor.B,
                    bgColor.R, bgColor.G, bgColor.B, textLabel.BackgroundTransparency))
                return {
                    type = "SurfaceGui",
                    id = stableId,
                    face = ch.Face.Name,
                    text = textLabel.Text,
                    textColor = { textColor.R, textColor.G, textColor.B },
                    bgColor = { bgColor.R, bgColor.G, bgColor.B },
                    bgTrans = textLabel.BackgroundTransparency,
                }
            end
            local imgLabel = ch:FindFirstChildWhichIsA("ImageLabel", true)
            if imgLabel and imgLabel.Visible and imgLabel.Image ~= "" then
                local numId = assetIdOf(imgLabel.Image)
                return {
                    type = "Decal",
                    id = numId or imgLabel.Image,
                    face = ch.Face.Name,
                }
            end
        end
    end

    return nil
end

local function texKeyOf(texInfo: any): string
    if not texInfo then
        return ""
    end
    return tostring(texInfo.type) .. "_" .. tostring(texInfo.id) .. "_" .. tostring(texInfo.face or "")
end

--------------------------------------------------------------------------------
-- Слои поверхности детали: все Decal, Texture и SurfaceGui (на каждой грани их может быть несколько).
-- Blender раскладывает их по граням и "запекает" декали и GUI в текстуру грани.
--------------------------------------------------------------------------------

local function color3Arr(c: Color3): { number }
    return { c.R, c.G, c.B }
end

-- UIStroke в режиме Contextual на текстовом объекте обводит текст, в остальных случаях — рамку
local function getStrokes(obj: GuiObject, isText: boolean): (any, any)
    local textStroke, borderStroke = nil, nil
    for _, ch in ipairs(obj:GetChildren()) do
        if ch:IsA("UIStroke") and ch.Enabled and ch.Transparency < 1 and ch.Thickness > 0 then
            local s = { c = color3Arr(ch.Color), t = ch.Transparency, th = ch.Thickness }
            if isText and ch.ApplyStrokeMode == Enum.ApplyStrokeMode.Contextual then
                textStroke = s
            else
                borderStroke = s
            end
        end
    end
    return textStroke, borderStroke
end

local function cornerRadiusOf(obj: GuiObject): number
    local corner = obj:FindFirstChildWhichIsA("UICorner")
    if not corner then
        return 0
    end
    local size = obj.AbsoluteSize
    return corner.CornerRadius.Scale * math.min(size.X, size.Y) + corner.CornerRadius.Offset
end

local function serializeGuiObject(obj: GuiObject, origin: Vector2): any?
    local pos = obj.AbsolutePosition - origin
    local size = obj.AbsoluteSize
    if size.X <= 0 or size.Y <= 0 then
        return nil
    end
    local isText = obj:IsA("TextLabel") or obj:IsA("TextButton") or obj:IsA("TextBox")
    local isImage = obj:IsA("ImageLabel") or obj:IsA("ImageButton")
    local textStroke, borderStroke = getStrokes(obj, isText)
    local el: any = {
        x = pos.X, y = pos.Y, w = size.X, h = size.Y,
        bg = color3Arr(obj.BackgroundColor3), bgT = obj.BackgroundTransparency,
        r = cornerRadiusOf(obj),
        border = borderStroke,
    }
    local hasContent = obj.BackgroundTransparency < 1 or borderStroke ~= nil

    if isText then
        local t = obj :: any
        local text: string = if t.RichText then t.ContentText else t.Text
        if text ~= "" and t.TextTransparency < 1 then
            if not textStroke and t.TextStrokeTransparency < 1 then
                textStroke = { c = color3Arr(t.TextStrokeColor3), t = t.TextStrokeTransparency, th = 1 }
            end
            local constraint = obj:FindFirstChildWhichIsA("UITextSizeConstraint")
            el.text = text
            el.tc = color3Arr(t.TextColor3)
            el.tt = t.TextTransparency
            el.ts = t.TextSize
            el.scaled = t.TextScaled
            el.maxTs = if constraint then constraint.MaxTextSize else nil
            el.minTs = if constraint then constraint.MinTextSize else nil
            el.wrap = t.TextWrapped
            el.xa = t.TextXAlignment.Name
            el.ya = t.TextYAlignment.Name
            el.lh = t.LineHeight
            el.font = { f = t.FontFace.Family, w = t.FontFace.Weight.Value, s = t.FontFace.Style.Name }
            el.stroke = textStroke
            hasContent = true
        end
    elseif isImage then
        local img = obj :: any
        if img.Image ~= "" and img.ImageTransparency < 1 then
            el.img = img.Image
            el.ic = color3Arr(img.ImageColor3)
            el.it = img.ImageTransparency
            el.st = img.ScaleType.Name
            if img.ImageRectSize.X > 0 and img.ImageRectSize.Y > 0 then
                el.rect = { img.ImageRectOffset.X, img.ImageRectOffset.Y, img.ImageRectSize.X, img.ImageRectSize.Y }
            end
            hasContent = true
        end
    end
    return if hasContent then el else nil
end

-- Порядок отрисовки как в Roblox (ZIndexBehavior.Sibling): родитель, затем дети по ZIndex
local function collectGuiElements(container: Instance, origin: Vector2, out: { any })
    local kids: { GuiObject } = {}
    for _, ch in ipairs(container:GetChildren()) do
        if ch:IsA("GuiObject") then
            table.insert(kids, ch)
        end
    end
    local order: { [GuiObject]: number } = {}
    for i, ch in ipairs(kids) do
        order[ch] = i
    end
    table.sort(kids, function(a, b)
        if a.ZIndex ~= b.ZIndex then
            return a.ZIndex < b.ZIndex
        end
        return order[a] < order[b]
    end)
    for _, ch in ipairs(kids) do
        if ch.Visible then
            local el = serializeGuiObject(ch, origin)
            if el then
                table.insert(out, el)
            end
            collectGuiElements(ch, origin, out)
        end
    end
end

-- Размер холста SurfaceGui в пикселях GUI (AbsoluteSize, а если он ещё не посчитан — по настройкам)
local function surfaceGuiCanvas(gui: SurfaceGui, part: BasePart): Vector2
    local abs = gui.AbsoluteSize
    if abs.X > 0 and abs.Y > 0 then
        return abs
    end
    if gui.SizingMode == Enum.SurfaceGuiSizingMode.PixelsPerStud then
        local s = part.Size
        local face = gui.Face
        local w, h
        if face == Enum.NormalId.Top or face == Enum.NormalId.Bottom then
            w, h = s.X, s.Z
        elseif face == Enum.NormalId.Left or face == Enum.NormalId.Right then
            w, h = s.Z, s.Y
        else
            w, h = s.X, s.Y
        end
        return Vector2.new(w * gui.PixelsPerStud, h * gui.PixelsPerStud)
    end
    return gui.CanvasSize
end

local function getSurfaceLayers(part: BasePart): ({ any }?, string)
    local layers = {}
    for _, ch in ipairs(part:GetChildren()) do
        if ch:IsA("Texture") then
            if ch.Texture ~= "" and ch.Transparency < 1 then
                table.insert(layers, {
                    kind = "texture", face = ch.Face.Name, img = ch.Texture,
                    c = color3Arr(ch.Color3), t = ch.Transparency, z = ch.ZIndex,
                    su = ch.StudsPerTileU, sv = ch.StudsPerTileV,
                    ou = ch.OffsetStudsU, ov = ch.OffsetStudsV,
                })
            end
        elseif ch:IsA("Decal") then
            if ch.Texture ~= "" and ch.Transparency < 1 then
                table.insert(layers, {
                    kind = "decal", face = ch.Face.Name, img = ch.Texture,
                    c = color3Arr(ch.Color3), t = ch.Transparency, z = ch.ZIndex,
                })
            end
        elseif ch:IsA("SurfaceGui") and ch.Enabled and (ch.Adornee == nil or ch.Adornee == part) then
            local canvas = surfaceGuiCanvas(ch, part)
            local elements = {}
            collectGuiElements(ch, ch.AbsolutePosition, elements)
            if #elements > 0 and canvas.X > 0 and canvas.Y > 0 then
                table.insert(layers, {
                    kind = "gui", face = ch.Face.Name, w = canvas.X, h = canvas.Y, elements = elements,
                })
            end
        end
    end
    if #layers == 0 then
        return nil, ""
    end
    local ok, json = pcall(function()
        return HttpService:JSONEncode(layers)
    end)
    return layers, if ok then hashString(json) else tostring(#layers)
end

-- Ключ "дорогих" изменений детали: старый texture_info + все слои поверхности
local function partTexKey(part: BasePart, texInfo: any?): string
    local _, sig = getSurfaceLayers(part)
    return texKeyOf(texInfo or getPartTextureInfo(part, getMeshTextureId(part))) .. "#" .. sig
end

local function getFolderCollectionPath(inst: Instance): { string }
    local path = {}
    local syncFolder = Workspace:FindFirstChild("BlenderSync")
    local stopAt: Instance = (syncFolder and (syncFolder:IsA("Folder") or syncFolder:IsA("Model"))) and syncFolder or Workspace
    local cur = inst.Parent
    while cur and cur ~= stopAt and cur ~= Workspace and cur ~= game do
        if cur:IsA("Folder") or cur:IsA("Model") then
            table.insert(path, 1, cur.Name)
        end
        cur = cur.Parent
    end
    return path
end

-- Материал вместе с MaterialVariant: смена варианта тоже должна уходить в Blender
local function materialKeyOf(part: BasePart): string
    return part.Material.Name .. "/" .. part.MaterialVariant
end

local function makePartState(part: BasePart, texKey: string, folderKey: string): PartState
    return {
        cf = part.CFrame,
        size = part.Size,
        color = part.Color,
        transparency = part.Transparency,
        material = materialKeyOf(part),
        texKey = texKey,
        folderKey = folderKey,
    }
end

local function getFallbackMesh(part: BasePart): string?
    local n = part.Name
    if n == "Head" then
        return "head"
    elseif n == "Torso" then
        return "torso"
    elseif n == "Left Arm" then
        return "leftarm"
    elseif n == "Right Arm" then
        return "rightarm"
    elseif n == "Left Leg" then
        return "leftleg"
    elseif n == "Right Leg" then
        return "rightleg"
    end
    return nil
end

-- Mesh data shared by individual and merged serialization
local function getMeshData(part: BasePart): (string?, { number }?, { number }?)
    local meshId: string? = nil
    local meshScale: { number }? = nil
    local meshOffset: { number }? = nil
    if part:IsA("MeshPart") then
        local mid = (part :: MeshPart).MeshId
        if mid ~= "" then
            meshId = mid
        end
    end
    local specialMesh = part:FindFirstChildWhichIsA("SpecialMesh")
    if specialMesh then
        if specialMesh.MeshId ~= "" then
            meshId = specialMesh.MeshId
        end
        meshScale = { specialMesh.Scale.X, specialMesh.Scale.Y, specialMesh.Scale.Z }
        meshOffset = { specialMesh.Offset.X, specialMesh.Offset.Y, specialMesh.Offset.Z }
    end
    return meshId, meshScale, meshOffset
end

-- MaterialVariant детали: заданный на самой детали или переопределение базового материала в MaterialService
local MaterialService = game:GetService("MaterialService")
local function getMaterialVariantData(part: BasePart): any?
    local name = part.MaterialVariant
    if name == "" then
        local ok, override = pcall(function()
            return MaterialService:GetBaseMaterialOverride(part.Material)
        end)
        -- Без переопределения возвращается имя самого материала
        if ok and typeof(override) == "string" and override ~= "" and override ~= part.Material.Name then
            name = override
        end
    end
    if name == "" then
        return nil
    end
    local mv = MaterialService:FindFirstChild(name, true)
    if not (mv and mv:IsA("MaterialVariant")) then
        return nil
    end
    return {
        name = mv.Name,
        baseMaterial = mv.BaseMaterial.Name,
        colorMap = mv.ColorMap,
        normalMap = mv.NormalMap,
        roughnessMap = mv.RoughnessMap,
        metalnessMap = mv.MetalnessMap,
        studsPerTile = mv.StudsPerTile,
    }
end

local function serializePart(part: BasePart)
    local cf = part.CFrame
    local x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22 = cf:GetComponents()
    local color = part.Color
    local isOp = part:IsA("PartOperation")
    local folderPath = getFolderCollectionPath(part)

    local meshId, meshScale, meshOffset = getMeshData(part)
    local textureId = getMeshTextureId(part)
    local texInfo = getPartTextureInfo(part, textureId)
    local surfaces, surfacesSig = getSurfaceLayers(part)
    local matVariantData = getMaterialVariantData(part)

    return {
        id = getOrCreateId(part),
        name = part.Name,
        className = part.ClassName,
        shape = getPartShape(part),
        mesh_id = meshId,
        texture_id = textureId,
        mesh_scale = meshScale,
        mesh_offset = meshOffset,
        fallback_mesh = getFallbackMesh(part),
        position = { x, y, z },
        rotation = { r00, r01, r02, r10, r11, r12, r20, r21, r22 },
        size = { part.Size.X, part.Size.Y, part.Size.Z },
        color = { color.R, color.G, color.B },
        transparency = part.Transparency,
        material = part.Material.Name,
        materialVariant = matVariantData,
        reflectance = part.Reflectance,
        texture_info = texInfo,
        surfaces = surfaces,
        surfaces_sig = surfacesSig,
        folder_path = folderPath,
        isOperation = isOp,
    }
end

local function isSeparateModelOrPart(inst: Instance): boolean
    if inst:GetAttribute("LiveLink_SeparateParts") == true then
        return true
    end
    if inst:IsA("Model") and inst:FindFirstChildWhichIsA("Humanoid") then
        return true
    end
    local ancestorModel = inst:FindFirstAncestorWhichIsA("Model")
    if ancestorModel and (ancestorModel:FindFirstChildWhichIsA("Humanoid") or ancestorModel:GetAttribute("LiveLink_SeparateParts") == true) then
        return true
    end
    return false
end

local function getTopModel(part: BasePart, selectedRoots: { Instance }?): Model?
    if importMode ~= "MergeModels" then
        return nil
    end
    if blenderCreatedParts[part] or isSeparateModelOrPart(part) then
        return nil
    end

    if selectedRoots and #selectedRoots > 0 then
        for _, sel in ipairs(selectedRoots) do
            if sel:IsA("Model") and (part == sel or part:IsDescendantOf(sel)) then
                if not isSeparateModelOrPart(sel) then
                    return sel :: Model
                end
            end
        end
    end

    local cur: Instance? = part.Parent
    local top: Model? = nil
    while cur and cur ~= Workspace and not cur:IsA("Workspace") do
        if cur:IsA("Model") then
            if isSeparateModelOrPart(cur) then
                return nil
            end
            top = cur :: Model
        end
        cur = cur.Parent
    end
    return top
end

local function mergeRoots(): { Instance }?
    return if syncMode == "Selection" then selectionRoots else nil
end

-- Frame used for a merged model in Blender: PrimaryPart CFrame or bounding box CFrame
local function getModelFrame(model: Model): CFrame
    if model.PrimaryPart then
        return model.PrimaryPart.CFrame
    end
    local cf = model:GetBoundingBox()
    return cf
end

-- forward declaration (defined below isEligiblePart)
local addSelectionRoots: (instances: { Instance }) -> boolean

local function serializeCompoundModel(model: Model): any
    if syncMode == "Selection" then
        -- Выделили деталь внутри модели: в Blender уходит вся модель, значит и отслеживать её нужно целиком
        addSelectionRoots({ model })
    end
    local _, size = model:GetBoundingBox()
    local modelCf = getModelFrame(model)
    local comps = { modelCf:GetComponents() }
    local invModelCf = modelCf:Inverse()

    local partsList = {}
    for _, desc in ipairs(model:GetDescendants()) do
        if desc:IsA("BasePart") then
            local part = desc :: BasePart
            if part:GetAttribute("LiveLink_SeparateParts") == true or part:GetAttribute("LiveLinkIgnore") then
                continue
            end

            local meshId, meshScale, meshOffset = getMeshData(part)
            local texInfo = getPartTextureInfo(part, getMeshTextureId(part))
            local relCf = invModelCf * part.CFrame
            local rComps = { relCf:GetComponents() }

            -- Детали модели отслеживаются, но в Blender уходят только как часть общей модели
            compoundOfPart[part] = model
            if not trackedPartsMap[part] then
                trackedPartsMap[part] = true
                table.insert(trackedPartsList, part)
            end
            local surfaces, surfacesSig = getSurfaceLayers(part)
            partStateCache[part] = makePartState(part, texKeyOf(texInfo) .. "#" .. surfacesSig, table.concat(getFolderCollectionPath(part), "/"))

            table.insert(partsList, {
                name = part.Name,
                shape = getPartShape(part),
                rel_pos = { relCf.Position.X, relCf.Position.Y, relCf.Position.Z },
                rel_rot = { rComps[4], rComps[5], rComps[6], rComps[7], rComps[8], rComps[9], rComps[10], rComps[11], rComps[12] },
                size = { part.Size.X, part.Size.Y, part.Size.Z },
                color = { part.Color.R, part.Color.G, part.Color.B },
                transparency = part.Transparency,
                material = part.Material.Name,
                reflectance = part.Reflectance,
                mesh_id = meshId,
                mesh_scale = meshScale,
                mesh_offset = meshOffset,
                fallback_mesh = getFallbackMesh(part),
                texture_info = texInfo,
                surfaces = surfaces,
                materialVariant = getMaterialVariantData(part),
            })
        end
    end

    local id = getOrCreateId(model)
    compoundModelIds[model] = id
    modelLastSent[model] = os.clock()
    return {
        id = id,
        name = model.Name,
        className = "Model",
        is_compound = true,
        folder_path = getFolderCollectionPath(model),
        position = { modelCf.Position.X, modelCf.Position.Y, modelCf.Position.Z },
        rotation = { comps[4], comps[5], comps[6], comps[7], comps[8], comps[9], comps[10], comps[11], comps[12] },
        size = { size.X, size.Y, size.Z },
        parts = partsList,
    }
end

local function isEligiblePart(inst: Instance): boolean
    if not inst:IsA("BasePart") or inst:IsA("Terrain") then
        return false
    end
    if inst:GetAttribute("LiveLinkIgnore") or inst.Name == "LiveLink_CSG_Temp" then
        return false
    end
    if not inst:IsDescendantOf(Workspace) then
        return false
    end
    local cam = Workspace.CurrentCamera
    if cam and inst:IsDescendantOf(cam) then
        return false
    end
    if blenderCreatedParts[inst :: BasePart] then
        return true
    end

    if syncMode == "Selection" then
        -- Быстрый путь: корень, под которым деталь была найдена в прошлый раз
        local cachedRoot = rootOfPart[inst :: BasePart]
        if cachedRoot and cachedRoot.Parent and (inst == cachedRoot or inst:IsDescendantOf(cachedRoot)) then
            return true
        end
        for _, root in ipairs(selectionRoots) do
            if inst == root or inst:IsDescendantOf(root) then
                rootOfPart[inst :: BasePart] = root
                return true
            end
        end
        rootOfPart[inst :: BasePart] = nil
        return false
    end

    local syncFolder = Workspace:FindFirstChild("BlenderSync")
    if syncFolder and (syncFolder:IsA("Folder") or syncFolder:IsA("Model")) then
        return inst:IsDescendantOf(syncFolder)
    end
    -- BlenderSync mode without the folder syncs nothing; Workspace mode syncs everything
    return syncMode == "Workspace"
end

local function trackPart(part: BasePart)
    if not trackedPartsMap[part] then
        trackedPartsMap[part] = true
        table.insert(trackedPartsList, part)
    end
end

local function trackSubtree(root: Instance)
    if root:IsA("BasePart") and isEligiblePart(root) then
        trackPart(root :: BasePart)
    end
    for _, desc in ipairs(root:GetDescendants()) do
        if desc:IsA("BasePart") and isEligiblePart(desc) then
            trackPart(desc :: BasePart)
        end
    end
end

local function rebuildTrackedParts()
    table.clear(trackedPartsMap)
    table.clear(trackedPartsList)

    if syncMode == "Selection" then
        for _, root in ipairs(selectionRoots) do
            if root.Parent then
                trackSubtree(root)
            end
        end
    else
        local syncFolder = Workspace:FindFirstChild("BlenderSync")
        local root: Instance = (syncFolder and (syncFolder:IsA("Folder") or syncFolder:IsA("Model"))) and syncFolder or Workspace
        trackSubtree(root)
    end
    for part in pairs(blenderCreatedParts) do
        if part.Parent and isEligiblePart(part) then
            trackPart(part)
        end
    end
end

-- Adds instances to the Selection-mode roots (skipping ones already covered by a root)
function addSelectionRoots(instances: { Instance }): boolean
    local added = false
    for _, inst in ipairs(instances) do
        if inst:IsDescendantOf(Workspace) and inst ~= Workspace and not inst:IsA("Terrain") then
            local covered = false
            for _, root in ipairs(selectionRoots) do
                if inst == root or inst:IsDescendantOf(root) then
                    covered = true
                    break
                end
            end
            if not covered then
                -- Новый корень поглощает уже добавленные вложенные корни
                for i = #selectionRoots, 1, -1 do
                    local root = selectionRoots[i]
                    if root:IsDescendantOf(inst) then
                        table.remove(selectionRoots, i)
                    end
                end
                table.insert(selectionRoots, inst)
                added = true
            end
        end
    end
    return added
end

local function getOrCreateTargetFolder(folderPath: { string }?): Instance
    local syncFolder = Workspace:FindFirstChild("BlenderSync")
    local root: Instance = (syncFolder and (syncFolder:IsA("Folder") or syncFolder:IsA("Model"))) and syncFolder or Workspace
    if not folderPath or #folderPath == 0 then
        return root
    end

    local current = root
    for _, name in ipairs(folderPath) do
        local child = current:FindFirstChild(name)
        if not child then
            child = Instance.new("Folder")
            child.Name = name
            child.Parent = current
        end
        current = child
    end
    return current
end

local function findById(id: string): Instance?
    local inst = partByIdMap[id]
    if inst and inst.Parent then
        return inst
    end
    for _, desc in ipairs(Workspace:GetDescendants()) do
        if (desc:IsA("BasePart") or desc:IsA("Model")) and desc:GetAttribute("LiveLinkId") == id then
            partByIdMap[id] = desc
            return desc
        end
    end
    return nil
end

local function cframeFromChange(change: any): CFrame?
    local p = change.position
    local r = change.rotation
    if typeof(p) == "table" and #p == 3 and typeof(r) == "table" and #r == 9 then
        return CFrame.new(p[1], p[2], p[3], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
    end
    return nil
end

local function refreshPartState(part: BasePart)
    local texKey = partTexKey(part)
    local folderKey = table.concat(getFolderCollectionPath(part), "/")
    partStateCache[part] = makePartState(part, texKey, folderKey)
end

local function applyOneChange(change: any)
    local action = change.action
    local id = change.id
    if typeof(id) ~= "string" or id == "" then
        return
    end

    if action == "delete" then
        local inst = partByIdMap[id]
        if inst then
            if inst:IsA("BasePart") then
                trackedPartsMap[inst :: BasePart] = nil
                partStateCache[inst :: BasePart] = nil
            end
            partByIdMap[id] = nil
            pcall(function()
                inst:Destroy()
            end)
        end

    elseif action == "create" then
        local existing = partByIdMap[id]
        local part: BasePart
        if existing and existing.Parent and existing:IsA("BasePart") then
            part = existing :: BasePart
        else
            local shape = change.shape or "Block"
            if shape == "Wedge" then
                part = Instance.new("WedgePart")
            else
                local p = Instance.new("Part")
                if shape == "Ball" or shape == "Sphere" then
                    p.Shape = Enum.PartType.Ball
                elseif shape == "Cylinder" then
                    p.Shape = Enum.PartType.Cylinder
                else
                    p.Shape = Enum.PartType.Block
                end
                part = p
            end
            part.Name = change.name or ("Part_" .. shape)
            part.Anchored = true
            part:SetAttribute("LiveLinkId", id)
        end

        if typeof(change.size) == "table" and #change.size == 3 then
            part.Size = Vector3.new(
                math.max(0.05, change.size[1]),
                math.max(0.05, change.size[2]),
                math.max(0.05, change.size[3])
            )
        end
        local cf = cframeFromChange(change)
        if cf then
            part.CFrame = cf
        end
        if typeof(change.color) == "table" and #change.color == 3 then
            part.Color = Color3.new(change.color[1], change.color[2], change.color[3])
        end
        if change.material and change.material ~= "" then
            pcall(function()
                part.Material = (Enum.Material :: any)[change.material]
            end)
        end

        local targetParent = getOrCreateTargetFolder(change.folder_path)
        if part.Parent ~= targetParent then
            part.Parent = targetParent
        end

        partByIdMap[id] = part
        blenderCreatedParts[part] = true
        compoundOfPart[part] = nil
        trackPart(part)
        refreshPartState(part)

    elseif action == "update" then
        local inst = findById(id)
        if not inst then
            return
        end

        if inst:IsA("Model") then
            -- Merged model moved/rotated in Blender: move the whole model rigidly
            local model = inst :: Model
            local target = cframeFromChange(change)
            if target then
                local delta = target * getModelFrame(model):Inverse()
                model:PivotTo(delta * model:GetPivot())
                -- Обновляем кэш, чтобы движение не вернулось эхом обратно в Blender
                for _, d in ipairs(model:GetDescendants()) do
                    if d:IsA("BasePart") then
                        local cached = partStateCache[d]
                        if cached then
                            cached.cf = d.CFrame
                        end
                    end
                end
            end
            return
        end

        if not inst:IsA("BasePart") then
            return
        end
        local part = inst :: BasePart
        if typeof(change.size) == "table" and #change.size == 3 then
            part.Size = Vector3.new(
                math.max(0.05, change.size[1]),
                math.max(0.05, change.size[2]),
                math.max(0.05, change.size[3])
            )
        end
        local cf = cframeFromChange(change)
        if cf then
            part.CFrame = cf
        end
        -- folder_path приходит только если объект реально перенесли в другую коллекцию в Blender
        if typeof(change.folder_path) == "table" then
            local targetParent = getOrCreateTargetFolder(change.folder_path)
            if part.Parent ~= targetParent then
                part.Parent = targetParent
            end
        end
        refreshPartState(part)
    end
end

local function applyBlenderChanges(changes: { any })
    -- Одна точка Undo (Ctrl+Z) на пакет изменений из Blender
    local recording: string? = nil
    local okRec, rec = pcall(function()
        return ChangeHistoryService:TryBeginRecording("Blender Live Link")
    end)
    if okRec then
        recording = rec
    end

    for _, change in ipairs(changes) do
        local ok, err = pcall(applyOneChange, change)
        if not ok then
            warn("[Roblox Live Link] Failed to apply Blender change: " .. tostring(err))
        end
    end

    if recording then
        ChangeHistoryService:FinishRecording(recording, Enum.FinishRecordingOperation.Commit)
    end
end

local function handleResponse(res: any)
    if not res.Body or res.Body == "" then
        return
    end
    local decOk, decData = pcall(function()
        return HttpService:JSONDecode(res.Body)
    end)
    if not decOk or typeof(decData) ~= "table" then
        return
    end

    -- Blender server restarted (new session): Blender's scene state is unknown -> resend everything
    local session = decData.session
    if typeof(session) == "string" and session ~= "" then
        if serverSession and serverSession ~= session and not isFullSyncing then
            print("[Roblox Live Link] Blender server restarted - resending scene.")
            needInitialFullSync = true
            pruneNextFullSync = syncMode ~= "Selection"
        end
        serverSession = session
    end

    if typeof(decData.blender_changes) == "table" and #decData.blender_changes > 0 then
        applyBlenderChanges(decData.blender_changes)
    end
end

local function reportFailure(res: any)
    local errStr = tostring(res)
    local now = os.clock()
    if serverAvailable or (now - lastErrorTime > 5) then
        lastErrorTime = now
        if string.find(errStr, "limit") then
            warn("[Roblox Live Link] HTTP rate limit reached. Throttling...")
        else
            warn("[Roblox Live Link] Blender server unreachable. Click 'Start Server' in Blender.")
        end
    end
    serverAvailable = false
end

local function postPayload(payloadTable: any): boolean
    if isPosting then
        return false
    end
    isPosting = true

    local jsonSuccess, jsonString = pcall(function()
        return HttpService:JSONEncode(payloadTable)
    end)
    if not jsonSuccess then
        isPosting = false
        warn("[Roblox Live Link] Failed to encode payload: " .. tostring(jsonString))
        return false
    end

    local reqSuccess, res = pcall(function()
        return HttpService:RequestAsync({
            Url = baseUrl() .. "/sync",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
            },
            Body = jsonString,
        })
    end)

    isPosting = false

    if reqSuccess and res.Success then
        if not serverAvailable then
            serverAvailable = true
            print("[Roblox Live Link] Connected to Blender Server!")
        end
        handleResponse(res)
        return true
    end
    reportFailure(if reqSuccess then (res.StatusCode .. " " .. res.StatusMessage) else res)
    return false
end

local function pollBlenderChanges(): boolean
    if isPosting or isFullSyncing then
        return false
    end
    isPosting = true

    local reqSuccess, res = pcall(function()
        return HttpService:RequestAsync({
            Url = baseUrl() .. "/poll_blender_changes",
            Method = "GET",
        })
    end)

    isPosting = false

    if reqSuccess and res.Success then
        if not serverAvailable then
            serverAvailable = true
            print("[Roblox Live Link] Connected to Blender Server!")
        end
        handleResponse(res)
        return true
    end
    reportFailure(if reqSuccess then (res.StatusCode .. " " .. res.StatusMessage) else res)
    return false
end

local function warnSerializeError(inst: Instance, err: any)
    warn(string.format("[Roblox Live Link] Skipped %s: %s", inst:GetFullName(), tostring(err)))
end

local function runInitialFullSync()
    currentSyncSession += 1
    local mySession = currentSyncSession
    local prune = pruneNextFullSync

    rebuildTrackedParts()
    partStateCache = {}
    table.clear(compoundOfPart)
    table.clear(compoundModelIds)
    table.clear(dirtyModels)
    table.clear(modelLastSent)

    local instances = {}
    local roots = mergeRoots()

    if importMode == "MergeModels" then
        local modelOrder: { Model } = {}
        local seenModels: { [Model]: boolean } = {}
        for _, part in ipairs(trackedPartsList) do
            if part.Parent then
                local topModel = getTopModel(part, roots)
                if topModel and not seenModels[topModel] then
                    seenModels[topModel] = true
                    table.insert(modelOrder, topModel)
                end
            end
        end
        for _, model in ipairs(modelOrder) do
            local ok, data = pcall(serializeCompoundModel, model)
            if ok then
                table.insert(instances, data)
            else
                warnSerializeError(model, data)
            end
        end
    end

    for _, part in ipairs(trackedPartsList) do
        if part.Parent and not compoundOfPart[part] then
            local ok, data = pcall(serializePart, part)
            if ok then
                table.insert(instances, data)
                partStateCache[part] = makePartState(part, texKeyOf(data.texture_info) .. "#" .. data.surfaces_sig, table.concat(data.folder_path, "/"))
            else
                warnSerializeError(part, data)
            end
        end
    end

    local totalCount = #instances
    print(string.format("[Roblox Live Link] Scene sync: %d objects to send (%s mode, %s).", totalCount, syncMode, importMode))

    local allOk = true
    local totalChunks = math.max(1, math.ceil(totalCount / FULL_SYNC_CHUNK_SIZE))
    for chunkIndex = 1, totalChunks do
        if not isSyncing or currentSyncSession ~= mySession then
            print(string.format("[Roblox Live Link] Scene sync aborted (session %d cancelled).", mySession))
            allOk = false
            break
        end
        local chunk = {}
        local first = (chunkIndex - 1) * FULL_SYNC_CHUNK_SIZE + 1
        for j = first, math.min(first + FULL_SYNC_CHUNK_SIZE - 1, totalCount) do
            table.insert(chunk, instances[j])
        end
        local ok = postPayload({
            event = if chunkIndex == 1 then "full_sync" else "diff_update",
            prune = prune,
            total_scene_objects = totalCount,
            chunk_index = chunkIndex,
            total_chunks = totalChunks,
            instances = chunk,
        })
        if not ok then
            allOk = false
            break
        end
        if chunkIndex < totalChunks then
            task.wait(0.05)
        end
    end

    if currentSyncSession == mySession and isSyncing then
        if allOk then
            pruneNextFullSync = false
            print(string.format("[Roblox Live Link] Scene sync completed: %d objects sent.", totalCount))
        else
            -- Blender недоступен: повторим полную синхронизацию, когда сервер появится
            needInitialFullSync = true
        end
    end
end

local function sendInitialFullSync()
    isFullSyncing = true
    local ok, err = pcall(runInitialFullSync)
    -- Сбрасываем флаг при любой ошибке, иначе syncTick навсегда перестаёт что-либо отправлять
    isFullSyncing = false
    if not ok then
        warn("[Roblox Live Link] Scene sync failed: " .. tostring(err))
    end
end

local function staleIdOf(part: BasePart): string?
    local id = part:GetAttribute("LiveLinkId")
    if typeof(id) == "string" and id ~= "" then
        if partByIdMap[id] == part then
            partByIdMap[id] = nil
        end
        return id
    end
    return nil
end

local function syncTick()
    if isFullSyncing then
        return
    end

    if needInitialFullSync then
        needInitialFullSync = false
        sendInitialFullSync()
        return
    end

    local diffList = {}
    local diffParts: { BasePart } = {}
    local diffModels: { Model } = {}
    local staleIds: { string } = {}
    local survivingParts = {}
    local roots = mergeRoots()

    local count = #trackedPartsList
    local expStart = expensiveCursor
    local expEnd = expStart + EXPENSIVE_CHECKS_PER_TICK - 1

    for i, part in ipairs(trackedPartsList) do
        if trackedPartsMap[part] and part.Parent and isEligiblePart(part) then
            table.insert(survivingParts, part)

            local cached = partStateCache[part]
            local changed = not cached
                or cached.cf ~= part.CFrame
                or cached.size ~= part.Size
                or cached.color ~= part.Color
                or cached.transparency ~= part.Transparency
                or cached.material ~= materialKeyOf(part)

            local texKey: string? = nil
            local folderKey: string? = nil
            if (changed or (i >= expStart and i <= expEnd)) then
                texKey = partTexKey(part)
                folderKey = table.concat(getFolderCollectionPath(part), "/")
                if cached and not changed then
                    changed = cached.texKey ~= texKey or cached.folderKey ~= folderKey
                end
            end

            if changed then
                local model = compoundOfPart[part]
                -- Новая деталь или смена родителя: к какой объединённой модели она теперь относится
                if importMode == "MergeModels" and (not cached or (cached.folderKey ~= folderKey)) then
                    local newModel = getTopModel(part, roots)
                    if newModel ~= model then
                        if model then
                            dirtyModels[model] = true
                        elseif cached then
                            -- была отдельной деталью в Blender, теперь входит в модель
                            local id = staleIdOf(part)
                            if id then
                                table.insert(staleIds, id)
                            end
                        end
                        compoundOfPart[part] = newModel
                        model = newModel
                    end
                end

                if model then
                    dirtyModels[model] = true
                    partStateCache[part] = makePartState(part, texKey or "", folderKey or "")
                else
                    local ok, data = pcall(serializePart, part)
                    if ok then
                        table.insert(diffList, data)
                        table.insert(diffParts, part)
                        partStateCache[part] = makePartState(part, texKeyOf(data.texture_info) .. "#" .. data.surfaces_sig, table.concat(data.folder_path, "/"))
                    else
                        -- Запоминаем состояние, чтобы не спамить предупреждением каждый тик
                        partStateCache[part] = makePartState(part, texKey or "", folderKey or "")
                        warnSerializeError(part, data)
                    end
                end
            end
        else
            -- Деталь удалена / вынесена из Workspace / больше не подходит под режим
            trackedPartsMap[part] = nil
            partStateCache[part] = nil
            local model = compoundOfPart[part]
            compoundOfPart[part] = nil
            if model then
                dirtyModels[model] = true
            else
                local id = staleIdOf(part)
                if id then
                    table.insert(staleIds, id)
                end
            end
        end
    end
    trackedPartsList = survivingParts
    expensiveCursor = if expEnd >= count then 1 else expEnd + 1

    -- Объединённые модели, в которых что-то изменилось, отправляются целиком
    local now = os.clock()
    for model in pairs(dirtyModels) do
        if now - (modelLastSent[model] or 0) < MODEL_RESEND_INTERVAL then
            continue -- остаётся "грязной" до следующего тика (троттлинг при перетаскивании)
        end
        dirtyModels[model] = nil
        local alive = model.Parent ~= nil and model:IsDescendantOf(Workspace)
        local data = nil
        if alive then
            local ok, res = pcall(serializeCompoundModel, model)
            if ok then
                data = res
            else
                warnSerializeError(model, res)
                continue
            end
        end
        if data and #data.parts > 0 then
            table.insert(diffList, data)
            table.insert(diffModels, model)
        else
            local id = compoundModelIds[model] or model:GetAttribute("LiveLinkId")
            if typeof(id) == "string" and id ~= "" then
                table.insert(staleIds, id)
            end
            compoundModelIds[model] = nil
            modelLastSent[model] = nil
        end
    end

    if #staleIds > 0 or #diffList > 0 then
        local ok = postPayload({
            event = "diff_update",
            instances = diffList,
            remove_ids = staleIds,
        })
        if not ok then
            -- Не теряем изменения: при следующей удачной отправке они уйдут снова
            for _, p in ipairs(diffParts) do
                partStateCache[p] = nil
            end
            for _, m in ipairs(diffModels) do
                dirtyModels[m] = true
                modelLastSent[m] = 0
            end
            if #staleIds > 0 then
                -- Сначала полная синхронизация с удалением лишнего, т.к. ID удалённых деталей уже потеряны
                needInitialFullSync = true
                pruneNextFullSync = syncMode ~= "Selection" or pruneNextFullSync
            end
        end
    else
        pollBlenderChanges()
    end
end

local connAdded: RBXScriptConnection? = nil
local connSelection: RBXScriptConnection? = nil
local heartbeatConn: RBXScriptConnection? = nil

local function onDescendantAdded(desc: Instance)
    if not isSyncing or isFullSyncing then
        return
    end
    -- Удаления не нужно отслеживать отдельно: syncTick сам видит part.Parent == nil
    if desc:IsA("BasePart") and isEligiblePart(desc) then
        trackPart(desc :: BasePart)
    end
end

local function onSelectionChanged()
    if not isSyncing or syncMode ~= "Selection" then
        return
    end
    -- Аддитивно: новые выделенные объекты добавляются, снятие выделения ничего не удаляет в Blender
    local selected = Selection:Get()
    if addSelectionRoots(selected) then
        for _, inst in ipairs(selected) do
            if inst:IsDescendantOf(Workspace) then
                trackSubtree(inst)
            end
        end
    end
end

local function setSyncState(enabled: boolean)
    if enabled == isSyncing then
        return
    end
    isSyncing = enabled
    toggleBtn:SetActive(enabled)

    if enabled then
        print(string.format("[Roblox Live Link] Starting sync in [%s] mode...", syncMode))
        needInitialFullSync = true
        -- Selection: в Blender могли остаться объекты прошлых сессий — не удаляем их
        pruneNextFullSync = syncMode ~= "Selection"
        lastSyncTime = 0
        serverSession = nil
        table.clear(selectionRoots)
        table.clear(rootOfPart)
        if syncMode == "Selection" then
            addSelectionRoots(Selection:Get())
            connSelection = Selection.SelectionChanged:Connect(onSelectionChanged)
        end

        connAdded = Workspace.DescendantAdded:Connect(onDescendantAdded)

        heartbeatConn = RunService.Heartbeat:Connect(function()
            if tickBusy then
                return
            end
            local now = os.clock()
            local interval = if serverAvailable then SYNC_INTERVAL else OFFLINE_RETRY_INTERVAL
            if now - lastSyncTime >= interval then
                lastSyncTime = now
                tickBusy = true
                task.spawn(function()
                    local ok, err = pcall(syncTick)
                    if not ok then
                        warn("[Roblox Live Link] Sync error: " .. tostring(err))
                    end
                    tickBusy = false
                end)
            end
        end)
    else
        currentSyncSession += 1
        print("[Roblox Live Link] Sync paused.")
        if heartbeatConn then
            heartbeatConn:Disconnect()
            heartbeatConn = nil
        end
        if connAdded then
            connAdded:Disconnect()
            connAdded = nil
        end
        if connSelection then
            connSelection:Disconnect()
            connSelection = nil
        end
        serverAvailable = false
        table.clear(partStateCache)
        table.clear(trackedPartsMap)
        table.clear(trackedPartsList)
        table.clear(compoundOfPart)
        table.clear(compoundModelIds)
        table.clear(dirtyModels)
        table.clear(modelLastSent)
        table.clear(selectionRoots)
        table.clear(rootOfPart)
    end
end

local function restartSync()
    if isSyncing then
        setSyncState(false)
        setSyncState(true)
    end
end

toggleBtn.Click:Connect(function()
    setSyncState(not isSyncing)
end)

modeBtn.Click:Connect(function()
    if syncMode == "Selection" then
        syncMode = "BlenderSync"
    elseif syncMode == "BlenderSync" then
        syncMode = "Workspace"
    else
        syncMode = "Selection"
    end
    print("[Roblox Live Link] Switched sync mode to: " .. syncMode)
    updateModeButtonLabel()
    restartSync()
end)

separateBtn.Click:Connect(function()
    local sel = Selection:Get()
    if #sel == 0 then
        print("[Roblox Live Link] Please select a Model or Part to toggle Separate Parts.")
        return
    end
    for _, item in ipairs(sel) do
        local cur = item:GetAttribute("LiveLink_SeparateParts") == true
        item:SetAttribute("LiveLink_SeparateParts", not cur)
        print(string.format("[Roblox Live Link] %s Separate Parts set to: %s", item.Name, tostring(not cur)))
    end
    if isSyncing then
        needInitialFullSync = true
        pruneNextFullSync = true
    end
end)

Workspace:GetAttributeChangedSignal("LiveLinkSync"):Connect(function()
    local val = Workspace:GetAttribute("LiveLinkSync")
    if typeof(val) == "boolean" and val ~= isSyncing then
        setSyncState(val)
    end
end)

plugin.Unloading:Connect(function()
    currentSyncSession += 1
    isSyncing = false
    if heartbeatConn then
        heartbeatConn:Disconnect()
    end
    if connAdded then
        connAdded:Disconnect()
    end
    if connSelection then
        connSelection:Disconnect()
    end
end)

print("[Roblox Live Link] Plugin ready (v" .. PLUGIN_VERSION .. "). BETA: still being tested, please report bugs.")
