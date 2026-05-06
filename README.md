# Viewport Export Panel — lichtfeld Studio

A lichtfeld Studio panel for exporting top-down orthographic images from the
viewport, with support for tiled exports at a precise ground resolution and
assembly of tiles into georeferenced GeoTIFF or JPEG2000 mosaics.

---

## Requirements

| Package | Purpose | Install |
|---|---|---|
| `numpy` | Array capture | |
| `Pillow` | Image save |  |
| `rasterio` | GeoTIFF / JPEG2000 mosaic write | |

> `rasterio` is only required for the **Mosaic** step. Tiled PNG export works without it.

---

## Workflow Overview

```
1. Set top-down view        (Set Top-Down button)
2. Load cropbox             (Read Cropbox)
3. Load coord TXT           (Browse… in Ortho Export section)
4. Export tiles             (Tiled Ortho Export section)
5. Build mosaic             (Mosaic from Tiles section)
```

---

## Panel Sections

### Standard Viewport Export

Export the current viewport as JPG or PNG at any supported resolution.

| Control | Description |
|---|---|
| Resolution | Preset output heights (720p → 8K) or Native viewport |
| Format | JPEG (with quality slider) or PNG (with compression level) |
| Transparency | PNG only — enables alpha channel |
| Export | Saves immediately; no dialog if a default path is set |

---

### Ortho Export

Exports a single top-down PNG of the entire cropbox area at up to 8K resolution.

**Setup:**

1. Click **Set Top-Down** to align the camera directly overhead.
2. Click **Read Cropbox** to read the scene's cropbox node — this sets the
   camera to frame the full crop area and stores its extents for all
   subsequent exports.
3. Optionally click **Browse…** to load a coordinate TXT file. This
   geo-references all exports (PNG world files, tile world files, mosaic embed).

**Coord TXT format:**

```
32725 298153.29 m E  9207873.34 m N  60 m RL
```

- First token: EPSG code (auto-fills the Mosaic EPSG field)
- `298153.29 m E` — easting of the model origin in the given CRS
- `9207873.34 m N` — northing (use `N` or `S`)
- `60 m RL` — reduced level (ignored)

**Options:**

| Control | Description |
|---|---|
| Resolution | Output pixel height for the single ortho image |
| BW2A Alpha | Extracts alpha by capturing black and white backgrounds and compositing |
| Export Ortho PNG | Runs the export; saves a `.png` and a `.pgw` world file alongside it |

---

### Tiled Ortho Export

Exports a grid of PNG tiles at a precise **pixels-per-metre** ground resolution,
covering the full cropbox extent with zero overlap and no image interpolation.
Each tile is captured at native viewport resolution.

**How scale is set:**

Rather than computing a camera FOV from scratch (which would require knowing
lichtfeld's internal projection constants), the panel:

1. Reads the **current** `ortho_view_extent_world` and camera FOV (set by
   Read Cropbox).
2. Derives the current `m/px = extent / viewport_height`.
3. Scales the FOV proportionally so the new extent matches `1/px_m × vp_height`.

This is fully independent of `ORTHO_EYE_H` and exactly matches the scale
lichtfeld's ortho projection produces.

**On the first tile**, the actual extent is read back and logged, so any
residual error is recorded and world files use the verified value.

**Controls:**

| Control | Description |
|---|---|
| Scale (px/m) | Ground resolution, e.g. `10` = 10 px/m = 0.1 m/px |
| Info label | Preview of tile grid: rows × cols, tile size in metres |
| Export Tiled Ortho | Opens save dialog for `_R01C01.png`; all other tiles are named automatically |

**Output files** (per tile):

```
<stem>_R01C01.png   — tile image (RGBA)
<stem>_R01C01.pgw   — world file (top-left easting/northing + pixel size)
<stem>_R02C01.png
<stem>_R02C01.pgw
...
```

Row numbering increases **southward** (first row = northernmost),
column numbering increases **eastward**.

**Tips:**

- Always click **Read Cropbox** before starting a tiled export — the panel
  needs the extent reading from that step to compute the correct FOV.
- The viewport must be in **orthographic** mode before exporting. Read Cropbox
  sets this automatically.
- The camera is left at its last tile position after export completes. Use
  **Read Cropbox** again to re-frame the full area.

---

### Mosaic from Tiles

Assembles a folder of `_R##C##.png` tiles (with `.pgw` world files) into a
single georeferenced image. Runs in a background thread — the UI stays
responsive during assembly.

**Controls:**

| Control | Description |
|---|---|
| Format | **GeoTIFF** (deflate-compressed, 512×512 tiled, fast in QGIS) or **JPEG2000** |
| EPSG code | CRS embedded in the output file. Auto-filled from the coord TXT first token. Leave blank to write without CRS. |
| Crop to cropbox extents | After assembling the full tile canvas, trims to the exact cropbox boundary |
| Use last export folder | (Default: on) Points at the most recent tiled export — no need to browse. Uncheck to select a different folder. |
| Browse tile folder… | Visible only when "Use last export folder" is unchecked |
| Build Mosaic | Opens save dialog then assembles. Progress shown in status bar. |

**Assembly process:**

1. Discovers all `_R##C##.png` files in the selected folder.
2. Reads each `.pgw` world file for exact pixel-accurate geo placement.
3. Computes the minimum bounding canvas covering all tiles.
4. Composites each tile onto the canvas at its correct position.
5. Optionally crops to the cropbox extents.
6. Writes the result with an embedded geotransform and, if an EPSG code is
   set, an embedded CRS — fully compatible with QGIS, ArcGIS, and GDAL.

**GeoTIFF output options** (applied automatically):

```
compress  = deflate
predictor = 2        (horizontal differencing — good for imagery)
tiled     = True
blocksize = 512×512  (optimal for QGIS overview rendering)
```

---

## Coordinate Convention

lichtfeld's model space uses:

- `+X` → West  (opposite to standard easting)
- `+Z` → South (negated before passing to lichtfeld camera, so the panel
  treats `Z` as north-positive internally)

The coord TXT gives the **real-world position of the model origin (0, 0)**.
All world file top-left coordinates are derived as:

```
tl_easting  = origin_E  - model_cx - (tile_width_px  / 2) × m/px
tl_northing = origin_N  - model_cz + (tile_height_px / 2) × m/px
```

---

## Troubleshooting

**Wrong scale / overlap in tiles**
- Make sure you clicked **Read Cropbox** immediately before starting the tiled
  export — the panel reads the live `ortho_view_extent_world` at that moment
  to calibrate the FOV scale.
- Check the log for `tile scale:` lines — these show the requested vs actual
  m/px and the derived FOV.

**Tiles rotated 180°**
- Should not occur from build `r` onward. If seen, check that
  `_mirror_lr` and `_rotate180` are both defined (they are module-level helpers).

**EPSG field is blank**
- The coord TXT first token must be a bare integer (e.g. `32725`).
- You can always type an EPSG code directly into the field.

**Mosaic crop produces a small or empty image**
- Confirm the coord TXT is loaded (coord file name shown in the Ortho Export
  section) and that the cropbox is still loaded (shown in the Ortho section).
- The crop uses the same sign convention as the world files — if tiles were
  exported without a coord TXT, the cropbox crop has no reference origin and
  is skipped.
