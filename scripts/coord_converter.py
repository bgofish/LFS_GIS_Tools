"""
RealityCapture XML Coordinate Converter
Converts lat/lon origins from RC coordinate XML to Easting/Northing
in a user-selected EPSG projection, then writes COORDS_000.txt
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import xml.etree.ElementTree as ET
import re, os, threading

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info, query_utm_crs_info

# ── Fallback list shown before any row is selected ────────────────────────────
COMMON_EPSG = [
    (2193,  "NZGD2000 / New Zealand TM"),
    (3793,  "NZGD2000 / Chatham Islands TM 2000"),
    (27200, "NZGD49 / New Zealand Map Grid"),
    (32701, "WGS 84 / UTM zone 1S"),
    (32755, "WGS 84 / UTM zone 55S"),
    (32756, "WGS 84 / UTM zone 56S"),
    (32760, "WGS 84 / UTM zone 60S"),
    (28355, "GDA94 / MGA zone 55"),
    (28356, "GDA94 / MGA zone 56"),
    (7855,  "GDA2020 / MGA zone 55"),
    (7856,  "GDA2020 / MGA zone 56"),
    (4326,  "WGS 84 (geographic, degrees)"),
    (3857,  "WGS 84 / Pseudo-Mercator (Web)"),
    (27700, "OSGB 1936 / British National Grid"),
    (2056,  "CH1903+ / LV95 (Switzerland)"),
    (25832, "ETRS89 / UTM zone 32N"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_xml(path: str):
    tree = ET.parse(path)
    root = tree.getroot()
    rows = []
    for cs in root.iter("cs"):
        cid    = cs.get("id", "?")
        desc   = cs.get("desc", "")
        params = cs.get("params", "")
        lat = lon = None
        m_lat = re.search(r'\+lat_0=([+-]?\d+\.?\d*)', params)
        m_lon = re.search(r'\+lon_0=([+-]?\d+\.?\d*)', params)
        if m_lat: lat = float(m_lat.group(1))
        if m_lon: lon = float(m_lon.group(1))
        rows.append((cid, desc, lat, lon))
    return rows


def query_epsg_for_point(lat: float, lon: float):
    """
    Return sorted list of (code, name) EPSG projected CRS that cover (lat,lon).
    Ordered: most-local first (smallest AOI bounding box area), then by code.
    Global/world-wide CRS are pushed to the bottom.
    """
    aoi = AreaOfInterest(lon, lat, lon, lat)

    # Projected CRS
    proj = query_crs_info(
        pj_types="PROJECTED_CRS",
        area_of_interest=aoi,
        contains=False,
    )
    epsg = [(int(c.code), c.name, c.area_of_use.bounds)
            for c in proj if c.auth_name == "EPSG"]

    def _area(bounds):
        w, s, e, n = bounds
        dlon = e - w if e >= w else (e + 360 - w)   # handle antimeridian
        dlat = n - s
        return dlon * dlat

    def _sort_key(item):
        code, name, bounds = item
        a = _area(bounds)
        # Treat area < 0 as very large (wrap-around artifact)
        if a <= 0:
            a = 999999
        return (a, code)

    epsg.sort(key=_sort_key)
    return [(code, name) for code, name, _ in epsg]


def convert(lat: float, lon: float, epsg: int):
    t = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    return t.transform(lon, lat)


def get_zone(epsg: int):
    try:
        crs = CRS.from_epsg(epsg)
        if crs.utm_zone:
            return crs.utm_zone
        m = re.search(r'zone\s+(\d+[NS]?)', crs.name, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return str(epsg)


# ── Main App ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RC Coordinate Converter")
        self.resizable(True, True)
        self.configure(bg="#0f1117")
        self.minsize(720, 640)

        self.xml_path   = tk.StringVar()
        self.rows       = []
        self.epsg_var   = tk.StringVar()
        self.elev_var   = tk.StringVar(value="")
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Drop or browse an XML file to begin.")

        # Cache: row_index -> list of (code, name)
        self._epsg_cache   = {}
        self._current_list = COMMON_EPSG[:]   # what's displayed now

        self._build_styles()
        self._build_ui()
        self._bind_dnd()

    # ── Styles ─────────────────────────────────────────────────────────────

    def _build_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        BG    = "#0d1117"
        PANEL = "#161b25"
        ACC   = "#4da6ff"
        ACC2  = "#2d8cff"
        TEXT  = "#dce6f5"
        DIM   = "#7a8fa8"
        SEL   = "#1a2d45"
        WARN  = "#f59e0b"

        UI   = ("Segoe UI", 10)
        UIS  = ("Segoe UI", 9)
        UIB  = ("Segoe UI", 10, "bold")
        UIH  = ("Segoe UI", 13, "bold")
        MONO = ("Consolas", 10)

        style.configure(".",              background=BG,    foreground=TEXT, font=UI)
        style.configure("TFrame",         background=BG)
        style.configure("Panel.TFrame",   background=PANEL)
        style.configure("TLabel",         background=BG,    foreground=TEXT, font=UI)
        style.configure("Dim.TLabel",     background=BG,    foreground=DIM,  font=UIS)
        style.configure("Warn.TLabel",    background=BG,    foreground=WARN, font=("Segoe UI", 9, "italic"))
        style.configure("Acc.TLabel",     background=BG,    foreground=ACC,  font=("Segoe UI", 9, "bold"))
        style.configure("Panel.TLabel",   background=PANEL, foreground=TEXT, font=UI)
        style.configure("Head.TLabel",    background=BG,    foreground=ACC,  font=UIH)
        style.configure("Acc.TButton",    background=ACC,   foreground="#000",
                         font=UIB, relief="flat", padding=(14, 6))
        style.map("Acc.TButton",
                  background=[("active", ACC2)],
                  relief=[("active", "flat")])
        style.configure("TEntry",         fieldbackground=PANEL, foreground=TEXT,
                         insertcolor=ACC, relief="flat", font=MONO)
        style.configure("Treeview",       background=PANEL, foreground=TEXT,
                         fieldbackground=PANEL, rowheight=26, font=UI)
        style.configure("Treeview.Heading", background="#0a0f18", foreground=ACC,
                         font=UIB)
        style.map("Treeview", background=[("selected", SEL)],
                              foreground=[("selected", ACC)])
        style.configure("Status.TLabel",  background="#0a0f18", foreground=DIM,
                         font=UIS, padding=(8, 4))

        self._colors = dict(BG=BG, PANEL=PANEL, ACC=ACC, ACC2=ACC2, TEXT=TEXT,
                            DIM=DIM, SEL=SEL, WARN=WARN)

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        C = self._colors
        root_pad = ttk.Frame(self, padding=20)
        root_pad.pack(fill="both", expand=True)

        # Title
        ttk.Label(root_pad, text="▸ RC COORDINATE CONVERTER",
                  style="Head.TLabel").pack(anchor="w", pady=(0, 16))

        # ── Drop zone ──
        drop_frame = tk.Frame(root_pad, bg=C["PANEL"],
                              highlightbackground=C["ACC"],
                              highlightthickness=1, cursor="hand2")
        drop_frame.pack(fill="x", pady=(0, 12))
        self._drop_label = tk.Label(
            drop_frame,
            text="⊕  Drop XML here  or  Click to Browse",
            bg=C["PANEL"], fg=C["DIM"],
            font=("Segoe UI", 11), pady=22
        )
        self._drop_label.pack(fill="x")
        drop_frame.bind("<Button-1>", lambda e: self._browse())
        self._drop_label.bind("<Button-1>", lambda e: self._browse())
        self._drop_frame = drop_frame

        path_row = ttk.Frame(root_pad)
        path_row.pack(fill="x", pady=(0, 12))
        ttk.Label(path_row, text="File:", style="Dim.TLabel").pack(side="left")
        ttk.Label(path_row, textvariable=self.xml_path,
                  style="Dim.TLabel").pack(side="left", padx=(6, 0))

        # ── XML rows table ──
        ttk.Label(root_pad, text="Select row to convert:",
                  style="Dim.TLabel").pack(anchor="w")

        tree_frame = ttk.Frame(root_pad, style="Panel.TFrame")
        tree_frame.pack(fill="x", pady=(4, 12))

        self.tree = ttk.Treeview(tree_frame,
                                  columns=("id", "desc", "lat", "lon"),
                                  show="headings", height=4,
                                  selectmode="browse")
        self.tree.heading("id",   text="ID")
        self.tree.heading("desc", text="Description")
        self.tree.heading("lat",  text="Lat₀")
        self.tree.heading("lon",  text="Lon₀")
        self.tree.column("id",   width=60,  anchor="center")
        self.tree.column("desc", width=280)
        self.tree.column("lat",  width=140, anchor="e")
        self.tree.column("lon",  width=140, anchor="e")

        sb = ttk.Scrollbar(tree_frame, orient="vertical",
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

        # ── EPSG section ──
        epsg_hdr = ttk.Frame(root_pad)
        epsg_hdr.pack(fill="x", pady=(0, 4))

        ttk.Label(epsg_hdr, text="Target EPSG:",
                  style="Dim.TLabel").pack(side="left")
        ttk.Label(epsg_hdr, text="  Search:",
                  style="Dim.TLabel").pack(side="left")
        search_entry = ttk.Entry(epsg_hdr, textvariable=self.search_var, width=28)
        search_entry.pack(side="left", padx=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._filter_epsg())

        # Auto-suggest badge
        self._epsg_badge = ttk.Label(epsg_hdr, text="", style="Acc.TLabel")
        self._epsg_badge.pack(side="left", padx=(14, 0))

        # EPSG listbox
        lb_frame = tk.Frame(root_pad, bg=C["PANEL"],
                             highlightbackground=C["DIM"],
                             highlightthickness=1)
        lb_frame.pack(fill="x", pady=(0, 4))

        self._epsg_lb = tk.Listbox(
            lb_frame, height=6, selectmode="single",
            bg=C["PANEL"], fg=C["TEXT"],
            selectbackground=C["SEL"], selectforeground=C["ACC"],
            font=("Segoe UI", 10), relief="flat",
            activestyle="none", bd=0
        )
        lb_sb = ttk.Scrollbar(lb_frame, orient="vertical",
                              command=self._epsg_lb.yview)
        self._epsg_lb.configure(yscrollcommand=lb_sb.set)
        self._epsg_lb.pack(side="left", fill="both", expand=True)
        lb_sb.pack(side="right", fill="y")
        self._epsg_lb.bind("<<ListboxSelect>>", self._on_epsg_select)

        # Manual code entry
        manual_row = ttk.Frame(root_pad)
        manual_row.pack(fill="x", pady=(2, 12))
        ttk.Label(manual_row, text="or enter code manually:",
                  style="Dim.TLabel").pack(side="left")
        ttk.Entry(manual_row, textvariable=self.epsg_var, width=10).pack(
            side="left", padx=(8, 0))

        self._populate_epsg(COMMON_EPSG)

        # ── Altitude ──
        elev_row = ttk.Frame(root_pad)
        elev_row.pack(fill="x", pady=(0, 16))
        ttk.Label(elev_row, text="Altitude / RL (m):",
                  style="Dim.TLabel").pack(side="left")
        ttk.Entry(elev_row, textvariable=self.elev_var, width=12).pack(
            side="left", padx=(10, 0))
        ttk.Label(elev_row, text="→ written to  Alt  &  m RL",
                  style="Dim.TLabel").pack(side="left", padx=(10, 0))

        # ── Convert button ──
        ttk.Button(root_pad, text="▶  CONVERT & SAVE",
                   style="Acc.TButton",
                   command=self._do_convert).pack(anchor="w")

        # ── Status bar ──
        ttk.Label(root_pad, textvariable=self.status_var,
                  style="Status.TLabel").pack(fill="x", pady=(12, 0))

    # ── EPSG list helpers ───────────────────────────────────────────────────

    def _populate_epsg(self, items):
        self._epsg_lb.delete(0, "end")
        self._current_list = items
        for code, desc in items:
            self._epsg_lb.insert("end", f"  {code:>6}   {desc}")

    def _filter_epsg(self):
        q = self.search_var.get().lower()
        base = getattr(self, "_auto_list", COMMON_EPSG)
        if not q:
            self._populate_epsg(base)
        else:
            self._populate_epsg([(c, d) for c, d in base
                                 if q in str(c) or q in d.lower()])

    def _on_epsg_select(self, _=None):
        sel = self._epsg_lb.curselection()
        if sel:
            code, _ = self._current_list[sel[0]]
            self.epsg_var.set(str(code))

    # ── Row selection → auto-populate EPSG ─────────────────────────────────

    def _on_row_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        _, _, lat, lon = self.rows[idx]
        if lat is None or lon is None:
            return

        self._epsg_badge.configure(text="⟳ querying…")
        self.status_var.set(f"Querying EPSG database for lat={lat:.5f}, lon={lon:.5f} …")
        self.update_idletasks()

        def _query():
            if idx in self._epsg_cache:
                result = self._epsg_cache[idx]
            else:
                try:
                    result = query_epsg_for_point(lat, lon)
                    self._epsg_cache[idx] = result
                except Exception as ex:
                    self.after(0, lambda: self.status_var.set(
                        f"EPSG query failed: {ex}"))
                    self.after(0, lambda: self._epsg_badge.configure(text=""))
                    return

            self.after(0, lambda: self._apply_auto_epsg(result, lat, lon))

        threading.Thread(target=_query, daemon=True).start()

    def _apply_auto_epsg(self, result, lat, lon):
        if not result:
            self._epsg_badge.configure(text="no results")
            return

        self._auto_list = result
        self.search_var.set("")          # clear filter so full list shows
        self._populate_epsg(result)

        # Auto-select the top (most local) entry
        self._epsg_lb.selection_clear(0, "end")
        self._epsg_lb.selection_set(0)
        self._epsg_lb.see(0)
        self.epsg_var.set(str(result[0][0]))

        self._epsg_badge.configure(
            text=f"✔ {len(result)} applicable CRS found — most local first")
        self.status_var.set(
            f"EPSG list updated for lat={lat:.5f}, lon={lon:.5f}  "
            f"({len(result)} results, sorted local→global)")

    # ── File handling ───────────────────────────────────────────────────────

    def _bind_dnd(self):
        try:
            from tkinterdnd2 import DND_FILES
            self._drop_frame.drop_target_register(DND_FILES)
            self._drop_frame.dnd_bind("<<Drop>>",
                lambda e: self._load_xml(e.data.strip("{}")))
            self._drop_label.configure(
                text="⊕  Drop XML here  or  Click to Browse")
        except Exception:
            self._drop_label.configure(
                text="⊕  Click to Browse XML file  (drag'n'drop: install tkinterdnd2)")

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select RealityCapture XML",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")]
        )
        if path:
            self._load_xml(path)

    def _load_xml(self, path: str):
        try:
            self.rows = parse_xml(path)
        except Exception as ex:
            messagebox.showerror("XML Error", f"Could not parse XML:\n{ex}")
            return

        self._epsg_cache.clear()
        self.xml_path.set(os.path.basename(path))
        self._drop_label.configure(text=f"✔  {os.path.basename(path)}",
                                   fg=self._colors["ACC"])

        for item in self.tree.get_children():
            self.tree.delete(item)
        for cid, desc, lat, lon in self.rows:
            lat_s = f"{lat:.6f}" if lat is not None else "—"
            lon_s = f"{lon:.6f}" if lon is not None else "—"
            self.tree.insert("", "end", values=(cid, desc, lat_s, lon_s))

        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
        self.status_var.set(
            f"Loaded {len(self.rows)} row(s). Select a row — EPSG list will update automatically.")

    # ── Conversion ─────────────────────────────────────────────────────────

    def _do_convert(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No row", "Please select a row.")
            return

        idx  = self.tree.index(sel[0])
        cid, desc, lat, lon = self.rows[idx]
        if lat is None or lon is None:
            messagebox.showerror("No coordinates",
                                 "Selected row has no lat/lon in params.")
            return

        epsg_str = self.epsg_var.get().strip()
        if not epsg_str:
            messagebox.showwarning("No EPSG", "Please select or enter an EPSG code.")
            return
        try:
            epsg = int(epsg_str)
        except ValueError:
            messagebox.showerror("Invalid EPSG", f"'{epsg_str}' is not a valid integer.")
            return

        try:
            elev = float(self.elev_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Invalid altitude", "Altitude must be a number.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save output as",
            initialfile="COORDS_000.txt",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not out_path:
            return

        self.status_var.set("Converting…")
        self.update_idletasks()

        def _run():
            try:
                e, n   = convert(lat, lon, epsg)
                zone   = get_zone(epsg)
                line1  = f"{epsg} {e:.2f} m E  {n:.2f} m N  {elev:.2f} m RL"
                content = "\n".join([
                    line1,
                    f"Zone      : {zone}",
                    f"EPSG      : {epsg}",
                    f"Lat       : {lat:.8f} deg",
                    f"Lon       : {lon:.8f} deg",
                    f"Alt       : {elev:.2f} m",
                ]) + "\n"
                with open(out_path, "w") as f:
                    f.write(content)
                self.after(0, lambda: self._on_success(
                    out_path, e, n, epsg, zone, lat, lon, elev))
            except Exception as ex:
                self.after(0, lambda: self._on_error(str(ex)))

        threading.Thread(target=_run, daemon=True).start()

    def _on_success(self, path, e, n, epsg, zone, lat, lon, elev, *_):
        self.status_var.set(
            f"✔  Saved → {os.path.basename(path)}  |  E {e:.2f}  N {n:.2f}  ({zone})")
        messagebox.showinfo("Conversion complete", (
            f"Output saved:\n{path}\n\n"
            f"{'─'*48}\n"
            f"{epsg} {e:.2f} m E  {n:.2f} m N  {elev:.2f} m RL\n"
            f"Zone      : {zone}\n"
            f"EPSG      : {epsg}\n"
            f"Lat       : {lat:.8f} deg\n"
            f"Lon       : {lon:.8f} deg\n"
            f"Alt       : {elev:.2f} m\n"
            f"{'─'*48}"
        ))

    def _on_error(self, msg):
        self.status_var.set(f"✖  Error: {msg}")
        messagebox.showerror("Conversion error",
                             f"Could not convert coordinates:\n\n{msg}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        from tkinterdnd2 import TkinterDnD
        App.__bases__ = (TkinterDnD.Tk,)
    except ImportError:
        pass
    App().mainloop()