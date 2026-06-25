"""
PixCut Kiosk UI Server

Hosts a local web UI for selecting stickers, building a print canvas
incrementally, and sending print+cut jobs to the PixCut S1 printer.

The CLI (pixcut_cli.py) remains fully independent — this is an optional add-on.

Usage:
    python server.py
    python server.py --host 0.0.0.0 --port 8000
    python server.py --stickers ./stickers --dpi 300 --margin 1.0 --padding 2.0
    python server.py --no-auto-detect --vid 0x302C --pid 0x3101
"""
from __future__ import annotations

import argparse
import logging
import os
import plistlib
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    import colorama
    colorama.just_fix_windows_console()
except ImportError:
    pass

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("pixcut.server")

# ---------------------------------------------------------------------------
# App-level config (populated by main() from argparse)
# ---------------------------------------------------------------------------
_cfg: Dict = {
    "stickers_dir": Path("stickers"),
    "backgrounds_dir": Path("backgrounds"),
    "dpi": 300,
    "margin_mm": 1.0,
    "padding_mm": 2.0,
    "left_margin_mm": 3.0,
    "kp": 42,
    "auto_detect": True,
    "vid": None,
    "pid": None,
    "perf_cut": False,
    "perf_kp": 53,
    "perf_dash_mm": 8.0,
    "perf_gap_mm": 0.05,
    "bg_image": None,          # Path or None
    "usb": True,
    "api_key": None,
}

# ---------------------------------------------------------------------------
# Canvas singleton
# ---------------------------------------------------------------------------
_canvas = None
_canvas_lock = threading.Lock()


def _get_canvas():
    global _canvas
    with _canvas_lock:
        if _canvas is None:
            from pixcut.image_to_cut import LayoutCanvas
            _canvas = LayoutCanvas(
                dpi=_cfg["dpi"],
                margin_mm=_cfg["margin_mm"],
                padding_mm=_cfg["padding_mm"],
                left_margin_mm=_cfg["left_margin_mm"],
                kp=_cfg["kp"],
                perf_cut=_cfg["perf_cut"],
                perf_kp=_cfg["perf_kp"],
                perf_dash_mm=_cfg["perf_dash_mm"],
                perf_gap_mm=_cfg["perf_gap_mm"],
                bg_image_path=_cfg["bg_image"],
            )
    return _canvas


# ---------------------------------------------------------------------------
# USB hot-plug monitor
# ---------------------------------------------------------------------------
_usb_generation: int = 0
_usb_gen_lock = threading.Lock()
_usb_last_dirs: set = set()


def _usb_monitor_thread() -> None:
    global _usb_generation, _usb_last_dirs
    while True:
        time.sleep(3)
        current = {str(d) for d in _usb_sticker_dirs()} if _cfg["usb"] else set()
        with _usb_gen_lock:
            if current != _usb_last_dirs:
                _usb_last_dirs = current
                _usb_generation += 1


# ---------------------------------------------------------------------------
# Print job state
# ---------------------------------------------------------------------------
_print_state: Dict = {
    "status": "idle",       # idle | printing | done | error
    "message": "",
    "error": None,
    "job_id": None,
    "request_id": None,
}
_print_lock = threading.Lock()
_print_thread: Optional[threading.Thread] = None


def _new_request_id() -> str:
    return secrets.token_urlsafe(12)


def _require_api_key(request: Request) -> None:
    """Require X-API-Key or Authorization: Bearer when an API key is configured."""
    expected = _cfg.get("api_key")
    if not expected:
        return

    supplied = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()

    if not supplied or not secrets.compare_digest(str(supplied), str(expected)):
        raise HTTPException(401, "Invalid or missing API key")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="PixCut Kiosk")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class CanvasAddRequest(BaseModel):
    name: str
    count: int = 1
    scale: float = 1.0


class CanvasRemoveRequest(BaseModel):
    name: str


class CanvasSetCountRequest(BaseModel):
    name: str
    count: int


class CanvasSetScaleRequest(BaseModel):
    name: str
    scale: float   # multiplier: 1.0 = original, 0.5 = half, 2.0 = double


class SettingsRequest(BaseModel):
    kp: Optional[int] = None
    left_margin_mm: Optional[float] = None
    margin_mm: Optional[float] = None
    padding_mm: Optional[float] = None
    perf_cut: Optional[bool] = None
    perf_kp: Optional[int] = None
    perf_dash_mm: Optional[float] = None
    perf_gap_mm: Optional[float] = None
    bg_image: Optional[str] = None   # filename from /api/backgrounds, or "" to clear


# ---------------------------------------------------------------------------
# Routes: sticker listing (with subfolder and USB media support)
# ---------------------------------------------------------------------------

def _macos_external_mount_points() -> List[Path]:
    """Return mount points of external drives on macOS via diskutil."""
    try:
        tmp = subprocess.run(
            ["diskutil", "list", "-plist", "external"],
            capture_output=True, timeout=5,
        )
        info = plistlib.loads(tmp.stdout)
        mounts: List[Path] = []
        for disk in info.get("AllDisksAndPartitions", []):
            for entry in [disk] + disk.get("Partitions", []):
                mp = entry.get("MountPoint")
                if mp:
                    mounts.append(Path(mp))
        return mounts
    except Exception:
        return []


def _has_visible_png(directory: Path) -> bool:
    """Return True if directory contains any non-hidden PNG file (top two levels only)."""
    try:
        for p in directory.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_file() and p.suffix.lower() == ".png":
                return True
            if p.is_dir() and not p.name.startswith("."):
                try:
                    if any(
                        f for f in p.iterdir()
                        if not f.name.startswith(".") and f.is_file() and f.suffix.lower() == ".png"
                    ):
                        return True
                except (PermissionError, OSError):
                    continue
        return False
    except (PermissionError, OSError):
        return False


def _usb_sticker_dirs() -> List[Path]:
    """Return mounted USB media directories that contain PNG files.

    Handles three layouts:
      /media/<user>/<label>/  — Linux desktop (udisks2); actual mount is two levels deep
      /mnt/<label>/           — Linux manual mounts; mount is one level deep
      macOS                   — external volumes via diskutil (avoids scanning internal drives)
    """
    candidates: List[Path] = []

    if sys.platform == "darwin":
        for mount in _macos_external_mount_points():
            if mount.is_dir() and _has_visible_png(mount):
                candidates.append(mount)
    elif sys.platform == "win32":
        import ctypes
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            # GetDriveTypeW == 2 → DRIVE_REMOVABLE (USB flash drives)
            if ctypes.windll.kernel32.GetDriveTypeW(str(drive)) == 2:
                if drive.is_dir() and _has_visible_png(drive):
                    candidates.append(drive)
    else:
        for mount_root in (Path("/media"), Path("/mnt")):
            if not mount_root.is_dir():
                continue
            try:
                top_dirs = [d for d in mount_root.iterdir() if d.is_dir()]
            except (PermissionError, OSError):
                continue
            for top in top_dirs:
                if mount_root.name == "media":
                    try:
                        for mount in top.iterdir():
                            if mount.is_dir() and _has_visible_png(mount):
                                candidates.append(mount)
                    except (PermissionError, OSError):
                        continue
                else:
                    if _has_visible_png(top):
                        candidates.append(top)
    return candidates


def _collect_stickers(root: Path, prefix: str = "") -> List[str]:
    """Recursively collect non-hidden PNG paths relative to root, sorted."""
    results: List[str] = []
    try:
        pngs = sorted(p for p in root.rglob("*.png") if p.is_file())
    except (PermissionError, OSError):
        return results
    for p in pngs:
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        results.append(f"{prefix}{rel.as_posix()}" if prefix else rel.as_posix())
    return results


def _resolve_sticker_path(name: str) -> Optional[Path]:
    """Resolve a sticker name (possibly usb/<label>/... prefixed) to an absolute Path."""
    if name.startswith("usb/"):
        if not _cfg["usb"]:
            return None
        # usb/<label>/<relative>  →  find the matching USB mount
        parts = name.split("/", 2)
        if len(parts) < 3:
            return None
        label, rel = parts[1], parts[2]
        for usb_dir in _usb_sticker_dirs():
            if usb_dir.name == label:
                candidate = (usb_dir / rel).resolve()
                if candidate.is_relative_to(usb_dir.resolve()):
                    return candidate
        return None
    candidate = (_cfg["stickers_dir"] / name).resolve()
    if not candidate.is_relative_to(_cfg["stickers_dir"].resolve()):
        return None
    return candidate


@app.get("/api/usb/generation")
async def usb_generation():
    """Returns a counter that increments whenever USB media is plugged/unplugged."""
    with _usb_gen_lock:
        return {"generation": _usb_generation}


@app.get("/api/stickers")
async def list_stickers():
    stickers_dir: Path = _cfg["stickers_dir"]
    files = _collect_stickers(stickers_dir)

    # Append stickers from any mounted USB media, prefixed with "usb/<label>/".
    if _cfg["usb"]:
        for usb_dir in _usb_sticker_dirs():
            label = usb_dir.name
            files += _collect_stickers(usb_dir, prefix=f"usb/{label}/")

    return {"stickers": files}


# ---------------------------------------------------------------------------
# Routes: background image listing
# ---------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def _is_png(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == _PNG_MAGIC
    except OSError:
        return False

@app.get("/api/sticker/{name:path}")
async def serve_sticker(name: str):
    """Serve any sticker image by name, including USB and subfolder paths."""
    path = _resolve_sticker_path(name)
    if path is None or not path.is_file() or path.suffix.lower() != ".png" or not _is_png(path):
        raise HTTPException(404, f"Sticker not found: {name}")
    return FileResponse(str(path))


@app.get("/api/backgrounds")
async def list_backgrounds():
    """List available background images (JPG/PNG) from the backgrounds directory."""
    bg_dir: Path = _cfg["backgrounds_dir"]
    if not bg_dir.is_dir():
        return {"backgrounds": []}
    files = sorted(
        p.name for p in bg_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
    )
    return {"backgrounds": files}


# ---------------------------------------------------------------------------
# Routes: canvas operations
# ---------------------------------------------------------------------------
@app.get("/api/canvas/state")
async def canvas_state():
    return _get_canvas().status()


@app.get("/api/canvas/preview.plt")
async def canvas_preview_plt():
    canvas = _get_canvas()
    with canvas._lock:
        result = canvas._result
        if result is None:
            raise HTTPException(404, "Canvas is empty — add stickers first")
        plt = result.cut_plt
    return Response(
        content=plt.encode(),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=layout_cut.plt"},
    )


@app.get("/api/canvas/preview.svg")
async def canvas_preview_svg(inline: bool = False):
    canvas = _get_canvas()
    with canvas._lock:
        result = canvas._result
        if result is None:
            raise HTTPException(404, "Canvas is empty — add stickers first")
        svg = result.cut_svg
    headers = {} if inline else {"Content-Disposition": "attachment; filename=layout_cut.svg"}
    return Response(content=svg, media_type="image/svg+xml", headers=headers)


@app.get("/api/canvas/preview.jpg")
async def canvas_preview(download: bool = False):
    import asyncio
    try:
        jpg = await asyncio.to_thread(_get_canvas().preview_jpeg)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    headers = {"Content-Disposition": "attachment; filename=layout.jpg"} if download else {}
    return Response(content=jpg, media_type="image/jpeg", headers=headers)


@app.post("/api/canvas/add")
async def canvas_add(req: CanvasAddRequest):
    import asyncio
    path = _resolve_sticker_path(req.name)
    if path is None or not path.is_file() or path.suffix.lower() != ".png" or not _is_png(path):
        raise HTTPException(404, f"Sticker not found: {req.name}")
    try:
        state = await asyncio.to_thread(_get_canvas().add, path, req.count, req.scale, req.name)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return state


@app.post("/api/canvas/remove")
async def canvas_remove(req: CanvasRemoveRequest):
    import asyncio
    path = _resolve_sticker_path(req.name)
    if path is None:
        raise HTTPException(404, f"Sticker not found: {req.name}")
    state = await asyncio.to_thread(_get_canvas().remove, path)
    return state


@app.post("/api/canvas/set_count")
async def canvas_set_count(req: CanvasSetCountRequest):
    import asyncio
    path = _resolve_sticker_path(req.name)
    if (path is None or not path.exists()) and req.count > 0:
        raise HTTPException(404, f"Sticker not found: {req.name}")
    try:
        state = await asyncio.to_thread(_get_canvas().set_count, path, req.count)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return state


@app.post("/api/canvas/set_scale")
async def canvas_set_scale(req: CanvasSetScaleRequest):
    import asyncio
    if req.scale < 0.1 or req.scale > 4.0:
        raise HTTPException(400, "scale must be between 0.1 and 4.0")
    path = _resolve_sticker_path(req.name)
    if path is None:
        raise HTTPException(404, f"Sticker not found: {req.name}")
    try:
        state = await asyncio.to_thread(_get_canvas().set_scale, path, req.scale)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return state


@app.post("/api/canvas/clear")
async def canvas_clear():
    _get_canvas().clear()
    return _get_canvas().status()


# ---------------------------------------------------------------------------
# Routes: settings (knife pressure, etc.)
# ---------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings():
    c = _get_canvas()
    bg = _cfg.get("bg_image")
    return {
        "kp": c._kp,
        "left_margin_mm": c._left_margin_mm,
        "margin_mm": c._margin_mm,
        "padding_mm": c._padding_mm,
        "perf_cut": c._perf_cut,
        "perf_kp": c._perf_kp,
        "perf_dash_mm": c._perf_dash_mm,
        "perf_gap_mm": c._perf_gap_mm,
        "bg_image": bg.name if isinstance(bg, Path) else None,
    }


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    import asyncio
    canvas = _get_canvas()
    state = None
    if req.kp is not None:
        if req.kp < 1 or req.kp > 100:
            raise HTTPException(400, "kp must be between 1 and 100")
        state = await asyncio.to_thread(canvas.set_kp, req.kp)
    if req.margin_mm is not None:
        if req.margin_mm < 0 or req.margin_mm > 20:
            raise HTTPException(400, "margin_mm must be between 0 and 20")
        state = await asyncio.to_thread(canvas.set_margin, req.margin_mm)
    if req.padding_mm is not None:
        if req.padding_mm < 0 or req.padding_mm > 20:
            raise HTTPException(400, "padding_mm must be between 0 and 20")
        state = await asyncio.to_thread(canvas.set_padding, req.padding_mm)
    if req.left_margin_mm is not None:
        if req.left_margin_mm < 0 or req.left_margin_mm > 20:
            raise HTTPException(400, "left_margin_mm must be between 0 and 20")
        state = await asyncio.to_thread(canvas.set_left_margin, req.left_margin_mm)
    if req.perf_cut is not None:
        state = await asyncio.to_thread(canvas.set_perf_cut, req.perf_cut)
    if req.perf_kp is not None:
        if req.perf_kp < 1 or req.perf_kp > 100:
            raise HTTPException(400, "perf_kp must be between 1 and 100")
        state = await asyncio.to_thread(canvas.set_perf_kp, req.perf_kp)
    if req.perf_dash_mm is not None:
        if req.perf_dash_mm < 0.1 or req.perf_dash_mm > 20:
            raise HTTPException(400, "perf_dash_mm must be between 0.1 and 20")
        state = await asyncio.to_thread(canvas.set_perf_dash, req.perf_dash_mm)
    if req.perf_gap_mm is not None:
        if req.perf_gap_mm < 0.01 or req.perf_gap_mm > 5:
            raise HTTPException(400, "perf_gap_mm must be between 0.01 and 5")
        state = await asyncio.to_thread(canvas.set_perf_gap, req.perf_gap_mm)
    if req.bg_image is not None:
        if req.bg_image == "":
            _cfg["bg_image"] = None
            state = await asyncio.to_thread(canvas.set_bg_image, None)
        else:
            bg_path = (_cfg["backgrounds_dir"] / req.bg_image).resolve()
            if not bg_path.is_relative_to(_cfg["backgrounds_dir"]) or not bg_path.is_file():
                raise HTTPException(404, f"Background not found: {req.bg_image}")
            _cfg["bg_image"] = bg_path
            state = await asyncio.to_thread(canvas.set_bg_image, bg_path)
    result = state or canvas.status()
    _save_config(canvas)
    return result


# ---------------------------------------------------------------------------
# Routes: print job
# ---------------------------------------------------------------------------
@app.post("/api/print/start")
async def print_start(request: Request):
    import asyncio
    global _print_thread

    _require_api_key(request)

    with _print_lock:
        if _print_state["status"] == "printing":
            raise HTTPException(409, "A print job is already in progress")
        if _get_canvas().is_empty:
            raise HTTPException(400, "Canvas is empty — add stickers before printing")

    # Finalize canvas (blocking image work) before starting the USB thread.
    try:
        jpg_bytes, plt_bytes, _ = await asyncio.to_thread(_get_canvas().finalize)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    with _print_lock:
        _print_state.update({
            "status": "printing",
            "message": "Connecting to printer…",
            "error": None,
            "job_id": None,
            "request_id": _new_request_id(),
        })
        _print_thread = threading.Thread(
            target=_run_print_job,
            args=(jpg_bytes, plt_bytes),
            daemon=True,
        )
        _print_thread.start()

        request_id = _print_state["request_id"]

    return {"status": "started", "request_id": request_id}


@app.post("/api/print/png")
async def print_png_sheet(
    request: Request,
    margin_mm: float = Query(0.0, ge=0.0, le=20.0),
    kp: Optional[int] = Query(None, ge=1, le=100),
    infer_border_mm: float = Query(3.0, ge=0.0, le=20.0),
    infer_threshold: int = Query(24, ge=1, le=255),
    ignore_bottom_mm: float = Query(0.0, ge=0.0, le=177.8),
):
    """Accept one pre-laid PNG sheet and start a print+cut job.

    The request body must be raw image/png. If alpha contains a useful cut mask
    it is used; otherwise cut paths are inferred from flattened RGB artwork.
    """
    import asyncio
    global _print_thread

    _require_api_key(request)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "image/png":
        raise HTTPException(415, "Content-Type must be image/png")

    with _print_lock:
        if _print_state["status"] == "printing":
            raise HTTPException(409, "A print job is already in progress")

    png_bytes = await request.body()
    if not png_bytes.startswith(_PNG_MAGIC):
        raise HTTPException(400, "Request body is not a PNG file")

    def _prepare_png_job():
        from pixcut.image_to_cut import process_sheet_png

        tmp_png: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png_bytes)
                tmp_png = Path(f.name)
            return process_sheet_png(
                tmp_png,
                dpi=_cfg["dpi"],
                margin_mm=margin_mm,
                kp=kp if kp is not None else _cfg["kp"],
                infer_border_mm=infer_border_mm,
                infer_threshold=infer_threshold,
                ignore_bottom_mm=ignore_bottom_mm,
            )
        finally:
            if tmp_png:
                try:
                    tmp_png.unlink()
                except Exception:
                    pass

    try:
        result = await asyncio.to_thread(_prepare_png_job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ImportError as exc:
        raise HTTPException(500, str(exc))

    with _print_lock:
        if _print_state["status"] == "printing":
            raise HTTPException(409, "A print job is already in progress")
        _print_state.update({
            "status": "printing",
            "message": "Connecting to printer...",
            "error": None,
            "job_id": None,
            "request_id": _new_request_id(),
        })
        _print_thread = threading.Thread(
            target=_run_print_job,
            args=(result.jpg_bytes, result.plt_bytes),
            daemon=True,
        )
        _print_thread.start()
        request_id = _print_state["request_id"]

    return {
        "status": "started",
        "request_id": request_id,
        "contours": result.contour_count,
        "mask_source": result.mask_source,
    }


def _run_print_job(jpg_bytes: bytes, plt_bytes: bytes) -> None:
    """Run a print+cut job in a background thread, updating _print_state."""
    tmp_jpg: Optional[Path] = None
    tmp_plt: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(jpg_bytes)
            tmp_jpg = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".plt", delete=False) as f:
            f.write(plt_bytes)
            tmp_plt = Path(f.name)

        from pixcut.transport import USBConfig, USBTransport, discover_pixcut
        from pixcut.orchestrator import JobConfig, run_job_session
        from pixcut.logging_utils import SessionLogger

        if _cfg["auto_detect"]:
            cfg = discover_pixcut(vid_hint=_cfg.get("vid"), pid_hint=_cfg.get("pid"))
        else:
            cfg = USBConfig(
                vid=_cfg["vid"], pid=_cfg["pid"],
                interface=2, out_ep=0x06, in_ep=0x86,
                data_interface=3, data_out_ep=0x04, data_in_ep=0x84,
            )
        transport = USBTransport(cfg)
        logger = SessionLogger(None, keep_json=False)

        with _print_lock:
            _print_state["message"] = "Uploading to printer…"

        def _status_cb(msg: str) -> None:
            with _print_lock:
                _print_state["message"] = msg

        result = run_job_session(
            transport=transport,
            logger=logger,
            jpg_path=tmp_jpg,
            plt_path=tmp_plt,
            job_cfg=JobConfig(),
            mode="combo",
            status_callback=_status_cb,
        )

        if result.get("error"):
            from pixcut.orchestrator import _describe_alerts
            alerts = result.get("alerts")
            desc = _describe_alerts(alerts) if alerts else result.get("error", "Unknown error")
            with _print_lock:
                _print_state.update({"status": "error", "message": desc, "error": result["error"]})
        else:
            with _print_lock:
                _print_state.update({
                    "status": "done",
                    "message": "Print & cut complete!",
                    "job_id": result.get("job_id"),
                    "error": None,
                })

    except RuntimeError as exc:
        with _print_lock:
            _print_state.update({"status": "error", "message": str(exc), "error": str(exc)})
    except Exception as exc:
        log.exception("print job failed unexpectedly")
        with _print_lock:
            _print_state.update({"status": "error", "message": f"Unexpected error: {exc}", "error": str(exc)})
    finally:
        for p in (tmp_jpg, tmp_plt):
            if p:
                try:
                    p.unlink()
                except Exception:
                    pass


@app.get("/api/print/status")
async def print_status(request: Request, request_id: Optional[str] = None):
    _require_api_key(request)
    with _print_lock:
        state = dict(_print_state)
    if request_id and state.get("request_id") != request_id:
        raise HTTPException(404, "Print job not found")
    return state


@app.post("/api/print/reset")
async def print_reset(request: Request):
    """Reset print state to idle after done/error so a new job can be started."""
    _require_api_key(request)
    with _print_lock:
        if _print_state["status"] == "printing":
            raise HTTPException(409, "Cannot reset while a job is in progress")
        _print_state.update({
            "status": "idle",
            "message": "",
            "error": None,
            "job_id": None,
            "request_id": None,
        })
    return {"status": "idle"}


# ---------------------------------------------------------------------------
# Static file mounts (order matters: specific mounts before catch-all)
# ---------------------------------------------------------------------------
app.mount("/stickers", StaticFiles(directory="stickers"), name="stickers")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# Config file loader
# ---------------------------------------------------------------------------

# Maps server.json keys to (argparse dest, transform).
# Boolean flags stored as positive booleans in JSON; inverted for store_true args.
_CONFIG_SCALARS = {
    "host":          "host",
    "port":          "port",
    "stickers":      "stickers",
    "dpi":           "dpi",
    "margin_mm":     "margin",
    "padding_mm":    "padding",
    "left_margin_mm": "left_margin",
    "kp":            "kp",
    "vid":           "vid",
    "pid":           "pid",
    "backgrounds":   "backgrounds",
    "api_key":       "api_key",
}
_CONFIG_BOOLS = {
    "usb":         "no_usb",        # usb: false  → --no-usb
    "auto_detect": "no_auto_detect", # auto_detect: false → --no-auto-detect
}


def _load_config(path: Path) -> dict:
    import json
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        log.info("Loaded config from %s", path)
        return data
    except Exception as exc:
        log.warning("Could not load config file %s: %s", path, exc)
        return {}


def _apply_config_defaults(parser: argparse.ArgumentParser, file_cfg: dict) -> None:
    """Push config-file values into parser defaults so CLI args still win."""
    defaults: dict = {}
    for cfg_key, dest in _CONFIG_SCALARS.items():
        if cfg_key in file_cfg:
            defaults[dest] = file_cfg[cfg_key]
    for cfg_key, flag_dest in _CONFIG_BOOLS.items():
        if cfg_key in file_cfg:
            defaults[flag_dest] = not file_cfg[cfg_key]
    if defaults:
        parser.set_defaults(**defaults)




def _save_config(canvas) -> None:
    """Persist current admin-panel settings back to server.json."""
    import json
    path: Optional[Path] = _cfg.get("config_path")
    if not path:
        return
    existing: dict = {}
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            pass
    bg = canvas._bg_image_path
    existing.update({
        "kp":             canvas._kp,
        "margin_mm":      canvas._margin_mm,
        "padding_mm":     canvas._padding_mm,
        "left_margin_mm": canvas._left_margin_mm,
        "perf_cut":       canvas._perf_cut,
        "perf_kp":        canvas._perf_kp,
        "perf_dash_mm":   canvas._perf_dash_mm,
        "perf_gap_mm":    canvas._perf_gap_mm,
        "bg_image":       bg.name if isinstance(bg, Path) else None,
    })
    try:
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
        log.info("Config saved to %s", path)
    except Exception as exc:
        log.warning("Could not save config to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    # Pre-parse just to find --config before building the real parser.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="server.json", help="Path to config file")
    pre_args, _ = pre.parse_known_args()

    parser = argparse.ArgumentParser(description="PixCut Kiosk UI Server")
    parser.add_argument("--config", default="server.json", help="Path to JSON config file (default: server.json)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--stickers", default="stickers", help="Path to stickers directory")
    parser.add_argument("--dpi", type=int, default=300, help="Layout DPI (default: 300)")
    parser.add_argument("--margin", type=float, default=1.0, metavar="MM", help="Cut margin in mm (default: 1.0)")
    parser.add_argument("--padding", type=float, default=2.0, metavar="MM", help="Gap between stickers in mm (default: 2.0)")
    parser.add_argument("--kp", type=int, default=42, help="Knife pressure (default: 42)")
    parser.add_argument("--left-margin", type=float, default=3.0, metavar="MM", help="Extra left paper margin in mm (default: 3.0)")
    parser.add_argument("--no-usb", action="store_true", help="Disable USB drive sticker scanning")
    parser.add_argument("--no-auto-detect", action="store_true", help="Disable USB auto-detect")
    parser.add_argument("--vid", default=None, help="USB Vendor ID hex (e.g. 0x302C)")
    parser.add_argument("--pid", default=None, help="USB Product ID hex (e.g. 0x3101)")
    parser.add_argument("--backgrounds", default="backgrounds", help="Path to backgrounds directory")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("PIXCUT_API_KEY"),
        help="Require this API key for /api/print/* routes (or set PIXCUT_API_KEY)",
    )

    config_path = Path(pre_args.config)
    file_cfg = _load_config(config_path)
    _apply_config_defaults(parser, file_cfg)

    args = parser.parse_args()

    _cfg["config_path"] = config_path
    _cfg["stickers_dir"] = Path(args.stickers)
    _cfg["dpi"] = args.dpi
    _cfg["margin_mm"] = args.margin
    _cfg["padding_mm"] = args.padding
    _cfg["kp"] = args.kp
    _cfg["left_margin_mm"] = args.left_margin
    _cfg["usb"] = not args.no_usb
    _cfg["auto_detect"] = not args.no_auto_detect
    _cfg["vid"] = int(args.vid, 16) if args.vid else None
    _cfg["pid"] = int(args.pid, 16) if args.pid else None
    _cfg["backgrounds_dir"] = Path(args.backgrounds).resolve()
    _cfg["backgrounds_dir"].mkdir(parents=True, exist_ok=True)
    _cfg["api_key"] = args.api_key

    # Load admin-only settings (no CLI equivalent) from config file.
    for key in ("perf_cut", "perf_kp", "perf_dash_mm", "perf_gap_mm"):
        if key in file_cfg:
            _cfg[key] = file_cfg[key]
    if file_cfg.get("bg_image"):
        bg_candidate = _cfg["backgrounds_dir"] / file_cfg["bg_image"]
        if bg_candidate.is_file():
            _cfg["bg_image"] = bg_candidate

    app.mount("/backgrounds", StaticFiles(directory=str(_cfg["backgrounds_dir"])), name="backgrounds")

    threading.Thread(target=_usb_monitor_thread, daemon=True, name="usb-monitor").start()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("PixCut Kiosk starting on http://%s:%d", args.host, args.port)
    log.info("Stickers dir: %s", _cfg["stickers_dir"].resolve())
    log.info("Backgrounds dir: %s", _cfg["backgrounds_dir"].resolve())
    log.info("Print API key auth: %s", "enabled" if _cfg["api_key"] else "disabled")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
