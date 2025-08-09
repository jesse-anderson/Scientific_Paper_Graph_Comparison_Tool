#!/usr/bin/env python3
"""
# SPDX-License-Identifier: GPL-3.0-or-later

# Copyright (C) 2025 Jesse Anderson
#
# This file is part of Scientific Paper Graph Comparison Tool.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


Scientific annotator and quick quantifier for figures and charts.

Purpose
    Interactively place four reference points on a frozen screenshot or loaded
    image so you can quantify heights relative to a baseline and compare groups.

Core features
    * Progressive overlay in the upper right. Values appear as soon as enough
      points are set. Missing values are shown as "NA".
    * Horizontal dotted guides at each chosen y coordinate.
    * Color legend: Baseline=gray, Control=green, A=red, B=blue.
    * Region of Interest (ROI) crop on a frozen multi-monitor capture or an
      opened image.
    * "Save Annotated" grabs the exact canvas region from the screen so the
      saved PNG matches what you see, with no extra padding.
    * Optional axis calibration: click your 100% (or any tick) and enter its value.
      Then the overlay shows Baseline-normalized percentages for Control/A/B.
      If no axis tick is set, the tool safely auto-scales to avoid division by zero
      using the largest available height (documented below).

Typical workflow
    1) Click "Capture All Monitors" to freeze the full desktop, or "Open Image".
    2) Optionally "Select ROI" and drag a rectangle to crop to the area you care about.
    3) Click "Set Baseline" and click the chart's baseline (usually zero).
    4) (Optional) Click "Set Axis 100%" to place the 100% line and enter its value (default 100).
    5) Click "Set Control", "Set Marker A", and "Set Marker B" at the tops of bars/markers.
    6) Read the overlay for pixel heights, percent differences, and Baseline-normalized percentages.
    7) "Save Annotated" to export a PNG of the current canvas view.

Notes
    * On macOS you may need to grant screen recording permissions to Python for
      both mss and PIL.ImageGrab to capture the screen. I haven't really tested 
      it there since I don't own a mac. Feel free to yell at me on github.
    * Percent calculations are only defined when the relevant heights exist and
      denominators are nonzero. Otherwise the display shows "NA".
    * Baseline-normalized percentages require a scale (height of Baseline->AxisTick).
      If that span is missing or zero, the app falls back to the largest available
      height among Control/A/B (or 1 px) to avoid division by zero; the overlay
      marks this as "auto" so you know the scale came from data rather than a tick.
"""

import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
except Exception as e:
    print("Error importing tkinter. Please ensure Tkinter is installed.", file=sys.stderr)
    raise

try:
    # PIL imports used throughout the app
    from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageGrab
except Exception as e:
    print("This program requires Pillow. Install with: pip install pillow", file=sys.stderr)
    raise

# Detect whether mss is available for screen capture.
# mss is preferred over PIL.ImageGrab for cross-platform reliability.
MSS_AVAILABLE = True
try:
    import mss
except Exception:
    MSS_AVAILABLE = False

# Guide and label colors for each point type.
COLORS = {
    "Baseline": "#A0A0A0",  # gray
    "Control":  "#00C853",  # green
    "A":        "#FF5252",  # red
    "B":        "#2979FF",  # blue
    "Axis":     "#FFD600",  # gold (axis calibration reference, e.g., 100%)
}


@dataclass
class Point:
    """
    Storage for a user-selected point and its associated canvas items.

    Attributes
    ----------
    name : str
        Logical name of the point ("Baseline", "Control", "A", "B", "Axis").
    xy : Optional[Tuple[int, int]]
        Image-space coordinates (x, y). These are in the coordinate system of
        the current working image (full capture or ROI crop), not canvas pixels.
    line_id : Optional[int]
        Canvas item id for the horizontal dotted line drawn through this point.
    dot_id : Optional[int]
        Canvas item id for the small circle placed at the clicked coordinate.
    label_id : Optional[int]
        Canvas item id for the text label next to the dot.
    """
    name: str
    xy: Optional[Tuple[int, int]] = None
    line_id: Optional[int] = None
    dot_id: Optional[int] = None
    label_id: Optional[int] = None


class AnnotatorApp:
    """
    Main Tkinter application.

    Responsibilities
    ----------------
    * Capture a full multi-monitor screenshot or open an image.
    * Allow ROI selection to crop the working image.
    * Manage point placement for Baseline, Control, A, and B.
    * Render horizontal guides, labels, and a progressive numeric overlay.
    * Export the exact canvas view as a PNG.

    Coordinate systems
    ------------------
    * Image space: pixels of the working PIL image (full capture or ROI).
    * Canvas space: on-screen display pixels of the Tkinter canvas.
      The app keeps track of a scale factor so clicks map to image coordinates.
    """

    def __init__(self, master: tk.Tk):
        """Build UI, initialize state, and register event handlers."""
        self.master = master
        master.title("Baseline + Control + A + B Annotator (Baseline-Normalized %)")

        # Working images and canvas scaling
        self.full_capture: Optional[Image.Image] = None   # last frozen full desktop
        self.image: Optional[Image.Image] = None          # current working image (full or ROI)
        self.display_image: Optional[Image.Image] = None  # scaled image shown on canvas
        self.photo: Optional[ImageTk.PhotoImage] = None   # Tkinter wrapper for display_image
        self.display_scale: Tuple[float, float] = (1.0, 1.0)

        # Point registry (+ optional axis tick)
        self.points: Dict[str, Point] = {
            "Baseline": Point("Baseline"),
            "Control": Point("Control"),
            "A": Point("A"),
            "B": Point("B"),
            "Axis": Point("Axis"),  # optional: a known Y tick (e.g., 100%)
        }
        self.axis_value: float = 100.0  # numeric value for Axis tick (default 100)

        self.current_mode: Optional[str] = None  # which point we are currently setting
        self.overlay_items = []                  # canvas item ids for the overlay panel

        # ROI selection state
        self.roi_start: Optional[Tuple[int, int]] = None
        self.roi_rect_canvas_id: Optional[int] = None

        # Toolbar
        tb = tk.Frame(master)
        tb.pack(side=tk.TOP, fill=tk.X)

        self.btn_capture_all = tk.Button(tb, text="Capture All Monitors", command=self.capture_all_monitors)
        self.btn_open = tk.Button(tb, text="Open Image", command=self.open_image)
        self.btn_roi = tk.Button(tb, text="Select ROI", command=self.start_roi_mode)
        self.btn_reset_roi = tk.Button(tb, text="Reset to Full Capture", command=self.reset_to_full_capture)

        self.btn_set_baseline = tk.Button(tb, text="Set Baseline", command=lambda: self.set_mode("Baseline"))
        self.btn_set_control = tk.Button(tb, text="Set Control", command=lambda: self.set_mode("Control"))
        self.btn_set_a = tk.Button(tb, text="Set Marker A", command=lambda: self.set_mode("A"))
        self.btn_set_b = tk.Button(tb, text="Set Marker B", command=lambda: self.set_mode("B"))
        self.btn_set_axis = tk.Button(tb, text="Set Axis 100%", command=self.set_axis_tick)

        self.btn_clear = tk.Button(tb, text="Clear Marks", command=self.clear_marks)
        self.btn_save = tk.Button(tb, text="Save Annotated", command=self.save_annotated)
        self.btn_info = tk.Button(tb, text="Monitors Info", command=self.show_monitors_info)

        for w in [self.btn_capture_all, self.btn_open, self.btn_roi, self.btn_reset_roi,
                  self.btn_set_baseline, self.btn_set_control, self.btn_set_a, self.btn_set_b, self.btn_set_axis,
                  self.btn_clear, self.btn_save, self.btn_info]:
            w.pack(side=tk.LEFT, padx=4, pady=4)

        # Status line at the bottom for user guidance
        self.status = tk.StringVar(value="Capture or open image. ROI optional. Then set Baseline, Axis 100% (optional), Control, A, B.")
        tk.Label(master, textvariable=self.status, anchor="w").pack(side=tk.BOTTOM, fill=tk.X)

        # Canvas for displaying the working image and all overlays
        self.canvas = tk.Canvas(master, background="#202020", cursor="crosshair")
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Event bindings. add="+" ensures handlers do not overwrite each other. Previous error with handlers, watch out for add = "+" !!!
        self.canvas.bind("<Button-1>", self.on_canvas_click, add="+")
        self.canvas.bind("<Configure>", self.on_resize, add="+")
        self.canvas.bind("<ButtonPress-1>", self.on_roi_press, add="+")
        self.canvas.bind("<B1-Motion>", self.on_roi_drag, add="+")
        self.canvas.bind("<ButtonRelease-1>", self.on_roi_release, add="+")

        # Keyboard shortcuts for faster workflow.
        # I want to be able to eventually just speedrun using this for scientific viz quantification esp if I'm writing more.
        master.bind("r", lambda e: self.start_roi_mode())
        master.bind("d", lambda e: self.reset_to_full_capture())
        master.bind("x", lambda e: self.clear_marks())
        master.bind("z", lambda e: self.set_mode("Baseline"))
        master.bind("c", lambda e: self.set_mode("Control"))
        master.bind("a", lambda e: self.set_mode("A"))
        master.bind("b", lambda e: self.set_mode("B"))
        master.bind("t", lambda e: self.set_axis_tick())
        master.bind("<Escape>", lambda e: self.set_mode(None))

    # ********* Capture / Open **********
    def show_monitors_info(self) -> None:
        """Display monitor geometry detected by mss for troubleshooting."""
        if not MSS_AVAILABLE:
            messagebox.showinfo("Monitors", "mss not available. pip install mss")
            return
        try:
            with mss.mss() as sct:
                mons = sct.monitors
                lines = [f"Detected {len(mons)-1} monitor(s)."]
                for idx, m in enumerate(mons):
                    lines.append(f"Index {idx}: left={m['left']}, top={m['top']}, width={m['width']}, height={m['height']}")
                messagebox.showinfo("Monitors", "\n".join(lines))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to query monitors: {e}")

    def capture_all_monitors(self) -> None:
        """
        Freeze the current desktop across all monitors and set it as the working image.

        Notes
        -----
        mss.monitors[0] returns a bounding box that covers all attached
        displays, which lets us handle multi-monitor setups in a single image.
        """
        if not MSS_AVAILABLE:
            messagebox.showerror("mss not available", "Install with: pip install mss")
            return
        try:
            with mss.mss() as sct:
                bbox = sct.monitors[0]
                shot = sct.grab(bbox)
        except Exception as e:
            messagebox.showerror("Error", f"Capture failed: {e}")
            return
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        self.full_capture = img
        self.set_image(img)
        self.status.set(f"Captured all monitors: {img.size[0]} x {img.size[1]}. Select ROI or set points.")

    def open_image(self) -> None:
        """Open an image from disk and make it the working image."""
        path = filedialog.askopenfilename(
            title="Open image",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open image: {e}")
            return
        self.full_capture = img.copy()
        self.set_image(img)
        self.status.set("Image loaded. Select ROI if needed, then set points.")

    # ******** Image / Canvas *********************
    def set_image(self, img: Image.Image) -> None:
        """
        Assign the working image and reset the canvas view.

        This also clears any existing marks because coordinates are with respect
        to the current image.
        """
        self.image = img
        self.reset_canvas_image()
        self.clear_marks()
        self.status.set("Image ready.")

    def reset_canvas_image(self) -> None:
        """
        Fit the working image to the canvas while preserving aspect ratio,
        then redraw all overlays at the new scale.
        """
        if self.image is None:
            return
        c_w = max(self.canvas.winfo_width(), 200)
        c_h = max(self.canvas.winfo_height(), 200)
        img_w, img_h = self.image.size
        scale = min(c_w / img_w, c_h / img_h)
        disp_w = max(1, int(img_w * scale))
        disp_h = max(1, int(img_h * scale))
        self.display_scale = (disp_w / img_w, disp_h / img_h)
        self.display_image = self.image.resize((disp_w, disp_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(self.display_image)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        # Redraw existing points at the new scale
        for name, pt in self.points.items():
            if pt.xy is not None:
                self.draw_point(name, pt.xy, redraw=True)

        self.update_metrics_overlay()

    def on_resize(self, event) -> None:
        """When the canvas resizes, recompute scaling and redraw."""
        if self.image is not None:
            self.reset_canvas_image()

    def canvas_to_image_xy(self, x: int, y: int) -> Tuple[int, int]:
        """
        Convert canvas coordinates to image coordinates, clamped to image bounds.
        """
        sx, sy = self.display_scale
        ix = int(x / sx) if sx else 0
        iy = int(y / sy) if sy else 0
        if self.image is not None:
            w, h = self.image.size
            ix = max(0, min(w - 1, ix))
            iy = max(0, min(h - 1, iy))
        return ix, iy

    def image_to_canvas_xy(self, x: int, y: int) -> Tuple[int, int]:
        """
        Convert image coordinates to canvas coordinates based on the current scale.
        """
        sx, sy = self.display_scale
        return int(x * sx), int(y * sy)

    # *********** ROI *****************
    def start_roi_mode(self) -> None:
        """
        Enter ROI mode. The user will drag to define a rectangle and release
        to crop the working image to that region.
        """
        if self.image is None:
            self.status.set("Capture or open an image first.")
            return
        self.current_mode = "ROI"
        self.status.set("ROI mode: drag to draw rectangle, release to crop. Esc to cancel.")
        if self.roi_rect_canvas_id is not None:
            try:
                self.canvas.delete(self.roi_rect_canvas_id)
            except Exception:
                pass
            self.roi_rect_canvas_id = None
        self.roi_start = None

    def on_roi_press(self, event) -> None:
        """Start point for ROI drag if we are in ROI mode."""
        if self.current_mode != "ROI" or self.image is None:
            return
        self.roi_start = (event.x, event.y)
        if self.roi_rect_canvas_id is not None:
            try:
                self.canvas.delete(self.roi_rect_canvas_id)
            except Exception:
                pass
            self.roi_rect_canvas_id = None

    def on_roi_drag(self, event) -> None:
        """Update the ROI rectangle while dragging."""
        if self.current_mode != "ROI" or self.image is None or self.roi_start is None:
            return
        x0, y0 = self.roi_start
        x1, y1 = event.x, event.y
        if self.roi_rect_canvas_id is None:
            self.roi_rect_canvas_id = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline="#00FF00", dash=(6, 4), width=2
            )
        else:
            self.canvas.coords(self.roi_rect_canvas_id, x0, y0, x1, y1)

    def on_roi_release(self, event) -> None:
        """Finalize ROI and crop the working image."""
        if self.current_mode != "ROI" or self.image is None or self.roi_start is None:
            return
        x0, y0 = self.roi_start
        x1, y1 = event.x, event.y
        self.current_mode = None

        # Normalize rectangle and enforce a minimum size
        x0, x1 = sorted([x0, x1])
        y0, y1 = sorted([y0, y1])
        if abs(x1 - x0) < 4 or abs(y1 - y0) < 4:
            self.status.set("ROI too small; cancelled.")
            if self.roi_rect_canvas_id is not None:
                self.canvas.delete(self.roi_rect_canvas_id)
                self.roi_rect_canvas_id = None
            self.roi_start = None
            return

        # Map to image space and clamp to bounds
        ix0, iy0 = self.canvas_to_image_xy(x0, y0)
        ix1, iy1 = self.canvas_to_image_xy(x1, y1)
        w, h = self.image.size
        ix0 = max(0, min(w - 1, ix0))
        iy0 = max(0, min(h - 1, iy0))
        ix1 = max(0, min(w, ix1))
        iy1 = max(0, min(h, iy1))
        if ix1 <= ix0 or iy1 <= iy0:
            self.status.set("ROI invalid; cancelled.")
            if self.roi_rect_canvas_id is not None:
                self.canvas.delete(self.roi_rect_canvas_id)
                self.roi_rect_canvas_id = None
            self.roi_start = None
            return

        # Crop, clear marks(coordinates are no longer valid!!!!!!!!!!), and redraw
        self.image = self.image.crop((ix0, iy0, ix1, iy1))
        self.clear_marks()
        self.reset_canvas_image()
        if self.roi_rect_canvas_id is not None:
            try:
                self.canvas.delete(self.roi_rect_canvas_id)
            except Exception:
                pass
            self.roi_rect_canvas_id = None
        self.roi_start = None
        self.status.set("ROI selected. Set Baseline, Axis 100%, Control, A, B.")

    def reset_to_full_capture(self) -> None:
        """Restore the last full multi-monitor capture as the working image."""
        if self.full_capture is None:
            self.status.set("No full capture available.")
            return
        self.set_image(self.full_capture.copy())
        self.status.set("Reset to full capture.")

    # ******** Points *********
    def set_mode(self, mode: Optional[str]) -> None:
        """Enter a point-setting mode or cancel if mode is None."""
        self.current_mode = mode
        if mode in ("Baseline", "Control", "A", "B", "Axis"):
            self.status.set(f"Click to set {mode}.")
        elif mode is None:
            self.status.set("Selection cancelled.")

    def set_axis_tick(self) -> None:
        """Prompt for a tick value (default 100) and enter click mode to place it."""
        try:
            val = simpledialog.askfloat("Axis calibration", "Value for this Y-axis tick:", initialvalue=self.axis_value,
                                        minvalue=-1e9, maxvalue=1e9, parent=self.master)
        except Exception:
            val = self.axis_value
        if val is not None:
            self.axis_value = float(val)
            self.set_mode("Axis")
            self.status.set(f"Click to set Axis tick (value {self.axis_value:g}).")

    def on_canvas_click(self, event) -> None:
        """Handle a click to set the current point."""
        if self.image is None or self.current_mode not in ("Baseline", "Control", "A", "B", "Axis"):
            return
        img_xy = self.canvas_to_image_xy(event.x, event.y)
        self.points[self.current_mode].xy = img_xy
        self.draw_point(self.current_mode, img_xy)
        label = self.current_mode if self.current_mode != "Axis" else f"Axis {self.axis_value:g}"
        self.status.set(f"{label} set at {img_xy}.")
        self.current_mode = None
        self.update_metrics_overlay()

    def _color_for(self, name: str) -> str:
        """Resolve the configured color for a point name."""
        return COLORS.get(name, "#00FFFF")

    def draw_point(self, name: str, img_xy: Tuple[int, int], redraw: bool = False) -> None:
        """
        Draw or redraw the guide line, dot, and label for a point.
        """
        if self.image is None:
            return
        pt = self.points[name]

        # Remove prior canvas items for this point (if any)
        for attr in ("line_id", "dot_id", "label_id"):
            item = getattr(pt, attr)
            if item is not None:
                try:
                    self.canvas.delete(item)
                except Exception:
                    pass
                setattr(pt, attr, None)

        can_x, can_y = self.image_to_canvas_xy(*img_xy)
        width = self.display_image.size[0] if self.display_image is not None else self.canvas.winfo_width()
        color = self._color_for(name)

        # Horizontal dotted guide across the full canvas width
        pt.line_id = self.canvas.create_line(0, can_y, width, can_y, dash=(6, 4), width=2, fill=color)

        # Small marker dot at the exact click location and a short label
        r = 4
        pt.dot_id = self.canvas.create_oval(can_x - r, can_y - r, can_x + r, can_y + r, outline=color, width=2)

        label_text = f"{name} ({img_xy[0]}, {img_xy[1]})"
        if name == "Axis":
            label_text = f"Axis {self.axis_value:g} ({img_xy[0]}, {img_xy[1]})"
        pt.label_id = self.canvas.create_text(
            can_x + 8, can_y - 10, text=label_text,
            anchor="w", fill=color, font=("TkDefaultFont", 10, "bold")
        )

        # Ensure overlay stays visible above lines/labels
        for oid in self.overlay_items:
            self.canvas.tag_raise(oid)

    # ************ Metrics ********************
    def clear_marks(self) -> None:
        """Remove all point graphics and forget their coordinates."""
        for pt in self.points.values():
            for attr in ("line_id", "dot_id", "label_id"):
                item = getattr(pt, attr)
                if item is not None:
                    try:
                        self.canvas.delete(item)
                    except Exception:
                        pass
                    setattr(pt, attr, None)
            pt.xy = None
        self.clear_overlay_text()

    def clear_overlay_text(self) -> None:
        """Erase the overlay panel and its text items."""
        for item in self.overlay_items:
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self.overlay_items = []

    def compute_metrics(self) -> Dict[str, Optional[float]]:
        """
        Compute heights and comparisons. Also compute Baseline-normalized
        percentages for Control/A/B using either:
          - the Baseline->Axis span (preferred), or
          - an auto scale = max(height of Control/A/B) when Axis is missing or zero.
        The overlay indicates if auto scaling was used.
        """
        baseline = self.points["Baseline"].xy
        control = self.points["Control"].xy
        A = self.points["A"].xy
        B = self.points["B"].xy
        axis_pt = self.points["Axis"].xy

        base_y = baseline[1] if baseline is not None else None
        axis_y = axis_pt[1] if axis_pt is not None else None

        def height(y_top: int) -> Optional[float]:
            if base_y is None:
                return None
            return float(base_y - y_top)

        # Heights will be None until both Baseline and the specific point exist
        hA = height(A[1]) if A is not None else None
        hB = height(B[1]) if B is not None else None
        hC = height(control[1]) if control is not None else None

        # Axis span in pixels (Baseline -> Axis). Could be zero if placed at same y.
        hAxis = None
        if base_y is not None and axis_y is not None:
            try:
                span = float(base_y - axis_y)
                hAxis = span if span != 0 else None
            except Exception:
                hAxis = None

        # Auto scale fallback (avoids division by zero; "scale both baseline and markers").
        auto_used = False
        if hAxis is None:
            # choose the largest available height as the scale; if none exist, use 1.0
            candidates = [v for v in (hA, hB, hC) if v is not None]
            if candidates:
                hAxis = max(abs(v) for v in candidates)
                auto_used = True
            else:
                hAxis = 1.0
                auto_used = True

        def pct(n, d):
            """Safe percent helper that returns None if inputs are missing or invalid."""
            try:
                if n is None or d is None or d == 0:
                    return None
                return 100.0 * n / d
            except Exception:
                return None

        # Baseline-normalized percentages using the chosen scale
        def base_norm(v_height: Optional[float]) -> Optional[float]:
            if v_height is None or hAxis is None:
                return None
            return (v_height / hAxis) * float(self.axis_value)

        m: Dict[str, Optional[float]] = {
            # Heights
            "height_Baseline_y": float(base_y) if base_y is not None else None,
            "height_Control_px": hC,
            "height_A_px": hA,
            "height_B_px": hB,
            "height_Axis_px": hAxis,
            "axis_auto_used": auto_used,

            # A <-> B
            "A_as_pct_of_B": pct(hA, hB),
            "B_as_pct_of_A": pct(hB, hA),
            "delta_A_vs_B_pct": pct(None if hA is None or hB is None else hA - hB, hB),
            "delta_B_vs_A_pct": pct(None if hA is None or hB is None else hB - hA, hA),

            # A <-> Control
            "A_as_pct_of_Control": pct(hA, hC),
            "Control_as_pct_of_A": pct(hC, hA),
            "delta_A_vs_Control_pct": pct(None if hA is None or hC is None else hA - hC, hC),
            "delta_Control_vs_A_pct": pct(None if hA is None or hC is None else hC - hA, hA),

            # B <-> Control
            "B_as_pct_of_Control": pct(hB, hC),
            "Control_as_pct_of_B": pct(hC, hB),
            "delta_B_vs_Control_pct": pct(None if hB is None or hC is None else hB - hC, hC),
            "delta_Control_vs_B_pct": pct(None if hB is None or hC is None else hC - hB, hB),

            # Baseline-normalized (calibrated) values
            "BaseNorm_Control": base_norm(hC),
            "BaseNorm_A": base_norm(hA),
            "BaseNorm_B": base_norm(hB),
            "Axis_Value": float(self.axis_value) if self.axis_value is not None else None,
        }
        return m

    def update_metrics_overlay(self) -> None:
        """
        Draw the overlay panel in the upper right. It always appears, and
        fills progressively with "NA" for values that are not yet available.
        """
        self.clear_overlay_text()
        m = self.compute_metrics()

        def fmt(v):
            return "NA" if v is None else f"{v:.2f}"

        axis_note = " (auto)" if m.get("axis_auto_used") else ""

        lines = [
            "Heights (pixels) relative to Baseline:",
            f"  Control height: {fmt(m['height_Control_px'])} px",
            f"  A height: {fmt(m['height_A_px'])} px",
            f"  B height: {fmt(m['height_B_px'])} px",
            f"  Axis span (Baseline->Tick){axis_note}: {fmt(m['height_Axis_px'])} px",
            "Baseline-normalized percentages:",
            f"  Control vs Baseline: {fmt(m['BaseNorm_Control'])}",
            f"  A vs Baseline: {fmt(m['BaseNorm_A'])}",
            f"  B vs Baseline: {fmt(m['BaseNorm_B'])}",
            f"  Axis tick value: {fmt(m['Axis_Value'])}",
            "A <-> B comparisons:",
            f"  A as percent of B: {fmt(m['A_as_pct_of_B'])} %",
            f"  B as percent of A: {fmt(m['B_as_pct_of_A'])} %",
            f"  A vs B: {fmt(m['delta_A_vs_B_pct'])} %   (positive means A is higher than B)",
            f"  B vs A: {fmt(m['delta_B_vs_A_pct'])} %   (positive means B is higher than A)",
            "A <-> Control comparisons:",
            f"  A as percent of Control: {fmt(m['A_as_pct_of_Control'])} %",
            f"  Control as percent of A: {fmt(m['Control_as_pct_of_A'])} %",
            f"  A vs Control: {fmt(m['delta_A_vs_Control_pct'])} %",
            f"  Control vs A: {fmt(m['delta_Control_vs_A_pct'])} %",
            "B <-> Control comparisons:",
            f"  B as percent of Control: {fmt(m['B_as_pct_of_Control'])} %",
            f"  Control as percent of B: {fmt(m['Control_as_pct_of_B'])} %",
            f"  B vs Control: {fmt(m['delta_B_vs_Control_pct'])} %",
            f"  Control vs B: {fmt(m['delta_Control_vs_B_pct'])} %",
        ]
        text = "\n".join(lines)

        pad = 8
        w = self.canvas.winfo_width()
        x_text, y_text = w - 10, 10  # anchor to upper right
        # Create text first so we can size the background rectangle to fit
        t = self.canvas.create_text(x_text, y_text, anchor="ne", text=text, fill="#EAF2F8", font=("TkDefaultFont", 10))
        bbox = self.canvas.bbox(t)
        if bbox:
            x1, y1, x2, y2 = bbox
            rect = self.canvas.create_rectangle(x1 - pad, y1 - pad, x2 + pad, y2 + pad,
                                                fill="#111111", outline="#666666")
            self.canvas.tag_lower(rect, t)
            self.overlay_items.append(rect)
        self.overlay_items.append(t)

        # Keep overlay above all point graphics
        for oid in self.overlay_items:
            self.canvas.tag_raise(oid)

        # Title bar summary with any available baseline-normalized values
        title_bits = []
        for key, label in (("BaseNorm_A", "A"), ("BaseNorm_B", "B"), ("BaseNorm_Control", "Control")):
            v = m.get(key)
            if v is not None:
                title_bits.append(f"{label} {v:.1f}")
        self.master.title(" | ".join(title_bits) if title_bits else "Baseline + Control + A + B Annotator")

    # **** Save (capture current canvas) ********
    def save_annotated(self) -> None:
        """
        Capture the canvas area from the screen as it currently appears
        and save it to a PNG. This preserves the exact layout with no gaps.
        """
        try:
            x0 = self.canvas.winfo_rootx()
            y0 = self.canvas.winfo_rooty()
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()

            # Prefer mss for cross-platform capture
            if MSS_AVAILABLE:
                with mss.mss() as sct:
                    shot = sct.grab({"left": x0, "top": y0, "width": w, "height": h})
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
            else:
                # Fallback to PIL.ImageGrab on platforms where it is allowed
                bbox = (x0, y0, x0 + w, y0 + h)
                img = ImageGrab.grab(bbox=bbox).convert("RGB")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to capture canvas: {e}")
            return

        path = filedialog.asksaveasfilename(
            title="Save annotated view",
            defaultextension=".png",
            initialfile=f"annotated_view_{int(time.time())}.png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img.save(path)
            messagebox.showinfo("Saved", f"Annotated view saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save image: {e}")


#Dupes from previous iterations
# from PIL import Image, ImageTk, ImageDraw, ImageFont 


def main() -> None:
    """Create the Tk root window and enter the event loop."""
    root = tk.Tk()
    root.geometry("1460x940")
    app = AnnotatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
