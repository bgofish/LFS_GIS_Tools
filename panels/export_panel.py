"""GIS_Tools panel — JPG/PNG export with optional BW2A alpha extraction."""
# *** BUILD 2026-05-07w ***
# vs 2026-05-07v:
#   w1. Tile BW2A alpha: "Alpha (RGBA via BW2A)" checkbox added to Tiled Ortho Export.
#       When enabled each tile does a 5-sub-step BW2A capture (black bg → capture →
#       white bg → capture → restore → compute RGBA) instead of a single RGB capture.
#       The draw-handler sub_step field (0-4) is nested inside the existing step-0/1/2
#       state machine.  World files and .pgw georeferencing are unchanged.
# vs 2026-05-06u:
#   v1. px/m floor lowered from 0.01 to 1e-9 so arbitrarily small scales are accepted.
#       Format changed from .4g to .6g to show more significant figures.
#   v2. FOV cap detection: when requested px/m would require FOV > ~170 deg (the
#       engine silently clamps, causing the 0.08298755 m/px ceiling), an explicit
#       error is raised with the max achievable px/m at the current zoom and
#       instructions to zoom out before reading the cropbox.
#   v3. _notify_tile_complete(): Windows balloon notification (NotifyIcon via
#       System.Windows.Forms) fires when the tiled mosaic export finishes.
#       No extra packages needed. Silently skipped on non-Win32.
# vs 2026-05-06o:
#   p1. Camera NOT restored after ortho export (stays where user left it).
#   r1. Tiled ortho export at user-defined px/m (R##C## PNGs + .pgw world files).
#   s1. Mosaic from tiles → GeoTIFF or JPEG2000 with optional cropbox crop.
#   t1. _read_coord_txt parses EPSG from first token; auto-fills EPSG field.
#   t2. "Use last export folder" checkbox in Mosaic section.
#   u1. SCALE FIX: tile FOV no longer computed from ORTHO_EYE_H assumption.
#       Instead we measure ortho_view_extent_world at the current cropbox zoom
#       BEFORE starting tiles, derive current m/px, then scale the live FOV
#       proportionally to hit the requested px/m exactly.  This eliminates the
#       ~2× scale error seen when using 25 px/m (0.08 m/px instead of 0.04).
#       Requires: rasterio  (pip install rasterio)
#   1. World file top-left no longer re-reads lf.get_camera() after the finally
#      block has restored the original camera.  Previously, when the user had
#      panned/zoomed away from model origin, the restored (original) cam.target
#      was used as the capture-centre offset → wrong TL coordinate.  Now
#      ortho_cx / ortho_cz (computed before capture, always the actual centre of
#      the exported image) are used directly.  "Zoom anywhere → Export" now
#      produces a correctly geo-referenced world file.
# Carried over from 2026-05-06j:
#   j1. _write_world_file: pixel_size wrapped in abs().
#   j2. pixel_size uses export_h not vp_h.
#   j3. North convention: lichtfeld +Z = South, negated for northing.
#   j4. img.convert("RGBA") before every PNG save → PIL colour-type 6.

import math
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image

import lichtfeld as lf

lf.log.info("[GIS_Tools] *** Version 0.1.0 BUILD 2026-05-07w LOADED ***")

# ── Version detection ─────────────────────────────────────────────────────────
def _parse_version(v: str) -> tuple:
    parts = v.lstrip("v").split(".")[:3]
    return tuple(int(re.match(r"\d+", x).group()) for x in parts)

Y_UP = _parse_version(lf.__version__) >= (0, 5, 1)

RESOLUTIONS = [
    ("Viewport", None),
    ("1080p",    1080),
    ("4K",       2160),
    ("8K",       4320),
]
FORMATS = ["JPG", "PNG"]
ORTHO_RESOLUTIONS = [
    ("Screen",  None),
    ("1080",   1080),
    ("4K",      2160),
    ("8K",      4320),
]
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
ORTHO_EYE_H = 200.0
# ORTHO_EYE_H = 1000.0

# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_dialog(default_name, title, file_filter, ext):
    if sys.platform != "win32":
        return None
    ps_script = f'''
    Add-Type -AssemblyName System.Windows.Forms
    $d = New-Object System.Windows.Forms.SaveFileDialog
    $d.Title = "{title}"
    $d.Filter = "{file_filter}"
    $d.FileName = "{default_name}"
    $d.DefaultExt = "{ext}"
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
        Write-Output $d.FileName
    }}
    '''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True,
            creationflags=_SUBPROCESS_FLAGS,
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception:
        return None


def _open_dialog(title, file_filter):
    if sys.platform != "win32":
        return None
    ps_script = f'''
    Add-Type -AssemblyName System.Windows.Forms
    $d = New-Object System.Windows.Forms.OpenFileDialog
    $d.Title = "{title}"
    $d.Filter = "{file_filter}"
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
        Write-Output $d.FileName
    }}
    '''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True,
            creationflags=_SUBPROCESS_FLAGS,
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception:
        return None


def _default_path(filename):
    return str(Path(os.getcwd()) / filename)


def _snap2(n):
    return n if n % 2 == 0 else n + 1


def _capture_arr():
    """Capture the current viewport. Returns float32 array or None."""
    vp = lf.capture_viewport()
    if vp is None or vp.image is None:
        return None
    arr = np.asarray(vp.image.cpu().contiguous(), dtype=np.float32)
    if Y_UP:
        arr = np.flip(arr, axis=0).copy()
    return arr


def _rotate180(arr):
    return np.flip(np.flip(arr, axis=0), axis=1).copy()


def _mirror_lr(arr):
    return np.flip(arr, axis=1).copy()


def _arr_to_image(arr):
    rgb = (arr[..., :3] * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


def _resize(img, target_h):
    src_w, src_h = img.size
    target_w = max(1, round(src_w * target_h / src_h))
    if (src_w, src_h) != (target_w, target_h):
        return img.resize((target_w, target_h), Image.LANCZOS)
    return img


def _bw2a(black_path, white_path, out_path):
    """BW2A: recover RGBA from a black-bg / white-bg capture pair.

    Both inputs must be RGB PNGs (no alpha channel).
    alpha = 1  →  opaque  (black and white renders look identical)
    alpha = 0  →  transparent  (white render = 255, black render = 0)
    """
    # FIX 4: always load as RGB so diff maths are clean
    img_black = np.array(Image.open(black_path).convert("RGB")).astype(float)
    img_white = np.array(Image.open(white_path).convert("RGB")).astype(float)

    diff  = img_white - img_black
    alpha = 1.0 - np.clip(np.mean(diff, axis=2) / 255.0, 0.0, 1.0)

    recovered = np.clip(img_black / (alpha[:, :, np.newaxis] + 1e-10), 0, 255).astype(np.uint8)
    alpha_u8  = (alpha * 255).astype(np.uint8)
    rgba      = np.dstack((recovered, alpha_u8))

    # FIX 4: explicit RGBA mode → PIL writes PNG colour-type 6, QGIS reads alpha correctly
    Image.fromarray(rgba, "RGBA").save(out_path, "PNG")
    lf.log.info(f"[ViewportExport] RGBA saved {out_path}")


def _read_coord_txt(path):
    """Parse format: '32725 298153.29 m E  9207873.34 m N  60 m RL'

    Returns (easting, northing, epsg_str).
    The first token is the EPSG code if it is a bare integer; otherwise None.
    """
    with open(path) as f:
        text = f.read()
    parts = text.split()
    easting = northing = epsg_str = None

    if parts and re.match(r"^\d+$", parts[0]):
        epsg_str = parts[0]

    for i, p in enumerate(parts):
        if p == "E" and i >= 2 and parts[i-1] == "m":
            easting = float(parts[i-2])
        if p in ("N", "S") and i >= 2 and parts[i-1] == "m":
            northing = float(parts[i-2])
    if easting is None or northing is None:
        raise ValueError(f"Could not parse easting/northing from: {text.strip()}")
    return easting, northing, epsg_str


def _write_world_file(png_path, tl_e, tl_n, pixel_size):
    """Write a .pgw world file for QGIS (north-up, square pixels).

    World file line order:
      1. pixel width  in X (metres/pixel, positive = east)
      2. rotation about Y (0)
      3. rotation about X (0)
      4. pixel height in Y (metres/pixel, NEGATIVE = north-up)
      5. easting  of centre of top-left pixel
      6. northing of centre of top-left pixel

    pixel_size must be a POSITIVE value — this function applies the negation.
    """
    pgw_path = str(Path(png_path).with_suffix(".pgw"))
    ps = abs(pixel_size)   # FIX 1: guard — ensure positive before negating line 4
    with open(pgw_path, "w") as f:
        f.write(f"{ps:.8f}\n")
        f.write("0.00000000\n")
        f.write("0.00000000\n")
        f.write(f"-{ps:.8f}\n")
        f.write(f"{tl_e:.4f}\n")
        f.write(f"{tl_n:.4f}\n")
    return pgw_path


def _notify_tile_complete(n_tiles: int, out_dir: str) -> None:
    """Show a Windows balloon notification when tiled export finishes.

    Uses the Win32 NotifyIcon API via System.Windows.Forms (no extra packages).
    Silently skipped on non-Win32 platforms.
    """
    if sys.platform != "win32":
        return
    title_ps   = "GIS_Tools — Tiled Export Complete"
    message_ps = f"{n_tiles} tiles saved to:`n{out_dir}"
    # Escape single-quotes for PowerShell string
    title_ps   = title_ps.replace("'", "'")
    message_ps = message_ps.replace("'", "'")
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipIcon  = [System.Windows.Forms.ToolTipIcon]::Info
$notify.BalloonTipTitle = '{title_ps}'
$notify.BalloonTipText  = '{message_ps}'
$notify.Visible = $true
$notify.ShowBalloonTip(8000)
Start-Sleep -Milliseconds 9000
$notify.Dispose()
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=_SUBPROCESS_FLAGS,
        )
        lf.log.info("[ViewportExport] tile-complete notification sent")
    except Exception as _ne:
        lf.log.warn(f"[ViewportExport] notification failed: {_ne}")


# ── Panel ─────────────────────────────────────────────────────────────────────

class ViewportExportPanel(lf.ui.Panel):
    """Export viewport as JPG or PNG; PNG supports RGBA via BW2A."""

    id          = "viewport_export.main_panel"
    label       = "GIS_Tools"
    space       = lf.ui.PanelSpace.MAIN_PANEL_TAB
    order       = 60
    update_interval_ms = 500
    template    = str(Path(__file__).resolve().with_name("export_panel.rml"))
    height_mode = lf.ui.PanelHeightMode.CONTENT

    def __init__(self):
        self._handle = None
        self._resolution_idx = 0
        self._format_idx = 0
        self._quality = 95
        self._png_compress = 6
        self._transparency = False
        self._status = ""
        self._ortho_coord_txt = ""
        self._ortho_alpha = False
        self._ortho_res_idx = 3

        self._crop_xmin     = None
        self._crop_xmax     = None
        self._crop_zmin     = None
        self._crop_zmax     = None
        self._crop_cx       = None
        self._crop_cz       = None
        self._crop_ground_y = None

        self._bw2a_state = {
            "step": 0,
            "black": None,
            "white": None,
            "orig_bg": None,
            "out_path": None,
            "target_h": None,
        }

        # ── Tiled export ──────────────────────────────────────────────────────
        self._tile_px_per_m  = 10.0
        self._tile_alpha     = False   # BW2A per-tile alpha
        self._last_tile_dir  = ""
        self._tile_state: dict = {
            "active":            False,
            "step":              0,
            "settle":            0,
            "tiles":             [],
            "tile_idx":          0,
            "path_stem":         "",
            "out_dir":           "",
            "vp_w":              0,
            "vp_h":              0,
            "tile_w_m":          0.0,
            "tile_h_m":          0.0,
            "ref_fov_deg":       0.0,   # FOV that produced ref_m_per_px
            "ref_m_per_px":      0.0,   # measured at cropbox zoom before tiles start
            "tile_fov_deg":      0.0,   # FOV scaled for requested px/m
            "ground_y":          0.0,
            "centre_e":          None,
            "centre_n":          None,
            "verified_m_per_px": None,
            # BW2A sub-step fields (used when tile_alpha is True)
            "tile_alpha":       False,
            "sub_step":         0,     # 0=RGB only; 1=set-black; 2=cap-black; 3=set-white; 4=cap-white
            "bw2a_black":       None,
            "bw2a_white":       None,
            "bw2a_orig_bg":     None,
        }

        # ── Mosaic ────────────────────────────────────────────────────────────
        self._mosaic_format_idx   = 0
        self._mosaic_crop         = True
        self._mosaic_epsg         = ""
        self._mosaic_use_last_dir = True
        self._mosaic_tile_dir     = ""
        self._mosaic_running      = False

        self._ortho_bw2a_state = {
            "step": 0,
            "black": None,
            "white": None,
            "orig_bg": None,
            "path": None,
            "out_dir": None,
            "export_w": None,
            "export_h": None,
            "orig_eye": None,
            "orig_target": None,
            "orig_up": None,
            "orig_fov": None,
            "ortho_cx": None,
            "ortho_cz": None,
            "ground_y": None,
            "crop_xmin": None,
            "crop_xmax": None,
            "crop_zmin": None,
            "crop_zmax": None,
            "vp_w": None,
            "vp_h": None,
            "centre_e": None,
            "centre_n": None,
        }

    def _register_draw_handler(self):
        lf.add_draw_handler("viewport_export.bw2a", self._bw2a_draw_handler)

    def _bw2a_draw_handler(self, context):
        """
        Multi-frame BW2A capture spread across draw callbacks.
          step 1 -> set black bg, wait
          step 2 -> capture black, set white bg, wait
          step 3 -> capture white, restore bg, run BW2A, clean up
        """
        rs = lf.get_render_settings()
        s  = self._bw2a_state

        if s["step"] == 1:
            s["orig_bg"] = rs.background_color
            rs.background_color = (0.0, 0.0, 0.0)
            s["step"] = 2

        elif s["step"] == 2:
            self._set_status("Capturing black bg...", warning=True)
            arr = _capture_arr()
            if arr is None:
                self._set_status("Capture failed (black bg).", error=True)
                self._bw2a_abort(rs)
                return
            s["black"] = (arr[..., :3] * 255.0).clip(0, 255).astype(np.uint8)
            rs.background_color = (1.0, 1.0, 1.0)
            s["step"] = 3

        elif s["step"] == 3:
            self._set_status("Capturing white bg...", warning=True)
            arr = _capture_arr()
            if arr is None:
                self._set_status("Capture failed (white bg).", error=True)
                self._bw2a_abort(rs)
                return
            s["white"] = (arr[..., :3] * 255.0).clip(0, 255).astype(np.uint8)
            rs.background_color = s["orig_bg"]
            s["step"] = 0

            try:
                out_path   = s["out_path"]
                out_dir    = Path(out_path).parent
                black_path = str(out_dir / "render_black_bg.png")
                white_path = str(out_dir / "render_white_bg.png")

                self._set_status("Running BW2A...", warning=True)
                Image.fromarray(s["black"], "RGB").save(black_path, "PNG")
                Image.fromarray(s["white"], "RGB").save(white_path, "PNG")
                _bw2a(black_path, white_path, out_path)

                if s["target_h"]:
                    img = _resize(Image.open(out_path).convert("RGBA"), s["target_h"])
                    img = img.convert("RGBA")   # FIX 4: ensure colour-type 6
                    img.save(out_path, "PNG")

                self._set_status(f"Saved: {out_path}", success=True)
                lf.log.info(f"[ViewportExport] done: {out_path}")

            except Exception as e:
                self._set_status(f"Error: {e}", error=True)
                lf.log.error(f"[ViewportExport] {e}")

            finally:
                lf.remove_draw_handler("viewport_export.bw2a")

    def _bw2a_abort(self, rs):
        orig = self._bw2a_state.get("orig_bg")
        if orig is not None:
            rs.background_color = orig
        self._bw2a_state["step"] = 0
        lf.remove_draw_handler("viewport_export.bw2a")

    # ── Ortho BW2A draw handler ───────────────────────────────────────────────

    def _ortho_bw2a_draw_handler(self, context):
        """
        Multi-frame ortho BW2A capture spread across draw callbacks.
          step 1 -> set black bg, wait one frame
          step 2 -> capture black, set white bg, wait one frame
          step 3 -> capture white, restore bg, run crop+BW2A+save, clean up
        """
        rs = lf.get_render_settings()
        s  = self._ortho_bw2a_state

        if s["step"] == 1:
            s["orig_bg"] = rs.background_color
            rs.background_color = (0.0, 0.0, 0.0)
            s["step"] = 2

        elif s["step"] == 2:
            self._set_status("Ortho: capturing black bg...", warning=True)
            arr = _capture_arr()
            if arr is None:
                self._set_status("Ortho: capture failed (black bg).", error=True)
                self._ortho_bw2a_abort(rs)
                return
            s["black"] = _mirror_lr(_rotate180(arr))
            rs.background_color = (1.0, 1.0, 1.0)
            s["step"] = 3

        elif s["step"] == 3:
            self._set_status("Ortho: capturing white bg...", warning=True)
            arr = _capture_arr()
            if arr is None:
                self._set_status("Ortho: capture failed (white bg).", error=True)
                self._ortho_bw2a_abort(rs)
                return
            s["white"] = _mirror_lr(_rotate180(arr))
            rs.background_color = s["orig_bg"]
            s["step"] = 0

            # Capture view info NOW — before camera is restored
            try:
                _v = lf.get_current_view()
                s["ortho_extent"]       = float(_v.ortho_view_extent_world)
                s["cam_target_capture"] = tuple(lf.get_camera().target)
                lf.log.info(
                    f"[ViewportExport] extent captured: {s['ortho_extent']:.4f}m  "
                    f"cam_target={s['cam_target_capture']}"
                )
            except Exception as _ve:
                lf.log.warn(f"[ViewportExport] could not capture view info: {_ve}")
                s["ortho_extent"]       = None
                s["cam_target_capture"] = None

            orig_eye = orig_target = orig_up = None  # guard for finally
            try:
                export_w    = s["export_w"]
                export_h    = s["export_h"]
                path        = s["path"]
                out_dir     = Path(path).parent
                orig_eye    = s["orig_eye"]
                orig_target = s["orig_target"]
                orig_up     = s["orig_up"]
                orig_fov    = s["orig_fov"]
                vp_w        = s["vp_w"]
                vp_h        = s["vp_h"]
                centre_e    = s["centre_e"]
                centre_n    = s["centre_n"]
                crop_xmin   = s["crop_xmin"]
                crop_xmax   = s["crop_xmax"]
                crop_zmin   = s["crop_zmin"]
                crop_zmax   = s["crop_zmax"]
                ortho_cx    = s["ortho_cx"]
                ortho_cz    = s["ortho_cz"]
                use_crop    = crop_xmin is not None
                crop_w      = (crop_xmax - crop_xmin) if use_crop else 0.0
                crop_h      = (crop_zmax - crop_zmin) if use_crop else 0.0

                def _arr_to_rgb_u8(a):
                    return Image.fromarray(
                        (a[..., :3] * 255.0).clip(0, 255).astype(np.uint8), "RGB"
                    )

                self._set_status("Ortho: processing BW2A...", warning=True)
                img_black = _arr_to_rgb_u8(s["black"]).resize((export_w, export_h), Image.LANCZOS)
                img_white = _arr_to_rgb_u8(s["white"]).resize((export_w, export_h), Image.LANCZOS)

                black_path = str(out_dir / "ortho_black_bg.png")
                white_path = str(out_dir / "ortho_white_bg.png")
                img_black.save(black_path, "PNG")
                img_white.save(white_path, "PNG")
                lf.log.info(f"[ViewportExport] ortho black_bg -> {black_path}")
                lf.log.info(f"[ViewportExport] ortho white_bg -> {white_path}")

                b     = np.array(img_black).astype(float)
                w     = np.array(img_white).astype(float)
                alpha = np.clip(1.0 - np.mean(w - b, axis=2) / 255.0, 0.0, 1.0)
                recovered = np.clip(b / (alpha[:, :, np.newaxis] + 1e-10), 0, 255).astype(np.uint8)
                alpha_u8  = (alpha * 255).astype(np.uint8)
                img = Image.fromarray(np.dstack((recovered, alpha_u8)), "RGBA")
                lf.log.info(f"[ViewportExport] ortho BW2A RGBA assembled  {export_w}x{export_h}")

                # FIX 2: pixel_size uses export_h (output rows), not vp_h (viewport rows)
                extent = s.get("ortho_extent")
                if extent:
                    pixel_size = extent / export_h
                    lf.log.info(
                        f"[ViewportExport] pixel_size = {extent:.4f} / {export_h} "
                        f"= {pixel_size:.8f} m/px"
                    )
                else:
                    pixel_size = (crop_h / export_h) if (use_crop and crop_h) else 1.0
                    lf.log.warn("[ViewportExport] BW2A no extent — fallback pixel_size")

                left = top = 0
                right, bottom = export_w, export_h
                if use_crop:
                    px_per_m = 1.0 / pixel_size

                    cx_off = (crop_xmin + crop_xmax) / 2.0 - ortho_cx
                    cz_off = (crop_zmin + crop_zmax) / 2.0 - ortho_cz

                    img_cx_px      = export_w / 2.0 + cx_off * px_per_m
                    img_cz_px      = export_h / 2.0 + cz_off * px_per_m
                    half_crop_w_px = (crop_w / 2.0) * px_per_m
                    half_crop_h_px = (crop_h / 2.0) * px_per_m

                    left   = int(round(img_cx_px - half_crop_w_px))
                    right  = int(round(img_cx_px + half_crop_w_px))
                    top    = int(round(img_cz_px - half_crop_h_px))
                    bottom = int(round(img_cz_px + half_crop_h_px))
                    left, right = max(0, left), min(export_w, right)
                    top, bottom = max(0, top),  min(export_h, bottom)
                    img = img.crop((left, top, right, bottom))

                cropped_w, cropped_h = img.size
                lf.log.info(
                    f"[ViewportExport] ortho BW2A crop  px ({left},{top})->({right},{bottom})  "
                    f"output {cropped_w}x{cropped_h}"
                )

                # FIX 4: explicit RGBA before save → PIL writes colour-type 6
                img = img.convert("RGBA")
                img.save(path, "PNG")
                lf.log.info(f"[ViewportExport] ortho RGBA PNG -> {path}")

                # World file
                _ct = s.get("cam_target_capture") or (0.0, 0.0, 0.0)
                if centre_e is not None:
                    _cx_world =  float(_ct[0])
                    _cz_world = -float(_ct[2])   # FIX 3: negate — lichtfeld +Z = South
                    _half_w_m = pixel_size * cropped_w / 2.0
                    _half_h_m = pixel_size * cropped_h / 2.0
                    tl_e = centre_e - _cx_world - _half_w_m
                    tl_n = centre_n - _cz_world + _half_h_m
                    world_path = _write_world_file(path, tl_e, tl_n, pixel_size)
                    lf.log.info(
                        f"[ViewportExport] world file: cx={_cx_world:.3f} cz_north={_cz_world:.3f} "
                        f"half_w={_half_w_m:.3f}m half_h={_half_h_m:.3f}m "
                        f"tl_e={tl_e:.3f} tl_n={tl_n:.3f} ps={pixel_size:.8f}"
                    )
                    lf.log.info(f"[ViewportExport] world file -> {world_path}")
                    self._set_status(
                        f"Saved: {Path(path).name}  |  {pixel_size:.6f} m/px  "
                        f"{cropped_w}x{cropped_h}px", success=True)
                else:
                    tl_e = tl_n = None
                    self._set_status(
                        f"Saved: {Path(path).name}  (no world file)  "
                        f"{cropped_w}x{cropped_h}px", success=True)

                # ── Log file ──────────────────────────────────────────────────
                log_path = str(Path(path).with_suffix(".log.txt"))
                lf.log.info(f"[ViewportExport] BW2A writing log -> {log_path}")
                try:
                    import traceback as _tb2
                    _ortho_res_label = ORTHO_RESOLUTIONS[self._ortho_res_idx][0]
                    _ext = s.get("ortho_extent")
                    _lines = [
                        "Ortho Export Log\n",
                        "=" * 40 + "\n",
                        f"Build:        2026-05-06o\n",
                        f"PNG:          {path}\n",
                        f"Resolution:   {_ortho_res_label} ({export_w}x{export_h} pre-crop)\n",
                        f"Alpha (BW2A): yes\n",
                        f"Export size:  {cropped_w} x {cropped_h} px\n\n",
                        f"Camera FOV:   {orig_fov:.4f} deg\n",
                        f"Eye height:   {ORTHO_EYE_H:.1f} m (fixed)\n\n",
                    ]
                    if use_crop:
                        _lines += [
                            "Cropbox extents (metres):\n",
                            f"  xmin: {crop_xmin:.4f} m\n",
                            f"  xmax: {crop_xmax:.4f} m\n",
                            f"  zmin: {crop_zmin:.4f} m\n",
                            f"  zmax: {crop_zmax:.4f} m\n",
                            f"  width  (X): {crop_w:.4f} m\n",
                            f"  depth  (Z): {crop_h:.4f} m\n\n",
                            "Crop pixel bounds (in pre-crop image):\n",
                            f"  left={left}  right={right}  top={top}  bottom={bottom}\n\n",
                        ]
                    _lines += [
                        "Resolution:\n",
                        f"  ortho_view_extent: {_ext:.4f} m\n" if _ext else "  ortho_view_extent: (unavailable)\n",
                        f"  export_h:          {export_h} px\n",
                        f"  m/pixel:           {pixel_size:.8f} m/px\n",
                        f"  px/metre:          {1.0 / pixel_size:.4f} px/m\n\n",
                    ]
                    if centre_e is not None and tl_e is not None:
                        _lines += [
                            "Coordinates:\n",
                            f"  Origin Easting:  {centre_e:.4f}\n",
                            f"  Origin Northing: {centre_n:.4f}\n",
                            f"  Capture cx (model): {float(_ct[0]):.4f}\n",
                            f"  Capture cz (model): {float(_ct[2]):.4f}\n",
                            f"  TL Easting:      {tl_e:.4f}\n",
                            f"  TL Northing:     {tl_n:.4f}\n",
                        ]
                    _content = "".join(_lines)
                    # Write to PNG-adjacent path AND cwd fallback
                    for _lp in [log_path, str(Path(os.getcwd()) / "ortho_bw2a.log.txt")]:
                        try:
                            with open(_lp, "w") as _lf:
                                _lf.write(_content)
                            lf.log.info(f"[ViewportExport] BW2A log written -> {_lp}")
                        except Exception as _le2:
                            lf.log.warn(f"[ViewportExport] log write failed for {_lp}: {_le2}")
                except Exception as _le:
                    lf.log.warn(f"[ViewportExport] BW2A log build failed: {_le}\n{_tb2.format_exc()}")
                    self._set_status(f"Log build failed: {_le}", error=True)

            except Exception as e:
                import traceback as _tb
                _msg = _tb.format_exc()
                self._set_status(f"Ortho BW2A failed: {e}", error=True)
                lf.log.error(f"[ViewportExport] ortho BW2A failed: {e}")
                lf.log.error(f"[ViewportExport] TRACEBACK:\n{_msg}")

            finally:
                if orig_eye is not None:
                    lf.set_camera(eye=orig_eye, target=orig_target, up=orig_up)
                lf.log.info("[ViewportExport] ortho BW2A: camera restored")
                lf.remove_draw_handler("viewport_export.ortho_bw2a")

    def _ortho_bw2a_abort(self, rs):
        orig = self._ortho_bw2a_state.get("orig_bg")
        if orig is not None:
            rs.background_color = orig
        self._ortho_bw2a_state["step"] = 0
        orig_eye    = self._ortho_bw2a_state.get("orig_eye")
        orig_target = self._ortho_bw2a_state.get("orig_target")
        orig_up     = self._ortho_bw2a_state.get("orig_up")
        if orig_eye:
            lf.set_camera(eye=orig_eye, target=orig_target, up=orig_up)
        lf.remove_draw_handler("viewport_export.ortho_bw2a")

    # ── RML helpers ───────────────────────────────────────────────────────────

    def _dirty(self, *fields):
        if not self._handle:
            return
        for f in fields:
            self._handle.dirty(f)

    def _dirty_all(self):
        self._dirty(
            "has_status", "status_text", "status_class",
            "resolution_idx", "resolution_hint",
            "format_idx", "is_jpg", "is_png", "fmt_label",
            "quality_str", "png_compress_str",
            "transparency", "ortho_alpha",
            "has_cropbox", "no_cropbox", "crop_x_label", "crop_z_label",
            "has_coord_txt", "no_coord_txt", "coord_txt_name",
            "convention_label",
            "tile_px_per_m_str", "tile_info_label", "tile_active", "tile_alpha",
            "mosaic_format_idx", "mosaic_crop", "mosaic_epsg_str",
            "mosaic_use_last_dir", "mosaic_tile_dir_label",
            "mosaic_running", "mosaic_show_browse",
        )

    def _status_class(self) -> str:
        s = self._status
        if not s:
            return "text-default"
        sl = s.lower()
        if "saved" in sl or "done" in sl:
            return "text-accent"
        if "failed" in sl or "error" in sl:
            return "text-muted"
        return "text-default"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_bind_model(self, ctx):
        model = ctx.create_data_model("viewport_export_panel")

        model.bind_func("has_status",   lambda: bool(self._status))
        model.bind_func("status_text",  lambda: self._status)
        model.bind_func("status_class", self._status_class)

        model.bind(
            "resolution_idx",
            lambda: str(self._resolution_idx),
            lambda v: (setattr(self, "_resolution_idx", int(v)),
                       self._dirty("resolution_idx", "resolution_hint")),
        )
        model.bind_func("resolution_hint", lambda: (
            f"  Height: {RESOLUTIONS[self._resolution_idx][1]} px (width from viewport aspect ratio)"
            if RESOLUTIONS[self._resolution_idx][1]
            else "  Native viewport resolution"
        ))

        model.bind(
            "format_idx",
            lambda: str(self._format_idx),
            lambda v: (setattr(self, "_format_idx", int(v)),
                       self._dirty("format_idx", "is_jpg", "is_png", "fmt_label")),
        )
        model.bind_func("is_jpg",    lambda: self._format_idx == 0)
        model.bind_func("is_png",    lambda: self._format_idx == 1)
        model.bind_func("fmt_label", lambda: FORMATS[self._format_idx])

        model.bind(
            "quality_str",
            lambda: str(self._quality),
            lambda v: (setattr(self, "_quality", int(float(v))),
                       self._dirty("quality_str")),
        )

        model.bind(
            "png_compress_str",
            lambda: str(self._png_compress),
            lambda v: (setattr(self, "_png_compress", int(float(v))),
                       self._dirty("png_compress_str")),
        )

        model.bind(
            "transparency",
            lambda: self._transparency,
            lambda v: (setattr(self, "_transparency", bool(v)),
                       self._dirty("transparency")),
        )

        model.bind_func("convention_label",
                        lambda: f"  {'+Y-up' if Y_UP else '-Y-up'}  lichtfeld {lf.__version__}")

        model.bind_func("has_cropbox",  lambda: self._crop_xmin is not None)
        model.bind_func("no_cropbox",   lambda: self._crop_xmin is None)
        model.bind_func("crop_x_label", lambda: (
            f"{self._crop_xmin:.2f} → {self._crop_xmax:.2f}  "
            f"({self._crop_xmax - self._crop_xmin:.2f} m)"
            if self._crop_xmin is not None else ""))
        model.bind_func("crop_z_label", lambda: (
            f"{self._crop_zmin:.2f} → {self._crop_zmax:.2f}  "
            f"({self._crop_zmax - self._crop_zmin:.2f} m)"
            if self._crop_zmin is not None else ""))

        model.bind_func("has_coord_txt",  lambda: bool(self._ortho_coord_txt))
        model.bind_func("no_coord_txt",   lambda: not bool(self._ortho_coord_txt))
        model.bind_func("coord_txt_name", lambda: (
            Path(self._ortho_coord_txt).name if self._ortho_coord_txt else ""))

        model.bind(
            "ortho_alpha",
            lambda: self._ortho_alpha,
            lambda v: (setattr(self, "_ortho_alpha", bool(v)),
                       self._dirty("ortho_alpha")),
        )

        model.bind(
            "ortho_res_idx",
            lambda: str(self._ortho_res_idx),
            lambda v: (setattr(self, "_ortho_res_idx", int(v)),
                       self._dirty("ortho_res_idx")),
        )

        model.bind_event("do_export",           self._on_do_export)
        model.bind_event("do_set_topdown",      self._on_set_topdown)
        model.bind_event("do_read_cropbox",     self._on_read_cropbox)
        model.bind_event("do_browse_coords",    self._on_browse_coords)
        model.bind_event("do_ortho_export",     self._on_do_ortho_export)
        model.bind_event("do_tile_export",      self._on_do_tile_export)
        model.bind_event("do_browse_tile_dir",  self._on_browse_tile_dir)
        model.bind_event("do_mosaic",           self._on_do_mosaic)

        # Tile bindings
        model.bind(
            "tile_px_per_m_str",
            lambda: f"{self._tile_px_per_m:.6g}",
            lambda v: (setattr(self, "_tile_px_per_m", max(1e-9, float(v))),
                       self._dirty("tile_px_per_m_str", "tile_info_label")),
        )
        model.bind(
            "tile_alpha",
            lambda: self._tile_alpha,
            lambda v: (setattr(self, "_tile_alpha", bool(v)),
                       self._dirty("tile_alpha")),
        )
        model.bind_func("tile_info_label", self._tile_info_label)
        model.bind_func("tile_active",     lambda: self._tile_state["active"])

        # Mosaic bindings
        model.bind(
            "mosaic_format_idx",
            lambda: str(self._mosaic_format_idx),
            lambda v: (setattr(self, "_mosaic_format_idx", int(v)),
                       self._dirty("mosaic_format_idx")),
        )
        model.bind(
            "mosaic_crop",
            lambda: self._mosaic_crop,
            lambda v: (setattr(self, "_mosaic_crop", bool(v)),
                       self._dirty("mosaic_crop")),
        )
        model.bind(
            "mosaic_epsg_str",
            lambda: self._mosaic_epsg,
            lambda v: (setattr(self, "_mosaic_epsg", v.strip()),
                       self._dirty("mosaic_epsg_str")),
        )
        model.bind(
            "mosaic_use_last_dir",
            lambda: self._mosaic_use_last_dir,
            lambda v: (setattr(self, "_mosaic_use_last_dir", bool(v)),
                       self._dirty("mosaic_use_last_dir", "mosaic_show_browse",
                                   "mosaic_tile_dir_label")),
        )
        model.bind_func("mosaic_show_browse",
                        lambda: not self._mosaic_use_last_dir)
        model.bind_func("mosaic_tile_dir_label", self._mosaic_tile_dir_label)
        model.bind_func("mosaic_running", lambda: self._mosaic_running)

        self._handle = model.get_handle()

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_do_export(self, handle, event, args):
        self._do_export()
        self._dirty("has_status", "status_text", "status_class")

    def _on_set_topdown(self, handle, event, args):
        self._set_topdown_camera()
        self._dirty("has_status", "status_text", "status_class")

    def _on_read_cropbox(self, handle, event, args):
        self._read_cropbox_and_zoom()
        self._dirty("has_cropbox", "no_cropbox", "crop_x_label", "crop_z_label",
                    "has_status", "status_text", "status_class")

    def _on_browse_coords(self, handle, event, args):
        p = _open_dialog("Select Coordinate TXT", "Text files (*.txt)|*.txt")
        if p:
            self._ortho_coord_txt = p
            try:
                _e, _n, epsg = _read_coord_txt(p)
                if epsg:
                    self._mosaic_epsg = epsg
                    lf.log.info(f"[ViewportExport] coord txt EPSG auto-fill: {epsg}")
            except Exception as _ex:
                lf.log.warn(f"[ViewportExport] could not auto-fill EPSG: {_ex}")
        self._dirty("has_coord_txt", "no_coord_txt", "coord_txt_name", "mosaic_epsg_str")

    def _on_do_ortho_export(self, handle, event, args):
        self._do_ortho_export()
        self._dirty("has_status", "status_text", "status_class")

    def _on_do_tile_export(self, handle, event, args):
        self._do_tile_export()
        self._dirty("has_status", "status_text", "status_class", "tile_active", "tile_info_label")

    def _on_browse_tile_dir(self, handle, event, args):
        p = _open_dialog("Select folder containing R##C## tile PNGs",
                         "PNG Image (*.png)|*.png")
        if p:
            pp = Path(p)
            self._mosaic_tile_dir = str(pp.parent if pp.is_file() else pp)
        self._dirty("mosaic_tile_dir_label", "mosaic_show_browse")

    def _on_do_mosaic(self, handle, event, args):
        self._do_mosaic()
        self._dirty("has_status", "status_text", "status_class", "mosaic_running")

    # ── Standard viewport export ──────────────────────────────────────────────

    def _do_export(self):
        is_png = self._format_idx == 1
        _, target_h = RESOLUTIONS[self._resolution_idx]
        default_name = "viewport_export.png" if is_png else "viewport_export.jpg"

        if is_png:
            path = _save_dialog(default_name, "Save Viewport as PNG",
                                "PNG Image (*.png)|*.png", "png")
        else:
            path = _save_dialog(default_name, "Save Viewport as JPG",
                                "JPEG Image (*.jpg)|*.jpg", "jpg")
        if not path:
            path = _default_path(default_name)

        ext = ".png" if is_png else ".jpg"
        if not path.lower().endswith(ext):
            path += ext

        if is_png and self._transparency:
            self._bw2a_state.update({
                "step": 1,
                "black": None,
                "white": None,
                "orig_bg": None,
                "out_path": path,
                "target_h": target_h,
            })
            self._set_status("Starting BW2A capture...", warning=True)
            self._register_draw_handler()
        else:
            try:
                self._set_status("Capturing...", warning=True)
                arr = _capture_arr()
                if arr is None:
                    self._set_status("Capture failed.", error=True)
                    return
                img = _arr_to_image(arr)
                if target_h:
                    img = _resize(img, target_h)
                if is_png:
                    img.save(path, "PNG", compress_level=self._png_compress)
                else:
                    img.save(path, "JPEG", quality=self._quality)
                self._set_status(f"Saved: {path}", success=True)
                lf.log.info(f"[ViewportExport] done: {path}")
            except Exception as e:
                self._set_status(f"Error: {e}", error=True)
                lf.log.error(f"[ViewportExport] {e}")

    # ── Camera helpers ────────────────────────────────────────────────────────

    def _set_topdown_camera(self):
        def _apply():
            cam = lf.get_camera()
            t   = cam.target
            lf.set_orthographic(False)
            lf.set_camera(
                eye    = (float(t[0]), float(t[1]) + ORTHO_EYE_H, float(t[2])),
                target = (float(t[0]), float(t[1]),                float(t[2])),
                up     = (0.0, 0.0, 1.0),
            )
            lf.set_orthographic(True)
        lf.ui.schedule_on_ui_thread(_apply)
        self._set_status("Camera set top-down — scroll to zoom, then Export", warning=True)

    def _read_cropbox_and_zoom(self):
        try:
            scene   = lf.get_scene()
            nodes   = scene.get_nodes()
            cb_node = next((n for n in nodes if "cropbox" in n.name.lower()), None)
            if cb_node is None:
                self._set_status("No cropbox node found — add one in lichtfeld first", error=True)
                return
            cb  = cb_node.cropbox()
            wt  = cb_node.world_transform
            sx  = float(wt[0][0]); tx = float(wt[0][3])
            sy  = float(wt[1][1]); ty = float(wt[1][3])
            sz  = float(wt[2][2]); tz = float(wt[2][3])
            self._crop_xmin = float(cb.min[0]) * sx + tx
            self._crop_xmax = float(cb.max[0]) * sx + tx
            self._crop_zmin = float(cb.min[2]) * sz + tz
            self._crop_zmax = float(cb.max[2]) * sz + tz
            ground_y        = float(cb.min[1]) * sy + ty
            lf.log.info(
                f"[ViewportExport] cropbox read  "
                f"X[{self._crop_xmin:.3f}->{self._crop_xmax:.3f}]  "
                f"Z[{self._crop_zmin:.3f}->{self._crop_zmax:.3f}]"
            )
        except Exception as e:
            self._set_status(f"Cropbox read error: {e}", error=True)
            return

        cx      = (self._crop_xmin + self._crop_xmax) / 2.0
        cz      = (self._crop_zmin + self._crop_zmax) / 2.0
        self._crop_cx       = cx
        self._crop_cz       = cz
        self._crop_ground_y = ground_y

        crop_h  = self._crop_zmax - self._crop_zmin
        half_h  = crop_h / 2.0
        fov_deg = math.degrees(2.0 * math.atan(half_h / ORTHO_EYE_H))

        def _apply_camera():
            lf.set_orthographic(False)
            lf.set_camera(
                eye    = (cx, ground_y + ORTHO_EYE_H, -cz),
                target = (cx, ground_y, -cz),
                up     = (0.0, 0.0, 1.0),
            )
            lf.set_camera_fov(fov_deg)
            lf.set_orthographic(True)

        lf.ui.schedule_on_ui_thread(_apply_camera)

        _hh = math.tan(math.radians(fov_deg / 2)) * ORTHO_EYE_H
        lf.log.info(
            f"[ViewportExport] zoomed  cx={cx:.3f} cz={cz:.3f} fov={fov_deg:.4f}  "
            f"crop {self._crop_xmax - self._crop_xmin:.2f} x {crop_h:.2f} m"
        )
        lf.log.info(
            f"[ViewportExport] FRAME EXTENTS "
            f"zmin={cz - _hh:.3f} zmax={cz + _hh:.3f} H={_hh * 2:.3f}m  "
            f"FOV={fov_deg:.4f} pixel_size={crop_h / 4320:.6f}m/px"
        )
        self._set_status(
            f"Crop {self._crop_xmax - self._crop_xmin:.1f}x{crop_h:.1f}m  "
            f"FOV={fov_deg:.2f} ps={crop_h / 4320:.4f}m/px", warning=True)

    # ── Ortho export ──────────────────────────────────────────────────────────

    def _do_ortho_export(self):
        cam         = lf.get_camera()
        orig_eye    = tuple(cam.eye)
        orig_target = tuple(cam.target)
        orig_up     = tuple(cam.up)
        orig_fov    = float(cam.fov)

        lf.log.info(f"[ViewportExport] ortho start  fov={orig_fov:.4f}  target={orig_target}")

        try:
            # ── Cropbox or live view ──────────────────────────────────────────
            if self._crop_xmin is not None and self._crop_cx is not None:
                ortho_cx = self._crop_cx
                ortho_cz = self._crop_cz
                ground_y = self._crop_ground_y
                crop_w   = self._crop_xmax - self._crop_xmin
                crop_h   = self._crop_zmax - self._crop_zmin
                use_crop = True
            else:
                v        = lf.get_current_view()
                ortho_cx = float(cam.target[0])
                ortho_cz = -float(cam.target[2])
                ground_y = float(cam.target[1])
                extent   = float(v.ortho_view_extent_world)
                vp0      = lf.capture_viewport()
                arr0     = np.asarray(vp0.image.cpu(), dtype=np.float32)
                vp_h0, vp_w0 = arr0.shape[:2]
                crop_h   = extent
                crop_w   = extent * (vp_w0 / vp_h0)
                use_crop = False
                lf.log.info(f"[ViewportExport] no cropbox — live extent={extent:.3f}m")

            # ── Coords ────────────────────────────────────────────────────────
            if self._ortho_coord_txt:
                try:
                    centre_e, centre_n, _ = _read_coord_txt(self._ortho_coord_txt)
                    lf.log.info(f"[ViewportExport] coords E={centre_e} N={centre_n}")
                except Exception as e:
                    self._set_status(f"Coord TXT error: {e}", error=True)
                    return
            else:
                centre_e = centre_n = None

            # ── Position camera top-down ──────────────────────────────────────
            self._set_status("Ortho: positioning camera...", warning=True)
            if use_crop:
                lf.set_camera(
                    eye    = (ortho_cx, ground_y + ORTHO_EYE_H, -ortho_cz),
                    target = (ortho_cx, ground_y, -ortho_cz),
                    up     = (0.0, 0.0, 1.0),
                )

            # ── Measure viewport ──────────────────────────────────────────────
            vp0 = lf.capture_viewport()
            if vp0 is None or vp0.image is None:
                self._set_status("Ortho: initial capture failed.", error=True)
                return
            arr0   = np.asarray(vp0.image.cpu(), dtype=np.float32)
            vp_h, vp_w = arr0.shape[:2]
            _, ortho_target_h = ORTHO_RESOLUTIONS[self._ortho_res_idx]
            if ortho_target_h is None:
                export_h = _snap2(vp_h)
                export_w = _snap2(vp_w)
            else:
                export_h = ortho_target_h
                export_w = _snap2(int(round(vp_w * export_h / vp_h))) if vp_h else export_h
            lf.log.info(f"[ViewportExport] ortho size {export_w}x{export_h}  viewport {vp_w}x{vp_h}")

            # ── Save dialog ───────────────────────────────────────────────────
            res_label    = ORTHO_RESOLUTIONS[self._ortho_res_idx][0].lower().replace(" ", "_")
            default_name = f"ortho_top_{res_label}.png"
            path = _save_dialog(default_name, "Save Ortho Export as PNG",
                                "PNG Image (*.png)|*.png", "png")
            if not path:
                path = _default_path(default_name)
            if not path.lower().endswith(".png"):
                path += ".png"

            # ── BW2A alpha path ───────────────────────────────────────────────
            if self._ortho_alpha:
                self._ortho_bw2a_state.update({
                    "step": 1,
                    "black": None,
                    "white": None,
                    "orig_bg": None,
                    "path": path,
                    "export_w": export_w,
                    "export_h": export_h,
                    "orig_eye": orig_eye,
                    "orig_target": orig_target,
                    "orig_up": orig_up,
                    "orig_fov": orig_fov,
                    "ortho_cx": ortho_cx,
                    "ortho_cz": ortho_cz,
                    "ground_y": ground_y,
                    "crop_xmin": self._crop_xmin if use_crop else None,
                    "crop_xmax": self._crop_xmax,
                    "crop_zmin": self._crop_zmin,
                    "crop_zmax": self._crop_zmax,
                    "vp_w": vp_w,
                    "vp_h": vp_h,
                    "centre_e": centre_e,
                    "centre_n": centre_n,
                })
                _state    = self._ortho_bw2a_state
                _use_crop = use_crop
                _cx       = ortho_cx
                _cz       = ortho_cz
                _gy       = ground_y
                def _start_capture():
                    if _use_crop:
                        lf.set_camera(
                            eye    = (_cx, _gy + ORTHO_EYE_H, -_cz),
                            target = (_cx, _gy, -_cz),
                            up     = (0.0, 0.0, 1.0),
                        )
                    _state["step"] = 1
                lf.ui.schedule_on_ui_thread(_start_capture)
                lf.add_draw_handler("viewport_export.ortho_bw2a", self._ortho_bw2a_draw_handler)
                self._set_status("Ortho: BW2A capture started...", warning=True)
                return

            # ── Standard RGB capture ──────────────────────────────────────────
            self._set_status("Ortho: capturing...", warning=True)
            arr = _capture_arr()
            if arr is None:
                self._set_status("Ortho: capture failed.", error=True)
                return
            arr = _mirror_lr(_rotate180(arr))
            lf.log.info(f"[ViewportExport] captured  shape={arr.shape}")

            img = _arr_to_image(arr)
            img = img.resize((export_w, export_h), Image.LANCZOS)
            lf.log.info(f"[ViewportExport] resized to {export_w}x{export_h}")

            # FIX 2: pixel_size uses export_h not vp_h
            extent = None  # guard — set inside try below
            try:
                _v     = lf.get_current_view()
                extent = float(_v.ortho_view_extent_world)
                pixel_size = extent / export_h
                lf.log.info(
                    f"[ViewportExport] pixel_size = {extent:.4f} / {export_h} "
                    f"= {pixel_size:.8f} m/px"
                )
            except Exception as _e:
                lf.log.warn(f"[ViewportExport] ortho_view_extent_world failed: {_e} — fallback")
                pixel_size = crop_h / export_h if crop_h else 1.0

            # ── Crop to cropbox ───────────────────────────────────────────────
            left = top = 0
            right, bottom = export_w, export_h
            if use_crop:
                px_per_m = 1.0 / pixel_size

                cx_off = (self._crop_xmin + self._crop_xmax) / 2.0 - ortho_cx
                cz_off = (self._crop_zmin + self._crop_zmax) / 2.0 - ortho_cz

                img_cx_px      = export_w / 2.0 + cx_off * px_per_m
                img_cz_px      = export_h / 2.0 + cz_off * px_per_m
                half_crop_w_px = (crop_w / 2.0) * px_per_m
                half_crop_h_px = (crop_h / 2.0) * px_per_m

                left   = int(round(img_cx_px - half_crop_w_px))
                right  = int(round(img_cx_px + half_crop_w_px))
                top    = int(round(img_cz_px - half_crop_h_px))
                bottom = int(round(img_cz_px + half_crop_h_px))
                left, right = max(0, left), min(export_w, right)
                top, bottom = max(0, top),  min(export_h, bottom)
                img = img.crop((left, top, right, bottom))
                cropped_w, cropped_h = img.size
                lf.log.info(
                    f"[ViewportExport] cropbox crop ({left},{top})->({right},{bottom})  "
                    f"output {cropped_w}x{cropped_h}"
                )
            else:
                cropped_w, cropped_h = img.size
                lf.log.info(f"[ViewportExport] no crop — full viewport {cropped_w}x{cropped_h}")

            img.save(path, "PNG")
            lf.log.info(f"[ViewportExport] ortho PNG -> {path}  pixel_size={pixel_size:.8f} m/px")

            # ── World file ────────────────────────────────────────────────────
            if centre_e is not None:
                # Use ortho_cx/cz — the actual capture-centre in model space.
                # Do NOT re-read lf.get_camera() here: the finally block has
                # already restored the original camera, so target no longer
                # reflects where we captured.
                _cx_world =  float(ortho_cx)          # model X  → east offset
                _cz_world =  float(ortho_cz)           # model Z already negated (north)
                _half_w_m = pixel_size * cropped_w / 2.0
                _half_h_m = pixel_size * cropped_h / 2.0
                # centre_e/N is real-world coord of model origin (0,0).
                # lichtfeld +X = West (opposite QGIS easting) → subtract cx
                # lichtfeld +Z = South; ortho_cz negated → north-positive → add
                # already north-positive. Symptom (top-right→bottom-left)
                # showed offsets were inverted: now subtract model offsets.
                tl_e = centre_e - _cx_world - _half_w_m
                tl_n = centre_n - _cz_world + _half_h_m
                lf.log.info(
                    f"[ViewportExport] world file: cx={_cx_world:.3f} cz_north={_cz_world:.3f} "
                    f"half_w={_half_w_m:.3f}m half_h={_half_h_m:.3f}m "
                    f"tl_e={tl_e:.3f} tl_n={tl_n:.3f} ps={pixel_size:.8f}"
                )
                world_path = _write_world_file(path, tl_e, tl_n, pixel_size)
                lf.log.info(f"[ViewportExport] world file -> {world_path}")
                self._set_status(
                    f"Saved: {Path(path).name}  |  {pixel_size:.6f} m/px  "
                    f"{cropped_w}x{cropped_h}px", success=True)
            else:
                self._set_status(
                    f"Saved: {Path(path).name}  (no world file)  "
                    f"{cropped_w}x{cropped_h}px", success=True)

            # ── Log file ──────────────────────────────────────────────────────
            log_path = str(Path(path).with_suffix(".log.txt"))
            try:
                with open(log_path, "w") as lt:
                    lt.write("Ortho Export Log\n")
                    lt.write("=" * 40 + "\n")
                    lt.write(f"Build:        2026-05-06o\n")
                    lt.write(f"PNG:          {path}\n")
                    lt.write(f"Resolution:   {ORTHO_RESOLUTIONS[self._ortho_res_idx][0]} "
                             f"({export_w}x{export_h} pre-crop)\n")
                    lt.write(f"Alpha (BW2A): {'yes' if self._ortho_alpha else 'no'}\n")
                    lt.write(f"Export size:  {cropped_w} x {cropped_h} px\n\n")
                    lt.write(f"Camera FOV:   {orig_fov:.4f} deg\n")
                    lt.write(f"Eye height:   {ORTHO_EYE_H:.1f} m (fixed)\n\n")
                    if use_crop:
                        lt.write("Cropbox extents (metres):\n")
                        lt.write(f"  xmin: {self._crop_xmin:.4f} m\n")
                        lt.write(f"  xmax: {self._crop_xmax:.4f} m\n")
                        lt.write(f"  zmin: {self._crop_zmin:.4f} m\n")
                        lt.write(f"  zmax: {self._crop_zmax:.4f} m\n")
                        lt.write(f"  width  (X): {crop_w:.4f} m\n")
                        lt.write(f"  depth  (Z): {crop_h:.4f} m\n\n")
                        lt.write("Crop pixel bounds (in pre-crop image):\n")
                        lt.write(f"  left={left}  right={right}  top={top}  bottom={bottom}\n\n")
                    lt.write("Resolution:\n")
                    if extent is not None:
                        lt.write(f"  ortho_view_extent: {extent:.4f} m\n")
                    else:
                        lt.write(f"  ortho_view_extent: (unavailable)\n")
                    lt.write(f"  export_h:          {export_h} px\n")
                    lt.write(f"  m/pixel:           {pixel_size:.8f} m/px\n")
                    lt.write(f"  px/metre:          {1.0 / pixel_size:.4f} px/m\n\n")
                    if centre_e is not None:
                        lt.write("Coordinates:\n")
                        lt.write(f"  Origin Easting:  {centre_e:.4f}\n")
                        lt.write(f"  Origin Northing: {centre_n:.4f}\n")
                        lt.write(f"  TL Easting:      {tl_e:.4f}\n")
                        lt.write(f"  TL Northing:     {tl_n:.4f}\n")
                lf.log.info(f"[ViewportExport] log -> {log_path}")
            except Exception as e:
                lf.log.warn(f"[ViewportExport] could not write log: {e}")

        except Exception as e:
            self._set_status(f"Ortho failed: {e}", error=True)
            lf.log.error(f"[ViewportExport] ortho failed: {e}")

        finally:
            # Camera intentionally NOT restored — user's camera position is preserved.
            lf.log.info("[ViewportExport] ortho: camera left at user position (no restore)")

    # ── Tiled ortho export ────────────────────────────────────────────────────

    def _tile_info_label(self) -> str:
        if self._crop_xmin is None:
            return "  (read cropbox first)"
        px_m     = self._tile_px_per_m
        m_per_px = 1.0 / px_m if px_m > 0 else 1.0
        crop_w   = self._crop_xmax - self._crop_xmin
        crop_h   = self._crop_zmax - self._crop_zmin
        vp_hint_w, vp_hint_h = 1920, 1080
        tile_w_m = vp_hint_w * m_per_px
        tile_h_m = vp_hint_h * m_per_px
        cols = math.ceil(crop_w / tile_w_m)
        rows = math.ceil(crop_h / tile_h_m)
        return (
            f"  {px_m:.4g} px/m  →  {m_per_px:.5f} m/px  "
            f"≈ {rows}R × {cols}C = {rows*cols} tiles  "
            f"(tile ≈{tile_w_m:.0f}×{tile_h_m:.0f}m at ~{vp_hint_w}×{vp_hint_h}px)"
        )

    def _tile_draw_handler(self, context):
        """3-step state machine: move+FOV → settle → capture → next tile."""
        s = self._tile_state
        if not s["active"]:
            lf.remove_draw_handler("viewport_export.tile")
            return

        # step 0: move camera + apply tile FOV
        if s["step"] == 0:
            idx = s["tile_idx"]
            if idx >= len(s["tiles"]):
                self._tile_finish()
                return
            _row, _col, cx, cz = s["tiles"][idx]
            ground_y    = s["ground_y"]
            tile_fov    = s["tile_fov_deg"]
            def _cam():
                lf.set_orthographic(False)
                lf.set_camera(
                    eye    = (cx, ground_y + ORTHO_EYE_H, -cz),
                    target = (cx, ground_y,                -cz),
                    up     = (0.0, 0.0, 1.0),
                )
                lf.set_camera_fov(tile_fov)
                lf.set_orthographic(True)
            lf.ui.schedule_on_ui_thread(_cam)
            s["step"]   = 1
            s["settle"] = 3
            return

        # step 1: settle
        if s["step"] == 1:
            if s["settle"] > 0:
                s["settle"] -= 1
                return
            s["step"] = 2
            return

        # step 2: capture (RGB) or BW2A sub-steps, save, advance
        if s["step"] == 2:
            idx = s["tile_idx"]
            row, col, cx, cz = s["tiles"][idx]
            n_total   = len(s["tiles"])
            centre_e  = s["centre_e"]
            centre_n  = s["centre_n"]
            path_stem = s["path_stem"]
            out_dir   = s["out_dir"]

            # ── BW2A sub-steps ────────────────────────────────────────────────
            if s["tile_alpha"]:
                rs = lf.get_render_settings()

                # sub_step 1: save bg, set black
                if s["sub_step"] == 1:
                    s["bw2a_orig_bg"] = rs.background_color
                    rs.background_color = (0.0, 0.0, 0.0)
                    s["sub_step"] = 2
                    return   # wait one frame

                # sub_step 2: capture black, set white
                elif s["sub_step"] == 2:
                    arr = _capture_arr()
                    if arr is None:
                        rs.background_color = s["bw2a_orig_bg"]
                        self._set_status(f"Tile BW2A black-bg capture failed at R{row:02d}C{col:02d}", error=True)
                        s["active"] = False
                        lf.remove_draw_handler("viewport_export.tile")
                        self._dirty("tile_active", "has_status", "status_text", "status_class")
                        return
                    s["bw2a_black"] = _mirror_lr(_rotate180(arr))
                    rs.background_color = (1.0, 1.0, 1.0)
                    s["sub_step"] = 3
                    return   # wait one frame

                # sub_step 3: capture white, restore bg, compute RGBA, save
                elif s["sub_step"] == 3:
                    arr = _capture_arr()
                    if arr is None:
                        rs.background_color = s["bw2a_orig_bg"]
                        self._set_status(f"Tile BW2A white-bg capture failed at R{row:02d}C{col:02d}", error=True)
                        s["active"] = False
                        lf.remove_draw_handler("viewport_export.tile")
                        self._dirty("tile_active", "has_status", "status_text", "status_class")
                        return
                    bw2a_white = _mirror_lr(_rotate180(arr))
                    rs.background_color = s["bw2a_orig_bg"]
                    s["sub_step"] = 0   # reset for next tile

                    try:
                        b_arr = (s["bw2a_black"][..., :3] * 255.0).clip(0, 255).astype(float) if s["bw2a_black"].dtype != float else s["bw2a_black"][..., :3]
                        w_arr = (bw2a_white[..., :3] * 255.0).clip(0, 255).astype(float)
                        # bw2a_black was stored as float32 [0,1] — scale to [0,255]
                        b_u8  = (s["bw2a_black"][..., :3] * 255.0).clip(0, 255).astype(float)
                        w_u8  = (bw2a_white[..., :3]      * 255.0).clip(0, 255).astype(float)
                        alpha     = np.clip(1.0 - np.mean(w_u8 - b_u8, axis=2) / 255.0, 0.0, 1.0)
                        recovered = np.clip(b_u8 / (alpha[:, :, np.newaxis] + 1e-10), 0, 255).astype(np.uint8)
                        alpha_u8  = (alpha * 255).astype(np.uint8)
                        arr_rgba  = np.dstack((recovered, alpha_u8))

                        # On first tile verify scale
                        if idx == 0:
                            try:
                                _v          = lf.get_current_view()
                                extent      = float(_v.ortho_view_extent_world)
                                cap_h       = arr_rgba.shape[0]
                                m_px_actual = extent / cap_h
                                s["verified_m_per_px"] = m_px_actual
                                s["tile_w_m"] = arr_rgba.shape[1] * m_px_actual
                                s["tile_h_m"] = cap_h * m_px_actual
                                req_m_per_px = 1.0 / s["tile_px_per_m"]
                                lf.log.info(
                                    f"[ViewportExport] tile BW2A scale: "
                                    f"requested {req_m_per_px:.6f} m/px  "
                                    f"actual {m_px_actual:.6f} m/px"
                                )
                            except Exception as _ve:
                                lf.log.warn(f"[ViewportExport] tile BW2A: could not verify scale: {_ve}")

                        m_per_px = (s["verified_m_per_px"]
                                    if s["verified_m_per_px"] is not None
                                    else 1.0 / s["tile_px_per_m"])

                        img      = Image.fromarray(arr_rgba, "RGBA")
                        tile_tag = f"R{row:02d}C{col:02d}"
                        out_path = str(Path(out_dir) / f"{path_stem}_{tile_tag}.png")
                        img.save(out_path, "PNG")
                        lf.log.info(f"[ViewportExport] tile BW2A {tile_tag} ({idx+1}/{n_total}) → {out_path}")

                        if centre_e is not None:
                            tile_w_px, tile_h_px = img.size
                            tl_e = centre_e - cx - (tile_w_px / 2.0) * m_per_px
                            tl_n = centre_n - cz + (tile_h_px / 2.0) * m_per_px
                            _write_world_file(out_path, tl_e, tl_n, m_per_px)

                        self._set_status(
                            f"Tile {tile_tag} α  {idx+1}/{n_total}  ({m_per_px:.5f} m/px)",
                            warning=True)
                        self._dirty("has_status", "status_text", "status_class")

                    except Exception as e:
                        import traceback as _tb
                        lf.log.error(f"[ViewportExport] tile BW2A error: {e}\n{_tb.format_exc()}")
                        self._set_status(f"Tile BW2A error at R{row:02d}C{col:02d}: {e}", error=True)
                        s["active"] = False
                        lf.remove_draw_handler("viewport_export.tile")
                        self._dirty("tile_active", "has_status", "status_text", "status_class")
                        return

                    # advance to next tile
                    s["tile_idx"] += 1
                    if s["tile_idx"] >= len(s["tiles"]):
                        self._tile_finish()
                    else:
                        s["step"] = 0
                    return

                else:
                    # sub_step == 0: kick off BW2A sequence on this frame
                    s["sub_step"] = 1
                    return

            # ── Standard RGB capture (tile_alpha == False) ────────────────────
            try:
                arr = _capture_arr()
                if arr is None:
                    raise RuntimeError("capture_viewport returned None")
                arr = _mirror_lr(_rotate180(arr))

                # On first tile verify actual scale from ortho_view_extent_world
                if idx == 0:
                    try:
                        _v              = lf.get_current_view()
                        extent          = float(_v.ortho_view_extent_world)
                        cap_h           = arr.shape[0]
                        m_px_actual     = extent / cap_h
                        s["verified_m_per_px"] = m_px_actual
                        s["tile_w_m"]   = arr.shape[1] * m_px_actual
                        s["tile_h_m"]   = cap_h        * m_px_actual
                        req_m_per_px    = 1.0 / s["tile_px_per_m"]
                        lf.log.info(
                            f"[ViewportExport] tile scale: "
                            f"requested {req_m_per_px:.6f} m/px  "
                            f"actual {m_px_actual:.6f} m/px  "
                            f"tile {s['tile_w_m']:.3f}×{s['tile_h_m']:.3f}m"
                        )
                        if abs(m_px_actual - req_m_per_px) / req_m_per_px > 0.01:
                            lf.log.warn(
                                f"[ViewportExport] tile: scale mismatch "
                                f">1% — world files use actual"
                            )
                    except Exception as _ve:
                        lf.log.warn(
                            f"[ViewportExport] tile: could not verify scale: {_ve}")

                m_per_px = (s["verified_m_per_px"]
                            if s["verified_m_per_px"] is not None
                            else 1.0 / s["tile_px_per_m"])

                img      = _arr_to_image(arr).convert("RGBA")
                tile_tag = f"R{row:02d}C{col:02d}"
                out_path = str(Path(out_dir) / f"{path_stem}_{tile_tag}.png")
                img.save(out_path, "PNG")
                lf.log.info(
                    f"[ViewportExport] tile {tile_tag} ({idx+1}/{n_total}) → {out_path}")

                if centre_e is not None:
                    tile_w_px, tile_h_px = img.size
                    tl_e = centre_e - cx - (tile_w_px / 2.0) * m_per_px
                    tl_n = centre_n - cz + (tile_h_px / 2.0) * m_per_px
                    _write_world_file(out_path, tl_e, tl_n, m_per_px)

                self._set_status(
                    f"Tile {tile_tag}  {idx+1}/{n_total}  ({m_per_px:.5f} m/px)",
                    warning=True)
                self._dirty("has_status", "status_text", "status_class")

            except Exception as e:
                import traceback as _tb
                lf.log.error(f"[ViewportExport] tile error: {e}\n{_tb.format_exc()}")
                self._set_status(f"Tile error at R{row:02d}C{col:02d}: {e}", error=True)
                s["active"] = False
                lf.remove_draw_handler("viewport_export.tile")
                self._dirty("tile_active", "has_status", "status_text", "status_class")
                return

            s["tile_idx"] += 1
            if s["tile_idx"] >= len(s["tiles"]):
                self._tile_finish()
            else:
                s["step"] = 0

    def _tile_finish(self):
        s = self._tile_state
        lf.remove_draw_handler("viewport_export.tile")
        s["active"] = False
        n, out_dir  = len(s["tiles"]), s["out_dir"]
        self._last_tile_dir = out_dir
        self._set_status(
            f"Tiled export complete — {n} tiles saved  →  {out_dir}", success=True)
        self._dirty(
            "tile_active", "has_status", "status_text", "status_class",
            "mosaic_tile_dir_label",
        )
        lf.log.info(f"[ViewportExport] tiled export done: {n} tiles in {out_dir}")
        # ── Completion notification ────────────────────────────────────────────────────────
        _notify_tile_complete(n, out_dir)

    def _do_tile_export(self):
        """
        KEY SCALE FIX (build u):
        Rather than computing FOV from ORTHO_EYE_H (which gave 2× error), we:
          1. Read the CURRENT camera FOV and ortho_view_extent_world (at cropbox zoom)
          2. Derive current m/px from extent / vp_h
          3. Scale the FOV proportionally: new_extent = target_m/px × vp_h
             new_fov_half = atan(tan(cur_fov_half) × new_extent / cur_extent)
        This is independent of EYE_H and matches whatever lichtfeld's ortho
        projection actually does with the FOV angle.
        """
        s = self._tile_state
        if s["active"]:
            self._set_status("Tiled export already running.", warning=True)
            return
        if self._crop_xmin is None:
            self._set_status("Read a cropbox first.", error=True)
            return
        px_m = self._tile_px_per_m
        if px_m <= 0:
            self._set_status("px/m must be > 0.", error=True)
            return
        target_m_per_px = 1.0 / px_m

        # ── Measure current viewport + scale ──────────────────────────────────
        try:
            vp0 = lf.capture_viewport()
            if vp0 is None or vp0.image is None:
                self._set_status("Tile: viewport capture failed.", error=True)
                return
            arr0   = np.asarray(vp0.image.cpu(), dtype=np.float32)
            vp_h, vp_w = arr0.shape[:2]
        except Exception as e:
            self._set_status(f"Tile: viewport measure failed: {e}", error=True)
            return

        # Read current ortho extent + FOV (set by _read_cropbox_and_zoom)
        try:
            _v          = lf.get_current_view()
            cur_extent  = float(_v.ortho_view_extent_world)   # metres, full height
            cur_m_per_px = cur_extent / vp_h
        except Exception as e:
            self._set_status(f"Tile: could not read ortho_view_extent_world: {e}", error=True)
            return

        cur_cam     = lf.get_camera()
        cur_fov_deg = float(cur_cam.fov)

        # Scale FOV so new extent = target_m_per_px × vp_h
        target_extent   = target_m_per_px * vp_h
        cur_half_tan    = math.tan(math.radians(cur_fov_deg / 2.0))
        new_half_tan    = cur_half_tan * (target_extent / cur_extent)
        tile_fov_deg    = math.degrees(2.0 * math.atan(new_half_tan))

        # Warn if FOV exceeds lichtfeld's practical orthographic limit (~170 deg).
        # This happens when target_m_per_px > cur_m_per_px (zooming OUT), i.e.
        # the user wants fewer pixels per metre than the current cropbox view.
        # Values above ~170 deg will be silently clamped by the engine, causing the
        # actual scale to cap (the ~0.08298755 m/px symptom).
        _MAX_FOV = 170.0
        if tile_fov_deg > _MAX_FOV:
            actual_half_tan = math.tan(math.radians(_MAX_FOV / 2.0))
            actual_extent   = (actual_half_tan / cur_half_tan) * cur_extent
            actual_m_per_px = actual_extent / vp_h
            actual_px_m     = 1.0 / actual_m_per_px
            self._set_status(
                f"Requested {px_m:.5g} px/m requires FOV {tile_fov_deg:.1f}\u00b0 which exceeds "
                f"the engine limit (~{_MAX_FOV:.0f}\u00b0). "
                f"Max achievable at current zoom: \u2248{actual_px_m:.5g} px/m. "
                f"Zoom OUT further (scroll back) before clicking 'Set Ortho Plan View', "
                f"then retry.",
                error=True,
            )
            lf.log.error(
                f"[ViewportExport] tile FOV {tile_fov_deg:.2f}\u00b0 exceeds engine limit "
                f"(\u2248{_MAX_FOV:.0f}\u00b0) \u2014 max px/m at current zoom \u2248 {actual_px_m:.5g}"
            )
            return

        tile_w_m = vp_w * target_m_per_px
        tile_h_m = vp_h * target_m_per_px

        lf.log.info(
            f"[ViewportExport] tile scale: "
            f"cur_extent={cur_extent:.4f}m cur_m/px={cur_m_per_px:.6f} "
            f"cur_fov={cur_fov_deg:.5f}° "
            f"→ target_m/px={target_m_per_px:.6f} "
            f"target_extent={target_extent:.4f}m tile_fov={tile_fov_deg:.5f}°  "
            f"vp={vp_w}×{vp_h}  tile={tile_w_m:.3f}×{tile_h_m:.3f}m"
        )

        # ── Build tile grid ────────────────────────────────────────────────────
        crop_x_min = self._crop_xmin;  crop_x_max = self._crop_xmax
        crop_z_min = self._crop_zmin;  crop_z_max = self._crop_zmax
        n_cols = math.ceil((crop_x_max - crop_x_min) / tile_w_m)
        n_rows = math.ceil((crop_z_max - crop_z_min) / tile_h_m)

        tiles = []
        for r in range(n_rows):
            for c in range(n_cols):
                cx = crop_x_min + (c + 0.5) * tile_w_m
                cz = crop_z_min + (r + 0.5) * tile_h_m
                tiles.append((r + 1, c + 1, cx, cz))

        # ── Coords ────────────────────────────────────────────────────────────
        centre_e = centre_n = None
        if self._ortho_coord_txt:
            try:
                centre_e, centre_n, _ = _read_coord_txt(self._ortho_coord_txt)
            except Exception as e:
                self._set_status(f"Coord TXT error: {e}", error=True)
                return

        # ── Save dialog ────────────────────────────────────────────────────────
        default_stem = "tile_ortho"
        first_path = _save_dialog(
            f"{default_stem}_R01C01.png",
            "Save Tiled Ortho — name becomes stem for all tiles",
            "PNG Image (*.png)|*.png", "png",
        )
        if not first_path:
            first_path = _default_path(f"{default_stem}_R01C01.png")
        if not first_path.lower().endswith(".png"):
            first_path += ".png"

        out_dir   = str(Path(first_path).parent)
        stem_base = re.sub(r"_R\d+C\d+$", "", Path(first_path).stem)
        ground_y  = self._crop_ground_y if self._crop_ground_y is not None else 0.0

        s.update({
            "active":            True,
            "step":              0,
            "settle":            0,
            "tiles":             tiles,
            "tile_idx":          0,
            "path_stem":         stem_base,
            "out_dir":           out_dir,
            "vp_w":              vp_w,
            "vp_h":              vp_h,
            "tile_w_m":          tile_w_m,
            "tile_h_m":          tile_h_m,
            "tile_px_per_m":     px_m,
            "ref_fov_deg":       cur_fov_deg,
            "ref_m_per_px":      cur_m_per_px,
            "tile_fov_deg":      tile_fov_deg,
            "ground_y":          ground_y,
            "centre_e":          centre_e,
            "centre_n":          centre_n,
            "verified_m_per_px": None,
            "tile_alpha":        self._tile_alpha,
            "sub_step":          0,
            "bw2a_black":        None,
            "bw2a_white":        None,
            "bw2a_orig_bg":      None,
        })

        lf.add_draw_handler("viewport_export.tile", self._tile_draw_handler)
        _alpha_tag = "  [BW2A α]" if self._tile_alpha else ""
        self._set_status(
            f"Tiled export{_alpha_tag}: {n_rows}R × {n_cols}C = {len(tiles)} tiles "
            f"@ {px_m:.4g} px/m  ({target_m_per_px:.5f} m/px)  "
            f"tile {tile_w_m:.1f}×{tile_h_m:.1f}m",
            warning=True,
        )
        self._dirty("tile_active", "tile_info_label")

    # ── Mosaic from tiles ─────────────────────────────────────────────────────

    def _mosaic_tile_dir_label(self) -> str:
        if self._mosaic_use_last_dir:
            if self._last_tile_dir:
                return f"  ↳ {self._last_tile_dir}"
            return "  (no export run yet this session)"
        if self._mosaic_tile_dir:
            return f"  ↳ {self._mosaic_tile_dir}"
        return "  (none selected)"

    def _mosaic_effective_dir(self) -> str:
        return self._last_tile_dir if self._mosaic_use_last_dir else self._mosaic_tile_dir

    def _do_mosaic(self):
        if self._mosaic_running:
            self._set_status("Mosaic already running.", warning=True)
            return

        tile_dir = self._mosaic_effective_dir()
        if not tile_dir or not Path(tile_dir).is_dir():
            msg = ("No last export folder available — run a tiled export first, "
                   "or uncheck 'Use last export folder' and browse manually."
                   if self._mosaic_use_last_dir
                   else "Select a tile directory first.")
            self._set_status(msg, error=True)
            return

        fmt_idx  = self._mosaic_format_idx
        do_crop  = self._mosaic_crop
        epsg_str = self._mosaic_epsg.strip()

        if do_crop and self._crop_xmin is None:
            self._set_status("Load a cropbox before using 'Crop to cropbox'.", error=True)
            return

        crop_xmin = self._crop_xmin;  crop_xmax = self._crop_xmax
        crop_zmin = self._crop_zmin;  crop_zmax = self._crop_zmax

        centre_e = centre_n = None
        if self._ortho_coord_txt:
            try:
                centre_e, centre_n, _ = _read_coord_txt(self._ortho_coord_txt)
            except Exception as e:
                self._set_status(f"Coord TXT error: {e}", error=True)
                return

        ext   = ".tif"  if fmt_idx == 0 else ".jp2"
        filt  = ("GeoTIFF (*.tif)|*.tif" if fmt_idx == 0
                 else "JPEG2000 (*.jp2)|*.jp2")
        label = "GeoTIFF" if fmt_idx == 0 else "JPEG2000"

        stem_hint = re.sub(r"_R\d+C\d+$", "", Path(tile_dir).name or "mosaic")
        out_path  = _save_dialog(
            f"{stem_hint}_mosaic{ext}",
            f"Save Mosaic as {label}", filt, ext.lstrip("."),
        )
        if not out_path:
            out_path = _default_path(f"{stem_hint}_mosaic{ext}")
        if not out_path.lower().endswith(ext):
            out_path += ext

        self._mosaic_running = True
        self._dirty("mosaic_running", "has_status", "status_text", "status_class")
        self._set_status("Mosaic: assembling tiles…", warning=True)

        def _run():
            try:
                self._mosaic_worker(
                    tile_dir, out_path, fmt_idx, do_crop,
                    epsg_str, centre_e, centre_n,
                    crop_xmin, crop_xmax, crop_zmin, crop_zmax,
                )
            finally:
                self._mosaic_running = False
                self._dirty("mosaic_running", "has_status", "status_text", "status_class")

        threading.Thread(target=_run, daemon=True).start()

    def _mosaic_worker(
        self, tile_dir, out_path, fmt_idx, do_crop,
        epsg_str, centre_e, centre_n,
        crop_xmin, crop_xmax, crop_zmin, crop_zmax,
    ):
        try:
            import rasterio
            from rasterio.transform import from_origin
            from rasterio.crs import CRS
        except ImportError:
            self._set_status(
                "Mosaic requires rasterio — run: pip install rasterio", error=True)
            return

        try:
            # 1. Discover tiles + world files
            tile_dir_p = Path(tile_dir)
            pattern    = re.compile(r"_R(\d+)C(\d+)\.png$", re.IGNORECASE)
            tile_files = sorted(
                (f for f in tile_dir_p.glob("*.png") if pattern.search(f.name)),
                key=lambda f: pattern.search(f.name).groups()
            )
            if not tile_files:
                self._set_status("No R##C## tile PNGs found in folder.", error=True)
                return

            lf.log.info(
                f"[ViewportExport] mosaic: {len(tile_files)} tiles in {tile_dir}")

            # 2. Read .pgw world files
            tile_info    = []
            ref_m_per_px = None
            for tf in tile_files:
                pgw = tf.with_suffix(".pgw")
                if not pgw.exists():
                    lf.log.warn(f"[ViewportExport] mosaic: no .pgw for {tf.name} — skipping")
                    continue
                with open(pgw) as fh:
                    lines = [l.strip() for l in fh.readlines()]
                m_per_px = abs(float(lines[0]))
                tl_e, tl_n = float(lines[4]), float(lines[5])
                with Image.open(tf) as im:
                    w_px, h_px = im.size
                if ref_m_per_px is None:
                    ref_m_per_px = m_per_px
                elif abs(m_per_px - ref_m_per_px) / ref_m_per_px > 0.001:
                    lf.log.warn(
                        f"[ViewportExport] mosaic: {tf.name} m/px={m_per_px:.6f} "
                        f"≠ ref {ref_m_per_px:.6f}"
                    )
                tile_info.append((tl_e, tl_n, w_px, h_px, tf))

            if not tile_info:
                self._set_status("No tiles with valid .pgw files found.", error=True)
                return

            ps = ref_m_per_px

            # 3. Canvas bounds
            min_e = min(t[0]           for t in tile_info)
            max_e = max(t[0] + t[2]*ps for t in tile_info)
            max_n = max(t[1]           for t in tile_info)
            min_n = min(t[1] - t[3]*ps for t in tile_info)
            canvas_w = int(round((max_e - min_e) / ps))
            canvas_h = int(round((max_n - min_n) / ps))

            lf.log.info(
                f"[ViewportExport] mosaic canvas: {canvas_w}×{canvas_h}px  "
                f"E[{min_e:.2f}→{max_e:.2f}]  N[{min_n:.2f}→{max_n:.2f}]  "
                f"ps={ps:.6f} m/px"
            )

            # 4. Composite
            self._set_status(
                f"Mosaic: canvas {canvas_w}×{canvas_h}px — "
                f"compositing {len(tile_info)} tiles…", warning=True)
            canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)

            for i, (tl_e, tl_n, w_px, h_px, tf) in enumerate(tile_info):
                col_off = int(round((tl_e - min_e) / ps))
                row_off = int(round((max_n - tl_n) / ps))
                with Image.open(tf) as im:
                    arr = np.asarray(im.convert("RGBA"), dtype=np.uint8)
                r0, c0 = row_off, col_off
                r1 = min(r0 + arr.shape[0], canvas_h)
                c1 = min(c0 + arr.shape[1], canvas_w)
                canvas[r0:r1, c0:c1] = arr[:r1-r0, :c1-c0]
                if (i + 1) % 10 == 0 or i == len(tile_info) - 1:
                    self._set_status(
                        f"Mosaic: composited {i+1}/{len(tile_info)} tiles…",
                        warning=True)

            # 5. Crop to cropbox
            if do_crop and centre_e is not None:
                cb_e_min = centre_e - crop_xmax
                cb_e_max = centre_e - crop_xmin
                cb_n_min = centre_n - crop_zmax
                cb_n_max = centre_n - crop_zmin
                c0 = max(0, int(round((cb_e_min - min_e) / ps)))
                c1 = min(canvas_w, int(round((cb_e_max - min_e) / ps)))
                r0 = max(0, int(round((max_n - cb_n_max) / ps)))
                r1 = min(canvas_h, int(round((max_n - cb_n_min) / ps)))
                lf.log.info(
                    f"[ViewportExport] mosaic crop: rows {r0}→{r1}  cols {c0}→{c1}")
                canvas   = canvas[r0:r1, c0:c1]
                out_tl_e = min_e + c0 * ps
                out_tl_n = max_n - r0 * ps
            else:
                out_tl_e, out_tl_n = min_e, max_n

            out_h, out_w = canvas.shape[:2]

            # 6. Write GeoTIFF or JPEG2000
            self._set_status("Mosaic: writing output file…", warning=True)
            transform = from_origin(out_tl_e, out_tl_n, ps, ps)
            crs = None
            if epsg_str:
                try:
                    crs = CRS.from_epsg(int(epsg_str))
                except Exception as _ce:
                    lf.log.warn(f"[ViewportExport] mosaic: bad EPSG '{epsg_str}': {_ce}")

            bands     = np.moveaxis(canvas, -1, 0)   # (4, H, W)
            common_kw = dict(height=out_h, width=out_w, count=bands.shape[0],
                             dtype="uint8", transform=transform)
            if crs:
                common_kw["crs"] = crs

            if fmt_idx == 0:
                with rasterio.open(out_path, "w", driver="GTiff",
                                   compress="deflate", predictor=2,
                                   tiled=True, blockxsize=512, blockysize=512,
                                   interleave="band", **common_kw) as dst:
                    dst.write(bands)
            else:
                with rasterio.open(out_path, "w", driver="JP2OpenJPEG",
                                   **common_kw) as dst:
                    dst.write(bands)

            size_mb = Path(out_path).stat().st_size / 1_048_576
            self._set_status(
                f"Mosaic saved: {Path(out_path).name}  "
                f"{out_w}×{out_h}px  {size_mb:.1f} MB", success=True)
            lf.log.info(
                f"[ViewportExport] mosaic done: {out_path}  "
                f"{out_w}×{out_h}px  {size_mb:.1f}MB  crs={crs}"
            )

        except Exception as e:
            import traceback as _tb
            lf.log.error(f"[ViewportExport] mosaic failed: {e}\n{_tb.format_exc()}")
            self._set_status(f"Mosaic failed: {e}", error=True)

    def _set_status(self, msg, *, success=False, warning=False, error=False):
        self._status = msg
        self._dirty("has_status", "status_text", "status_class")