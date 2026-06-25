"""
Auto-cutline generation from PNG/JPG raster images.

Optional module: requires Pillow, numpy, and scikit-image.
Shapely is used for margin/buffer if installed (recommended: pip install shapely).
Without shapely, margin is approximated via centroid-scaling (fine for convex shapes).

Reuses simplify_polyline, prune_straight_segments, and points_to_plt from svg_to_plt.py.

Pipeline per image:
  load → RGBA (synthesize alpha for JPEGs/white-bg) → trace alpha contours →
  margin buffer → simplify → canvas placement

Pipeline for layout:
  load all → shelf-pack onto 4x7" canvas → composite JPEG →
  combined cut SVG + PLT (repeat per batch if --paginate)

Outputs per batch:
  layout.jpg       — composite sticker sheet  (use as --jpg when sending)
  layout.plt       — cut paths in PLT format  (use as --plt when sending)
  layout_cut.svg   — human-readable SVG preview of cut paths
"""
from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .svg_to_plt import (
    DEFAULT_KP,
    DEFAULT_SIMPLIFY,
    DEFAULT_UNITS_PER_INCH,
    MM_PER_INCH,
    STRAIGHT_DIST_EPS,
    _plt_path_commands,
    points_to_plt,
    prune_straight_segments,
    simplify_polyline,
)

# Type aliases
Point = Tuple[float, float]
Contour = List[Point]

# Physical canvas for PixCut media
CANVAS_W_IN: float = 4.0
CANVAS_H_IN: float = 7.0

# Defaults (also referenced in cli.py argparse defaults — keep in sync)
DEFAULT_LAYOUT_DPI: int = 300
DEFAULT_MARGIN_MM: float = 2.0
DEFAULT_PADDING_MM: float = 3.0
DEFAULT_MIN_AREA_MM2: float = 4.0    # ignore artifacts smaller than this


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def _check_deps() -> None:
    missing = []
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import skimage.measure  # noqa: F401
    except ImportError:
        missing.append("scikit-image")
    if missing:
        raise ImportError(
            f"The 'layout' command requires additional packages: {', '.join(missing)}\n"
            f"Install with:  pip install {' '.join(missing)}\n"
            f"Shapely is optional but recommended for accurate margin offsets:\n"
            f"  pip install shapely"
        )


# ---------------------------------------------------------------------------
# Image loading: always produce RGBA with meaningful alpha
# ---------------------------------------------------------------------------

def _load_rgba(path: Path, bg_white: bool) -> "PIL.Image.Image":
    """
    Load an image as RGBA.  For images without alpha (JPEG, palette) or when
    bg_white=True, synthesise an alpha channel by treating near-white pixels as
    background (transparent).

    Always autocrop transparent margins so the layout engine packs against the
    true content boundary rather than the full image canvas.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(path)

    if img.mode == "RGBA" and not bg_white:
        return _autocrop_alpha(img)

    rgb = img.convert("RGB")
    arr = np.array(rgb, dtype=np.int32)
    near_white = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
    rgba = rgb.convert("RGBA")
    alpha = Image.fromarray(((~near_white).astype("uint8")) * 255, mode="L")
    rgba.putalpha(alpha)
    return _autocrop_alpha(rgba)


def _autocrop_alpha(img: "PIL.Image.Image") -> "PIL.Image.Image":
    """Crop transparent border pixels, returning the tight content bounding box."""
    alpha = img.split()[3]
    bbox = alpha.getbbox()
    if bbox:
        return img.crop(bbox)
    return img


# ---------------------------------------------------------------------------
# Mask extraction (from RGBA alpha channel)
# ---------------------------------------------------------------------------

def _alpha_mask(img: "PIL.Image.Image", threshold: int = 10) -> "numpy.ndarray":
    """Return boolean mask (True = sticker content) from image alpha channel."""
    import numpy as np
    return np.array(img.split()[3]) > threshold


# ---------------------------------------------------------------------------
# Contour tracing
# ---------------------------------------------------------------------------

def _polygon_area(pts: List[Point]) -> float:
    """Shoelace formula."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    return area / 2.0


def _trace_contours(mask: "numpy.ndarray", min_area_px: float) -> List[Contour]:
    """
    Trace outer contours of a boolean mask using marching squares.
    Returns contours as lists of (x, y) pixel coordinates.
    """
    import numpy as np
    from skimage.measure import find_contours

    # Pad by 1 so contours touching the image edge are properly closed.
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    raw = find_contours(padded.astype(float), level=0.5)

    result: List[Contour] = []
    for contour in raw:
        # skimage returns (row=y, col=x); undo the 1-px pad
        pts: Contour = [(float(c[1]) - 1.0, float(c[0]) - 1.0) for c in contour]
        if len(pts) < 3:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        if abs(_polygon_area(pts)) < min_area_px:
            continue
        result.append(pts)
    return result


# ---------------------------------------------------------------------------
# Margin / buffer
# ---------------------------------------------------------------------------

def _apply_margin(contours: List[Contour], margin_px: float) -> List[Contour]:
    """
    Expand each contour outward by margin_px pixels.
    Uses shapely.buffer if available (accurate for all shapes).
    Falls back to centroid-scale (fine for convex/near-convex stickers).
    """
    if margin_px <= 0.0:
        return contours

    try:
        from shapely.geometry import Polygon
        result: List[Contour] = []
        for pts in contours:
            try:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)                # fix self-intersections
                expanded = poly.buffer(margin_px, join_style=2)  # mitre corners
                if expanded.is_empty:
                    continue                              # negative margin collapsed the shape
                coords = list(expanded.exterior.coords)
                result.append([(float(x), float(y)) for x, y in coords])
            except Exception:
                result.append(pts)
        return result

    except ImportError:
        # Centroid-scale fallback (works well for convex/near-convex shapes)
        result = []
        for pts in contours:
            if len(pts) < 3:
                result.append(pts)
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            avg_r = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts)
            if avg_r < 1.0:
                result.append(pts)
                continue
            factor = (avg_r + margin_px) / avg_r
            if factor <= 0:
                continue                                  # negative margin collapsed the shape
            result.append(
                [(cx + (p[0] - cx) * factor, cy + (p[1] - cy) * factor) for p in pts]
            )
        return result


# ---------------------------------------------------------------------------
# Packing layout
# ---------------------------------------------------------------------------

@dataclass
class _Item:
    path: Path
    img: "PIL.Image.Image"      # RGBA, already scaled to fit canvas if needed
    contours: List[Contour]     # kiss-cut contours in image-local pixel coords
    w: int
    h: int
    margin_offset: int = 0      # px to offset image placement inside the packer's effective footprint
    effective_w: int = 0        # w + 2*margin_offset — footprint used by packer
    effective_h: int = 0        # h + 2*margin_offset
    perf_contours: List[Contour] = field(default_factory=list)  # perf-cut contours (empty when disabled)


@dataclass
class PlacedItem:
    source_path: Path
    img: "PIL.Image.Image"
    contours: List[Contour]     # kiss-cut contours in canvas pixel coords
    x: int
    y: int
    w: int
    h: int
    perf_contours: List[Contour] = field(default_factory=list)  # perf-cut contours in canvas pixel coords


# Free-rectangle type alias: (x, y, w, h)
_Rect = Tuple[int, int, int, int]


def _rect_intersects(ax: int, ay: int, aw: int, ah: int,
                     bx: int, by: int, bw: int, bh: int) -> bool:
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _rect_contains(outer: _Rect, inner: _Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _pack(
    items: List[_Item],
    canvas_w: int,
    canvas_h: int,
    padding: int,
    left_margin_px: int = 0,
) -> Tuple[List[Tuple[int, int, int]], List[int]]:
    """
    Maximal Rectangles packing with Best Short Side Fit (BSSF) heuristic.

    Maintains a list of free rectangles.  After placing each item the
    intersected free rects are split and dominated rects pruned.  This allows
    small items to fill gaps beside tall items — far better space utilisation
    than a row-based shelf packer.

    Items should be pre-sorted largest-area-first for best results.

    Returns:
      placed  : [(item_idx, x, y), ...]  where (x, y) is top-left of effective footprint
      overflow: [item_idx, ...]          items that did not fit
    """
    # Initial free rect inset by padding on all edges, plus an extra left margin.
    left = padding + left_margin_px
    free: List[_Rect] = [
        (left, padding, canvas_w - left - padding, canvas_h - 2 * padding)
    ]
    placed: List[Tuple[int, int, int]] = []
    overflow: List[int] = []

    for idx, item in enumerate(items):
        iw, ih = item.effective_w, item.effective_h

        # Find best-fit free rect (BSSF: minimise the shorter leftover dimension).
        best_score: Optional[int] = None
        best_pos: Optional[Tuple[int, int]] = None

        for rx, ry, rw, rh in free:
            if rw >= iw and rh >= ih:
                score = min(rw - iw, rh - ih)
                if best_score is None or score < best_score:
                    best_score = score
                    best_pos = (rx, ry)

        if best_pos is None:
            overflow.append(idx)
            continue

        px, py = best_pos
        placed.append((idx, px, py))

        # The occupied zone includes a padding gap so items don't crowd each other.
        occ_w = iw + padding
        occ_h = ih + padding

        # Split every free rect that overlaps the occupied zone.
        new_free: List[_Rect] = []
        for rx, ry, rw, rh in free:
            if not _rect_intersects(px, py, occ_w, occ_h, rx, ry, rw, rh):
                new_free.append((rx, ry, rw, rh))
                continue
            # Left strip
            if px > rx:
                new_free.append((rx, ry, px - rx, rh))
            # Right strip
            right_x = px + occ_w
            if rx + rw > right_x:
                new_free.append((right_x, ry, rx + rw - right_x, rh))
            # Top strip
            if py > ry:
                new_free.append((rx, ry, rw, py - ry))
            # Bottom strip
            bottom_y = py + occ_h
            if ry + rh > bottom_y:
                new_free.append((rx, bottom_y, rw, ry + rh - bottom_y))

        # Prune rects that are fully contained within another (keeps list small).
        pruned: List[_Rect] = []
        for i, r in enumerate(new_free):
            if not any(_rect_contains(other, r) for j, other in enumerate(new_free) if j != i):
                pruned.append(r)
        free = pruned

    return placed, overflow


def _center_shift(
    placements: List[Tuple[int, int, int]],
    items: List["_Item"],
    canvas_w: int,
    padding: int,
) -> int:
    """Return the X pixel offset needed to center the packed content horizontally.

    Computes the right edge of the bounding box of all placed items, then
    shifts the group so it sits in the middle of the canvas (ignoring any
    left_margin_px that was baked into the packer's initial free rect).
    """
    if not placements:
        return 0
    right_edge = max(px + items[idx].effective_w for idx, px, _py in placements)
    content_w = right_edge - padding  # strip the leading padding from the left
    shift = (canvas_w - content_w) // 2 - padding
    return max(0, shift)


# ---------------------------------------------------------------------------
# Coordinate transform: canvas pixels → PLT units
# ---------------------------------------------------------------------------

def _canvas_to_plt_coords(
    contours: List[Contour],
    canvas_w_px: int,
    canvas_h_px: int,
) -> List[Contour]:
    """
    Convert canvas pixel contours to PLT coordinate units applying the standard
    -90° rotation used by svg_to_plt.py.

    Canvas space:  (0,0) top-left, x→right (4" wide), y→down (7" tall)
    PLT space:     x ∈ [0, 7112], y ∈ [0, 4064]  (after -90° rotation)
      PLT_x = CANVAS_H_IN * UNITS - canvas_y * scale
      PLT_y = canvas_x * scale
      scale = UNITS_PER_INCH / dpi  =  (CANVAS_W_IN * UNITS) / canvas_w_px
    """
    units = DEFAULT_UNITS_PER_INCH
    scale = (CANVAS_W_IN * units) / canvas_w_px   # same for x and y (square pixels)
    cur_h_plt = CANVAS_H_IN * units               # 7112 at 1016 u/in

    result: List[Contour] = []
    for pts in contours:
        result.append([(cur_h_plt - py * scale, px * scale) for px, py in pts])
    return result


# ---------------------------------------------------------------------------
# SVG output
# ---------------------------------------------------------------------------

def _contours_to_svg(
    contours: List[Contour],
    canvas_w_px: int,
    canvas_h_px: int,
    dpi: int,
    perf_contours: Optional[List[Contour]] = None,
    perf_dash_mm: float = 1.5,
    perf_gap_mm: float = 0.15,
) -> str:
    """Produce an SVG showing cut paths, with coordinates in inches.

    Kiss-cut contours are drawn in red.  If perf_contours is provided,
    they are drawn in orange with a dashed stroke.
    """
    scale = 1.0 / dpi   # px → inches
    path_els: List[str] = []

    for pts in contours:
        if not pts:
            continue
        d = f"M {pts[0][0] * scale:.4f},{pts[0][1] * scale:.4f}"
        for x, y in pts[1:]:
            d += f" L {x * scale:.4f},{y * scale:.4f}"
        d += " Z"
        path_els.append(
            f'  <path d="{d}" fill="none" stroke="#ff0000" stroke-width="0.01"/>'
        )

    if perf_contours:
        dash_in = perf_dash_mm / MM_PER_INCH
        gap_in = perf_gap_mm / MM_PER_INCH
        for pts in perf_contours:
            if not pts:
                continue
            d = f"M {pts[0][0] * scale:.4f},{pts[0][1] * scale:.4f}"
            for x, y in pts[1:]:
                d += f" L {x * scale:.4f},{y * scale:.4f}"
            d += " Z"
            path_els.append(
                f'  <path d="{d}" fill="none" stroke="#ff8800" stroke-width="0.012"'
                f' stroke-dasharray="{dash_in:.4f},{gap_in:.4f}"/>'
            )

    inner = "\n".join(path_els)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' width="{CANVAS_W_IN}in" height="{CANVAS_H_IN}in"'
        f' viewBox="0 0 {CANVAS_W_IN} {CANVAS_H_IN}">\n'
        f'{inner}\n'
        f'</svg>\n'
    )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class LayoutResult:
    batch_index: int                    # 0-based
    placed: List[PlacedItem]
    overflow_paths: List[Path]          # images not placed in this batch
    composite: "PIL.Image.Image"        # RGB image, ready to save as JPEG
    cut_svg: str
    cut_plt: str


@dataclass
class SheetPngResult:
    jpg_bytes: bytes
    plt_bytes: bytes
    cut_svg: str
    contour_count: int
    mask_source: str


def _jpeg_bytes_under_limit(img: "PIL.Image.Image", max_bytes: int = 1024 * 1024) -> bytes:
    """Encode JPEG, stepping quality down until it fits the observed device limit."""
    for quality in (92, 88, 84, 80, 76, 72, 68, 64, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    raise ValueError(
        f"JPEG is still larger than {max_bytes} bytes after compression; "
        "simplify the artwork or lower the output DPI."
    )


def _infer_flattened_sheet_mask(
    rgb: "numpy.ndarray",
    *,
    dpi: int,
    threshold: int,
    border_mm: float,
    ignore_bottom_mm: float,
    min_component_mm2: float,
) -> "numpy.ndarray":
    """
    Infer sticker islands from a flattened RGB sheet.

    This is intentionally heuristic: it treats the corner color as the page
    background, finds pixels that differ from it, grows that content to include
    the white sticker border, then removes tiny artifacts.
    """
    import numpy as np
    from skimage import morphology

    h, w, _ = rgb.shape
    patch = max(8, min(80, min(w, h) // 40))
    corner_samples = np.concatenate([
        rgb[:patch, :patch].reshape(-1, 3),
        rgb[:patch, -patch:].reshape(-1, 3),
        rgb[-patch:, :patch].reshape(-1, 3),
        rgb[-patch:, -patch:].reshape(-1, 3),
    ])
    bg = np.median(corner_samples, axis=0)
    delta = rgb.astype("int32") - bg.astype("int32")
    non_bg = np.sqrt(np.sum(delta * delta, axis=2)) >= threshold

    if ignore_bottom_mm > 0:
        ignore_px = int(round((ignore_bottom_mm / MM_PER_INCH) * dpi))
        if ignore_px > 0:
            non_bg[max(0, h - ignore_px):, :] = False

    border_px = int(round((border_mm / MM_PER_INCH) * dpi))
    if border_px > 0:
        if hasattr(morphology, "isotropic_dilation"):
            mask = morphology.isotropic_dilation(non_bg, border_px)
        else:
            mask = morphology.binary_dilation(non_bg, morphology.disk(border_px))
    else:
        mask = non_bg

    hole_area_px = max(64, int(mask.size * 0.02))
    mask = morphology.remove_small_holes(mask, area_threshold=hole_area_px)

    min_component_px = max(
        16,
        int((min_component_mm2 / (MM_PER_INCH ** 2)) * (dpi ** 2)),
    )
    mask = morphology.remove_small_objects(mask, min_size=min_component_px)
    return mask


def process_sheet_png(
    png_path: Path,
    *,
    dpi: int = DEFAULT_LAYOUT_DPI,
    margin_mm: float = 0.0,
    min_area_mm2: float = DEFAULT_MIN_AREA_MM2,
    kp: int = DEFAULT_KP,
    simplify: float = DEFAULT_SIMPLIFY,
    straight_dist_eps: float = STRAIGHT_DIST_EPS,
    infer_flattened: bool = True,
    infer_threshold: int = 24,
    infer_border_mm: float = 3.0,
    ignore_bottom_mm: float = 0.0,
    infer_min_component_mm2: float = 25.0,
) -> SheetPngResult:
    """
    Process one pre-laid 4x7 PNG sheet.

    If a meaningful alpha channel exists, it is used as the cut mask. If the
    PNG is flattened/fully opaque, a mask is inferred from the rendered RGB
    content instead.
    """
    _check_deps()
    from PIL import Image
    import numpy as np

    canvas_w_px = int(CANVAS_W_IN * dpi)
    canvas_h_px = int(CANVAS_H_IN * dpi)
    expected_ratio = CANVAS_W_IN / CANVAS_H_IN

    img = Image.open(png_path).convert("RGBA")
    ratio = img.width / img.height
    if abs(ratio - expected_ratio) > 0.01:
        raise ValueError(
            f"PNG must be a 4x7 sheet. Got {img.width}x{img.height}; "
            "generate a 4:7 canvas such as 1200x2100 or 2880x5040."
        )

    if img.size != (canvas_w_px, canvas_h_px):
        img = img.resize((canvas_w_px, canvas_h_px), Image.LANCZOS)

    alpha = np.array(img.split()[3])
    rgb = np.array(img.convert("RGB"))
    mask_source = "alpha"

    if not np.any(alpha > 10):
        raise ValueError("PNG alpha is fully transparent; no cuttable sticker area was found.")

    if np.all(alpha == 255):
        if not infer_flattened:
            raise ValueError(
                "PNG alpha is fully opaque, so the only detectable cut path is the whole sheet."
            )
        mask = _infer_flattened_sheet_mask(
            rgb,
            dpi=dpi,
            threshold=infer_threshold,
            border_mm=infer_border_mm,
            ignore_bottom_mm=ignore_bottom_mm,
            min_component_mm2=infer_min_component_mm2,
        )
        mask_source = "inferred-rgb"
    else:
        if np.any(alpha == 254) or np.min(alpha) >= 250:
            mask = alpha == 255
        else:
            mask = alpha > 10

    coverage = float(np.count_nonzero(mask)) / float(mask.size)
    if coverage <= 0.0:
        raise ValueError("No cuttable sticker area was found in the PNG.")
    if coverage > 0.95:
        raise ValueError(
            "PNG cut mask covers almost the whole sheet. Set alpha 255 only on sticker regions, "
            "not on the page background, or adjust the flattened PNG inference parameters."
        )

    min_area_px = (min_area_mm2 / (MM_PER_INCH ** 2)) * (dpi ** 2)
    raw_contours = _trace_contours(mask, min_area_px)
    if not raw_contours:
        raise ValueError("No usable cut contours found in the PNG cut mask.")

    margin_px = (margin_mm / MM_PER_INCH) * dpi
    kiss_contours = _apply_margin(raw_contours, margin_px)
    kiss_contours = [prune_straight_segments(c, dist_eps=straight_dist_eps) for c in kiss_contours]
    kiss_contours = [simplify_polyline(c, simplify) for c in kiss_contours if len(c) >= 3]
    if not kiss_contours:
        raise ValueError("No usable cut contours remained after simplification.")

    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    composite_rgb = Image.alpha_composite(white, img).convert("RGB")
    jpg_bytes = _jpeg_bytes_under_limit(composite_rgb)

    plt_kiss = _canvas_to_plt_coords(kiss_contours, canvas_w_px, canvas_h_px)
    plt_parts = ["IN", "VER0.1.0", f"KP{kp}"]
    plt_parts += _plt_path_commands(plt_kiss)
    plt_parts.append(" U6476,0 @ ")
    cut_plt = " ".join(plt_parts)

    cut_svg = _contours_to_svg(kiss_contours, canvas_w_px, canvas_h_px, dpi)
    return SheetPngResult(
        jpg_bytes=jpg_bytes,
        plt_bytes=cut_plt.encode("ascii"),
        cut_svg=cut_svg,
        contour_count=len(kiss_contours),
        mask_source=mask_source,
    )


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

def process_images(
    img_paths: List[Path],
    *,
    dpi: int = DEFAULT_LAYOUT_DPI,
    margin_mm: float = DEFAULT_MARGIN_MM,
    padding_mm: float = DEFAULT_PADDING_MM,
    left_margin_mm: float = 0.0,
    min_area_mm2: float = DEFAULT_MIN_AREA_MM2,
    kp: int = DEFAULT_KP,
    simplify: float = DEFAULT_SIMPLIFY,
    straight_dist_eps: float = STRAIGHT_DIST_EPS,
    paginate: bool = False,
    bg_white: bool = False,
    perf_cut: bool = False,
    perf_kp: int = 53,
    perf_dash_mm: float = 8.0,
    perf_gap_mm: float = 0.05,
    bg_image_path: Optional[Path] = None,
) -> List[LayoutResult]:
    """
    Process one or more images into a PixCut-ready sticker layout.

    Returns a list of LayoutResult — usually one entry, more if paginate=True
    and images overflow the 4×7" canvas.

    Each LayoutResult has:
      .composite     — PIL RGB Image (save as JPEG, send with --jpg)
      .cut_plt       — PLT string (send with --plt)
      .cut_svg       — SVG string (Inkscape / browser preview)
      .overflow_paths — paths skipped in this batch (populated only when paginate=True)
    """
    _check_deps()
    from PIL import Image
    import numpy as np

    canvas_w_px = int(CANVAS_W_IN * dpi)
    canvas_h_px = int(CANVAS_H_IN * dpi)
    margin_px = (margin_mm / MM_PER_INCH) * dpi
    margin_offset = int(round(abs(margin_px)))          # px reserved around each image for its cutline halo
    padding_px = int((padding_mm / MM_PER_INCH) * dpi)
    left_margin_px = int((left_margin_mm / MM_PER_INCH) * dpi)
    min_area_px = (min_area_mm2 / (MM_PER_INCH ** 2)) * (dpi ** 2)
    # Max image dimensions: canvas minus padding, margin halo, and extra left margin
    max_item_w = canvas_w_px - 2 * padding_px - 2 * margin_offset - left_margin_px
    max_item_h = canvas_h_px - 2 * padding_px - 2 * margin_offset

    # Load and prepare all images
    items: List[_Item] = []
    skipped: List[Path] = []
    for path in img_paths:
        img = _load_rgba(path, bg_white=bg_white)

        # Scale down if the image exceeds canvas bounds (warn)
        if img.width > max_item_w or img.height > max_item_h:
            scale_f = min(max_item_w / img.width, max_item_h / img.height)
            new_w = max(1, int(img.width * scale_f))
            new_h = max(1, int(img.height * scale_f))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        mask = _alpha_mask(img)
        raw_contours = _trace_contours(mask, min_area_px)
        if not raw_contours:
            skipped.append(path)
            continue

        kiss_contours = _apply_margin(raw_contours, margin_px)
        kiss_contours = [prune_straight_segments(c, dist_eps=straight_dist_eps) for c in kiss_contours]
        kiss_contours = [simplify_polyline(c, simplify) for c in kiss_contours if len(c) >= 3]
        if not kiss_contours:
            skipped.append(path)
            continue

        items.append(_Item(
            path=path, img=img, contours=kiss_contours,
            w=img.width, h=img.height,
            margin_offset=margin_offset,
            effective_w=img.width + 2 * margin_offset,
            effective_h=img.height + 2 * margin_offset,
        ))

    if skipped:
        import logging
        logging.getLogger("pixcut.image_to_cut").warning(
            "Skipped %d image(s) — no detectable content (check alpha/white-bg): %s",
            len(skipped),
            ", ".join(p.name for p in skipped),
        )

    if not items:
        raise ValueError("No usable images after loading — nothing to lay out.")

    # Sort tallest-effective-first for shelf-packing efficiency
    items.sort(key=lambda it: it.effective_w * it.effective_h, reverse=True)

    results: List[LayoutResult] = []
    remaining_indices = list(range(len(items)))
    batch_idx = 0

    while remaining_indices:
        batch = [items[i] for i in remaining_indices]
        placements, overflow_local = _pack(batch, canvas_w_px, canvas_h_px, padding_px, left_margin_px)
        shift_x = _center_shift(placements, batch, canvas_w_px, padding_px)
        placements = [(idx, x + shift_x, y) for idx, x, y in placements]

        if not placements:
            # Nothing fits (shouldn't happen after the scale-down guard, but be safe)
            overflow_paths = [items[i].path for i in remaining_indices]
            import logging
            logging.getLogger("pixcut.image_to_cut").error(
                "No items could be packed into batch %d — images may be too large.",
                batch_idx + 1,
            )
            results.append(LayoutResult(
                batch_index=batch_idx,
                placed=[],
                overflow_paths=overflow_paths,
                composite=Image.new("RGB", (canvas_w_px, canvas_h_px), (255, 255, 255)),
                cut_svg=_contours_to_svg([], canvas_w_px, canvas_h_px, dpi),
                cut_plt=points_to_plt([], kp=kp),
            ))
            break

        overflow_paths = [batch[i].path for i in overflow_local]

        # Build canvas — transparent so background shows through in composite step.
        canvas = Image.new("RGBA", (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
        placed: List[PlacedItem] = []

        for item_local_idx, cx, cy in placements:
            item = batch[item_local_idx]
            # cx, cy is the top-left of the effective footprint (which includes the margin halo).
            # The actual image is inset by margin_offset so the cutline halo has room.
            img_x = cx + item.margin_offset
            img_y = cy + item.margin_offset
            alpha = item.img.split()[3]
            canvas.paste(item.img, (img_x, img_y), alpha)

            # Contours are in image-local coords (may be negative by up to margin_offset).
            # Translate to canvas coords relative to the image's placed position.
            canvas_kiss = [
                [(px + img_x, py + img_y) for px, py in c]
                for c in item.contours
            ]
            canvas_perf = [
                [(px + img_x, py + img_y) for px, py in c]
                for c in item.perf_contours
            ]
            placed.append(PlacedItem(
                source_path=item.path,
                img=item.img,
                contours=canvas_kiss,
                x=img_x, y=img_y, w=item.w, h=item.h,
                perf_contours=canvas_perf,
            ))

        # Composite over background (or white), convert to RGB for JPEG
        if bg_image_path and Path(bg_image_path).is_file():
            try:
                bg_src = Image.open(bg_image_path).convert("RGB")
                bg = bg_src.resize(canvas.size, Image.LANCZOS).convert("RGBA")
            except Exception:
                bg = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
        else:
            bg = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
        composite_rgb = Image.alpha_composite(bg, canvas).convert("RGB")

        all_kiss: List[Contour] = [c for p in placed for c in p.contours]
        plt_kiss = _canvas_to_plt_coords(all_kiss, canvas_w_px, canvas_h_px)

        units_per_mm = DEFAULT_UNITS_PER_INCH / MM_PER_INCH
        if perf_cut:
            dash_u = perf_dash_mm * units_per_mm
            gap_u = perf_gap_mm * units_per_mm
            plt_parts = ["IN", "VER0.1.0"]
            plt_parts += _plt_path_commands(plt_kiss, dash_u, gap_u, dash_kp=perf_kp, gap_kp=kp,
                                            nudge_u=0.2 * units_per_mm)
        else:
            plt_parts = ["IN", "VER0.1.0", f"KP{kp}"]
            plt_parts += _plt_path_commands(plt_kiss)
        plt_parts.append(" U6476,0 @ ")
        cut_plt = " ".join(plt_parts)

        cut_svg = _contours_to_svg(all_kiss, canvas_w_px, canvas_h_px, dpi)

        results.append(LayoutResult(
            batch_index=batch_idx,
            placed=placed,
            overflow_paths=overflow_paths,
            composite=composite_rgb,
            cut_svg=cut_svg,
            cut_plt=cut_plt,
        ))

        if not paginate or not overflow_local:
            if overflow_local and not paginate:
                import logging
                logging.getLogger("pixcut.image_to_cut").warning(
                    "%d image(s) did not fit on the canvas and were skipped: %s\n"
                    "  Re-run with --paginate to create additional sheets.",
                    len(overflow_local),
                    ", ".join(batch[i].path.name for i in overflow_local),
                )
            break

        remaining_indices = [remaining_indices[i] for i in overflow_local]
        batch_idx += 1

    return results


# ---------------------------------------------------------------------------
# Stateful canvas for incremental kiosk-style layout building
# ---------------------------------------------------------------------------

class LayoutCanvas:
    """
    Incremental sticker canvas.  Add/remove images one at a time; the canvas
    repacks and produces an updated JPEG preview after every change.

    Each selection stores (path, count, scale) where scale is a float multiplier
    (1.0 = original size, 0.5 = half size, 2.0 = double size).

    Thread-safe: all mutations are protected by an RLock.

    Usage::

        canvas = LayoutCanvas()
        canvas.add(Path("stickers/cat.png"), count=2, scale=1.5)
        canvas.add(Path("stickers/dog.png"))
        preview_bytes = canvas.preview_jpeg()          # show in UI
        jpg, plt, svg = canvas.finalize()              # send to printer
    """

    def __init__(
        self,
        dpi: int = DEFAULT_LAYOUT_DPI,
        margin_mm: float = DEFAULT_MARGIN_MM,
        padding_mm: float = DEFAULT_PADDING_MM,
        left_margin_mm: float = 3.0,
        kp: int = DEFAULT_KP,
        simplify: float = DEFAULT_SIMPLIFY,
        straight_dist_eps: float = STRAIGHT_DIST_EPS,
        bg_white: bool = False,
        min_area_mm2: float = DEFAULT_MIN_AREA_MM2,
        perf_cut: bool = False,
        perf_kp: int = 53,
        perf_dash_mm: float = 8.0,
        perf_gap_mm: float = 0.05,
        bg_image_path: Optional[Path] = None,
    ):
        self._dpi = dpi
        self._margin_mm = margin_mm
        self._padding_mm = padding_mm
        self._left_margin_mm = left_margin_mm
        self._kp = kp
        self._simplify = simplify
        self._straight_dist_eps = straight_dist_eps
        self._bg_white = bg_white
        self._min_area_mm2 = min_area_mm2
        self._perf_cut = perf_cut
        self._perf_kp = max(1, int(perf_kp))
        self._perf_dash_mm = max(0.1, float(perf_dash_mm))
        self._perf_gap_mm = max(0.01, float(perf_gap_mm))
        self._bg_image_path: Optional[Path] = Path(bg_image_path) if bg_image_path else None

        self._lock = threading.RLock()
        # Ordered list of (path, count, scale) — preserves selection order for the UI.
        self._selections: List[Tuple[Path, int, float]] = []
        # User-facing names keyed by str(path.resolve()) — may differ from p.name for subfolders/USB.
        self._names: Dict[str, str] = {}
        # Processed _Item cache keyed by "{abs_path}@{scale:.3f}".
        self._cache: Dict[str, _Item] = {}
        # Most recent packed result; None when canvas is empty or not yet repacked.
        self._result: Optional[LayoutResult] = None
        # Stickers-only RGBA canvas (no background); background applied at output time.
        self._canvas_rgba = None
        self._overflow_count: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _canvas_dims(self) -> Tuple[int, int]:
        return int(CANVAS_W_IN * self._dpi), int(CANVAS_H_IN * self._dpi)

    def _ensure_item(self, path: Path, scale: float = 1.0) -> Optional[_Item]:
        """Load and cache a processed _Item for *path* at *scale*.  Returns None on failure."""
        cache_key = f"{path.resolve()}@{scale:.3f}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        from PIL import Image as _PIL_Image

        canvas_w_px, canvas_h_px = self._canvas_dims()
        margin_px = (self._margin_mm / MM_PER_INCH) * self._dpi
        margin_offset = int(round(abs(margin_px)))
        padding_px = int((self._padding_mm / MM_PER_INCH) * self._dpi)
        min_area_px = (self._min_area_mm2 / (MM_PER_INCH ** 2)) * (self._dpi ** 2)
        left_margin_px = int((self._left_margin_mm / MM_PER_INCH) * self._dpi)
        max_item_w = canvas_w_px - 2 * padding_px - 2 * margin_offset - left_margin_px
        max_item_h = canvas_h_px - 2 * padding_px - 2 * margin_offset

        try:
            img = _load_rgba(path, bg_white=self._bg_white)
        except Exception as exc:
            import logging
            logging.getLogger("pixcut.image_to_cut").warning("Could not load %s: %s", path, exc)
            return None

        # Apply user-requested scale first, then clamp to canvas bounds.
        if scale != 1.0:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), _PIL_Image.LANCZOS)

        if img.width > max_item_w or img.height > max_item_h:
            clamp_f = min(max_item_w / img.width, max_item_h / img.height)
            img = img.resize((max(1, int(img.width * clamp_f)), max(1, int(img.height * clamp_f))), _PIL_Image.LANCZOS)

        mask = _alpha_mask(img)
        raw_contours = _trace_contours(mask, min_area_px)
        if not raw_contours:
            return None

        # Kiss-cut contours at the configured margin.
        kiss_contours = _apply_margin(raw_contours, margin_px)
        kiss_contours = [prune_straight_segments(c, dist_eps=self._straight_dist_eps) for c in kiss_contours]
        kiss_contours = [simplify_polyline(c, self._simplify) for c in kiss_contours if len(c) >= 3]
        if not kiss_contours:
            return None

        item = _Item(
            path=path, img=img, contours=kiss_contours,
            w=img.width, h=img.height,
            margin_offset=margin_offset,
            effective_w=img.width + 2 * margin_offset,
            effective_h=img.height + 2 * margin_offset,
        )
        self._cache[cache_key] = item
        return item

    def _repack(self) -> None:
        """Repack the canvas from current selections.  Updates self._result."""
        from PIL import Image as _PIL_Image

        canvas_w_px, canvas_h_px = self._canvas_dims()
        padding_px = int((self._padding_mm / MM_PER_INCH) * self._dpi)
        left_margin_px = int((self._left_margin_mm / MM_PER_INCH) * self._dpi)

        # Expand selections into a flat item list.
        flat_items: List[_Item] = []
        for path, count, scale in self._selections:
            base = self._ensure_item(path, scale)
            if base is None:
                continue
            for _ in range(count):
                flat_items.append(base)

        if not flat_items:
            self._result = None
            self._canvas_rgba = None
            self._overflow_count = 0
            return

        # Sort tallest-effective-first.
        sorted_items = sorted(flat_items, key=lambda it: it.effective_w * it.effective_h, reverse=True)

        placements, overflow = _pack(sorted_items, canvas_w_px, canvas_h_px, padding_px, left_margin_px)
        self._overflow_count = len(overflow)
        shift_x = _center_shift(placements, sorted_items, canvas_w_px, padding_px)
        placements = [(idx, x + shift_x, y) for idx, x, y in placements]

        # Build composite — transparent fill so background shows through in _build_jpeg().
        canvas_img = _PIL_Image.new("RGBA", (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
        placed: List[PlacedItem] = []

        for item_local_idx, cx, cy in placements:
            item = sorted_items[item_local_idx]
            img_x = cx + item.margin_offset
            img_y = cy + item.margin_offset
            alpha = item.img.split()[3]
            canvas_img.paste(item.img, (img_x, img_y), alpha)
            canvas_kiss = [
                [(px + img_x, py + img_y) for px, py in c]
                for c in item.contours
            ]
            canvas_perf = [
                [(px + img_x, py + img_y) for px, py in c]
                for c in item.perf_contours
            ]
            placed.append(PlacedItem(
                source_path=item.path, img=item.img,
                contours=canvas_kiss, x=img_x, y=img_y, w=item.w, h=item.h,
                perf_contours=canvas_perf,
            ))

        # Store the stickers-only RGBA canvas; background is applied at output time.
        self._canvas_rgba = canvas_img
        composite_rgb = _PIL_Image.alpha_composite(
            _PIL_Image.new("RGBA", canvas_img.size, (255, 255, 255, 255)),
            canvas_img,
        ).convert("RGB")

        all_kiss: List[Contour] = [c for p in placed for c in p.contours]
        plt_kiss = _canvas_to_plt_coords(all_kiss, canvas_w_px, canvas_h_px)

        units_per_mm = DEFAULT_UNITS_PER_INCH / MM_PER_INCH
        if self._perf_cut:
            dash_u = self._perf_dash_mm * units_per_mm
            gap_u = self._perf_gap_mm * units_per_mm
            plt_parts = ["IN", "VER0.1.0"]
            plt_parts += _plt_path_commands(plt_kiss, dash_u, gap_u, dash_kp=self._perf_kp,
                                            gap_kp=self._kp, nudge_u=0.2 * units_per_mm)
        else:
            plt_parts = ["IN", "VER0.1.0", f"KP{self._kp}"]
            plt_parts += _plt_path_commands(plt_kiss)
        plt_parts.append(" U6476,0 @ ")
        cut_plt = " ".join(plt_parts)

        cut_svg = _contours_to_svg(all_kiss, canvas_w_px, canvas_h_px, self._dpi)

        self._result = LayoutResult(
            batch_index=0,
            placed=placed,
            overflow_paths=[],
            composite=composite_rgb,
            cut_svg=cut_svg,
            cut_plt=cut_plt,
        )

    def _status(self) -> dict:
        total_requested = sum(c for _, c, _ in self._selections)
        total_placed = total_requested - self._overflow_count
        return {
            "selections": [
                {"name": self._names.get(str(p.resolve()), p.name), "count": c, "scale": round(s, 3)}
                for p, c, s in self._selections
            ],
            "overflow_count": self._overflow_count,
            "total_placed": total_placed,
            "total_requested": total_requested,
            "kp": self._kp,
            "margin_mm": round(self._margin_mm, 2),
            "padding_mm": round(self._padding_mm, 2),
            "left_margin_mm": round(self._left_margin_mm, 2),
            "perf_cut": self._perf_cut,
            "perf_kp": self._perf_kp,
            "perf_dash_mm": round(self._perf_dash_mm, 2),
            "perf_gap_mm": round(self._perf_gap_mm, 3),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return len(self._selections) == 0

    def status(self) -> dict:
        with self._lock:
            return self._status()

    def add(self, path: Path, count: int = 1, scale: float = 1.0, name: str = "") -> dict:
        """Add *count* copies of *path* at *scale*.  If already selected, increments count."""
        _check_deps()
        with self._lock:
            key = str(path.resolve())
            self._names[key] = name or path.name
            for i, (p, c, s) in enumerate(self._selections):
                if str(p.resolve()) == key:
                    self._selections[i] = (p, c + count, s)
                    self._repack()
                    return self._status()
            self._selections.append((path, count, scale))
            self._repack()
            return self._status()

    def remove(self, path: Path) -> dict:
        """Remove *path* from selections entirely."""
        with self._lock:
            key = str(path.resolve())
            self._selections = [(p, c, s) for p, c, s in self._selections if str(p.resolve()) != key]
            self._names.pop(key, None)
            self._repack()
            return self._status()

    def set_count(self, path: Path, count: int) -> dict:
        """Set exact copy count for *path*.  Removes if count <= 0."""
        with self._lock:
            if count <= 0:
                return self.remove(path)
            key = str(path.resolve())
            for i, (p, c, s) in enumerate(self._selections):
                if str(p.resolve()) == key:
                    self._selections[i] = (p, count, s)
                    self._repack()
                    return self._status()
            # Not yet selected — add it at default scale.
            _check_deps()
            self._selections.append((path, count, 1.0))
            self._repack()
            return self._status()

    def set_scale(self, path: Path, scale: float) -> dict:
        """Set the size multiplier for *path* (e.g. 0.5 = half, 2.0 = double)."""
        scale = max(0.1, scale)   # floor to prevent degenerate items
        with self._lock:
            key = str(path.resolve())
            for i, (p, c, _s) in enumerate(self._selections):
                if str(p.resolve()) == key:
                    self._selections[i] = (p, c, scale)
                    self._repack()
                    return self._status()
            # Not selected — nothing to scale.
            return self._status()

    def set_kp(self, kp: int) -> dict:
        """Update knife pressure and regenerate PLT output (no re-layout needed)."""
        with self._lock:
            self._kp = max(1, int(kp))
            self._repack()
            return self._status()

    def set_left_margin(self, left_margin_mm: float) -> dict:
        """Update the left paper margin and repack."""
        with self._lock:
            self._left_margin_mm = max(0.0, float(left_margin_mm))
            self._cache.clear()   # max_item_w changes; cached items may need reprocessing
            self._repack()
            return self._status()

    def set_margin(self, margin_mm: float) -> dict:
        """Update the cut margin (outward offset from sticker edge) and repack."""
        with self._lock:
            self._margin_mm = max(0.0, float(margin_mm))
            self._cache.clear()   # contours and margin_offset change
            self._repack()
            return self._status()

    def set_padding(self, padding_mm: float) -> dict:
        """Update the gap between stickers and repack."""
        with self._lock:
            self._padding_mm = max(0.0, float(padding_mm))
            self._cache.clear()   # max_item_w changes
            self._repack()
            return self._status()

    def set_perf_cut(self, enabled: bool) -> dict:
        """Toggle perf-cut on or off.  Clears cache because perf contours must be (re)computed."""
        with self._lock:
            self._perf_cut = bool(enabled)
            self._cache.clear()
            self._repack()
            return self._status()

    def set_perf_kp(self, kp: int) -> dict:
        """Update perf-cut knife pressure (no contour change, just PLT regeneration)."""
        with self._lock:
            self._perf_kp = max(1, int(kp))
            self._repack()
            return self._status()

    def set_perf_dash(self, dash_mm: float) -> dict:
        """Update perf-cut dash length (PLT regeneration only)."""
        with self._lock:
            self._perf_dash_mm = max(0.1, float(dash_mm))
            self._repack()
            return self._status()

    def set_perf_gap(self, gap_mm: float) -> dict:
        """Update perf-cut gap length between dashes (PLT regeneration only)."""
        with self._lock:
            self._perf_gap_mm = max(0.01, float(gap_mm))
            self._repack()
            return self._status()

    def set_bg_image(self, path: Optional[Path]) -> dict:
        """Set or clear the background image (print-only, not cut). Pass None to revert to white."""
        with self._lock:
            self._bg_image_path = Path(path) if path else None
            return self._status()

    def clear(self) -> None:
        """Reset the canvas to empty."""
        with self._lock:
            self._selections.clear()
            self._names.clear()
            self._result = None
            self._canvas_rgba = None
            self._overflow_count = 0

    def _build_jpeg(self, quality: int) -> bytes:
        """Composite _canvas_rgba over the background (or white) and return JPEG bytes."""
        import logging as _logging
        from PIL import Image as _PIL_Image
        canvas_w_px, canvas_h_px = self._canvas_dims()
        canvas = self._canvas_rgba if self._canvas_rgba is not None else \
            _PIL_Image.new("RGBA", (canvas_w_px, canvas_h_px), (0, 0, 0, 0))
        if self._bg_image_path and self._bg_image_path.is_file():
            try:
                bg_src = _PIL_Image.open(self._bg_image_path).convert("RGB")
                bg = bg_src.resize(canvas.size, _PIL_Image.LANCZOS).convert("RGBA")
            except Exception as exc:
                _logging.getLogger("pixcut.image_to_cut").warning(
                    "Failed to load background image %s: %s", self._bg_image_path, exc)
                bg = _PIL_Image.new("RGBA", canvas.size, (255, 255, 255, 255))
        else:
            bg = _PIL_Image.new("RGBA", canvas.size, (255, 255, 255, 255))
        result = _PIL_Image.alpha_composite(bg, canvas).convert("RGB")
        buf = io.BytesIO()
        result.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def preview_jpeg(self, quality: int = 82) -> bytes:
        """Return the current canvas composite as JPEG bytes."""
        with self._lock:
            return self._build_jpeg(quality)

    def finalize(self) -> Tuple[bytes, bytes, str]:
        """Return (jpg_bytes, plt_bytes, svg_str) for the current canvas.

        Raises ValueError if the canvas is empty.
        """
        with self._lock:
            if self._result is None:
                raise ValueError("Canvas is empty — add stickers before printing.")
            jpg_bytes = self._build_jpeg(quality=92)
            return jpg_bytes, self._result.cut_plt.encode(), self._result.cut_svg
