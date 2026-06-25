# PixCut CLI + Kiosk

Python toolset to control the Liene PixCut S1 over USB bulk endpoints.

![PixCut CLI](docs/cli.png)

## Overview

This repo contains three independent but related tools:

- **[PixCut CLI](#pixcut-cli)** (`pixcut_cli.py` + `pixcut/`) — command-line tool for sending print/cut jobs, auto-generating cut paths, and inspecting the printer over USB. This is the core; everything else builds on it.
- **[pixcut-kiosk](#pixcut-kiosk-web-ui-server)** (`server.py` + `static/`) — a local web UI that wraps the CLI into a point-and-click sticker builder. No command line needed during a session.
- **[Raspberry Pi deploy](#raspberry-pi-kiosk-deployment)** (`deploy.sh`, `deploy/99-pixcut.rules`, `deploy/pixcut-kiosk.service`) — automated script to sync and configure the kiosk on a Pi over SSH, including udev rules and a systemd service.

If you just want to drive the printer from a Mac or PC, you only need the CLI. The kiosk and deploy script are for a dedicated touchscreen kiosk setup.

---

## Install

### macOS

```bash
brew install libusb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-server.txt   # optional, to run the GUI/kiosk server on this machine
```

### Linux / Raspberry Pi

```bash
sudo apt-get install -y libusb-1.0-0
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-server.txt   # optional, to run the GUI/kiosk server on this machine
```

For non-root USB access, install the included udev rule (required unless you run as root):

```bash
sudo cp deploy/99-pixcut.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev $USER   # log out and back in after this
```

#### Deploy for Raspberry Pi

A fully automated deployment script is included for kiosk deployment to Raspberry Pi OS — see [Raspberry Pi Kiosk Deployment](#raspberry-pi-kiosk-deployment) below.

### Windows

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-server.txt   # optional, to run the GUI/kiosk server on this machine
```

`libusb-package` (included in `requirements.txt`) bundles the USB backend automatically for most Python versions — no extra steps needed. If you see **"USB backend not found"**, see [Windows USB troubleshooting](#windows-usb-troubleshooting) below.

### Windows USB troubleshooting

`libusb-package` has a release lag — newly released Python versions may not yet have a bundled-DLL wheel, so `pip install` succeeds but the USB backend is missing. If you see **"USB backend not found"**:

1. Download the latest Windows binary from [github.com/libusb/libusb/releases](https://github.com/libusb/libusb/releases) — get the `.7z` archive (requires [7-Zip](https://www.7-zip.org/) to open).
2. Extract the DLL matching your Python installation:

   | Python | DLL path inside the archive |
   | --- | --- |
   | 64-bit (most common) | `VS2022\MS64\dll\libusb-1.0.dll` |
   | 32-bit | `VS2022\MS32\dll\libusb-1.0.dll` |
   | ARM64 | `VS2022\ARM64\dll\libusb-1.0.dll` |

   Not sure which you have? Run: `python -c "import struct; print(struct.calcsize('P')*8, 'bit')"`

3. Place `libusb-1.0.dll` in the `pixcut\` folder inside this project.

Once `libusb-package` ships a wheel for your Python version, a fresh `pip install -r requirements.txt` will pick it up and you can remove the manual DLL.

**Zadig note:** If you have never installed Liene's official software, Windows has no driver bound to the device and libusb claims it automatically — no extra steps needed. If you have the Liene app installed, you may need [Zadig](https://zadig.akeo.ie/) to rebind the device driver to WinUSB.

## Example files

The repo includes a sample sticker (`meow.jpg` + `meow.svg`) and layout templates to help you get started:

|File|Description|
|---|---|
|`examples/meow.jpg`|Sample print image — a die-cut cat sticker at 300 DPI|
|`examples/meow.svg`|Matching hand-drawn cut paths for `meow.jpg`|
|`examples/sticker-sheet-template.af`|Affinity Studio template showing a full 4×7″ sticker sheet layout|
|`examples/sticker-sheet-template.pdf`|PDF version of the template (for Inkscape/Illustrator/other applications)|

### Try a combo print+cut with the sample sticker

```bash
python3 pixcut_cli.py send --mode combo --jpg examples/meow.jpg --svg examples/meow.svg
```

### Auto-generate cut paths with `layout`

If you have a PNG/JPG sticker image without a pre-drawn cut path, `layout` traces one automatically:

```bash
python3 pixcut_cli.py layout examples/meow.jpg --repeat 4 --out-dir ./output
python3 pixcut_cli.py send --mode combo --jpg output/layout.jpg --plt output/layout.plt
```

### Sticker sheet template

Open `examples/sticker-sheet-template.af` (Affinity Designer) or `examples/sticker-sheet-template.pdf` to see how to lay out artwork on the 4×7″ canvas. The template has separate layers for the SVG cut paths and the art. The art should be saved as a .jpg file, 1200×2100 px, and must be under 1 MiB in size; the SVG layer should be exported as simple paths without fills or any other art.

---

## PixCut CLI

**Files:** `pixcut_cli.py`, `pixcut/`

- `send`    — run a print-only or combo (print+cut) job.
- `layout`  — auto-trace cut paths from PNG/JPG sticker images and export a print-ready sheet (no USB).
- `convert` — offline SVG→PLT converter (no USB).
- `query`   — send a single JSON request (get-prop, get-job-info, etc.).
- `probe`   — batched property sweep + optional experimental methods; optional `set-prop` (guarded).
- `printer` — send `pause-printer` or `resume-printer`.
- `scan`    — list visible USB devices, highlighting PixCut devices.

All commands share USB flags: `--vid/--pid --interface --out-ep --in-ep --data-interface --data-out-ep --data-in-ep --timeout-ms --auto-detect/--no-auto-detect`.
Use `--verbose` to enable per-session file logging (see [Logging](#logging) below).

Typical working endpoints (from captures): control JSON on interface **2** OUT `0x06` IN `0x86`; data on interface **3** OUT `0x04` IN `0x84`.

### Logging

By default no files are written — output goes to the console only. Pass `--verbose` to capture a full session directory under `run-logs/session-<timestamp>/`:

- JSONL: `requests_sent.jsonl`, `responses_seen.jsonl`, `requests_and_responses.jsonl`
- Raw frames: `raw/*.bin`
- Any converted PLT files (when using `--svg`)

---

### send

```zsh
python3 pixcut_cli.py send \
  --mode combo \
  --jpg photo.jpg \
  --plt path.plt \
  --media-size 5313 --media-type 2030 \
  --copies 1 --quality 4 \
  --channel 14864 \
  --kp 42
```

Modes:

- `--mode combo` (default): requires `--jpg` and `--plt` (or `--svg` to convert to PLT).
- `--mode print`: requires `--jpg`; sends a photo-only job (no cutting).

Key options:

- `--svg` auto-converts SVG → PLT and uploads the generated PLT. Supports color-coded perf-cut — see [convert](#convert-svg--plt-offline) below.
- `--kp` kiss-cut knife pressure applied to standard paths (default 42).
- `--perf-color HEX` stroke color marking perf-cut paths in `--svg` input (default `ff8800` / orange).
- `--perf-kp N` knife pressure for perf-cut paths (default 53).
- `--perf-dash MM` perf-cut dash length in mm (default 8.0).
- `--perf-gap MM` gap between dashes in mm (default 0.05).
- `--job-type` optional; defaults to 0 for print, 600 for combo/cut.
- `--chunk-delay-ms` (default 120) pacing between data chunks.
- `--extlen` (default 4075) chunk size.
- `--ack-timeout` per-chunk ACK wait (seconds).
- `--heartbeat-interval` (default 5s) background get-prop pings.
- `--poll-interval` (default 10s) job status polling after upload.
- `--max-poll-seconds` overall poll timeout (0 = unlimited).
- `--id-strategy {monotonic,fixed}`: monotonic (default) uses ever-increasing ids; fixed mimics captured ids.
- `--interface`: If auto-detect fails, set endpoints explicitly, e.g. `--interface 2 --out-ep 0x06 --in-ep 0x86 --data-interface 3 --data-out-ep 0x04 --data-in-ep 0x84`.

Constraints:

- JPG must be ≤ 1 MiB (device limit).

---

### layout (auto-cutlines from PNG/JPG)

Traces cut paths from raster sticker images and packs them onto the 4×7″ canvas using a Maximal Rectangles packer (fills gaps beside tall stickers — much better than shelf-only packing). Requires `Pillow` and `scikit-image`.

```zsh
# Single sticker, 3 copies
python3 pixcut_cli.py layout sticker.png --repeat 3 --out-dir ./output

# Multiple stickers, paginate overflow onto additional sheets
python3 pixcut_cli.py layout s1.png s2.png s3.png --paginate --out-dir ./output

# JPEG / PNG without alpha
python3 pixcut_cli.py layout photo.jpg --bg-white --out-dir ./output

# With perf-cut (pop-out outer dashed lines at higher KP)
python3 pixcut_cli.py layout sticker.png --perf-cut --perf-kp 53 --perf-dash 8 --out-dir ./output
```

Outputs in `--out-dir`:

|File|Description|
|---|---|
|`layout.jpg`|Composite sticker sheet — pass to `send --jpg`|
|`layout.plt`|Cut paths (kiss-cut + optional perf-cut) — pass to `send --plt`|
|`layout_cut.svg`|SVG preview: kiss-cut in red, perf-cut in orange dashed|

With `--paginate`, overflow sheets are written as `layout_1.*`, `layout_2.*`, etc.

|Flag|Default|Description|
|---|---|---|
|`--dpi N`|300|Output resolution. A 300×300 px image at 300 DPI = 1×1 inch sticker.|
|`--margin MM`|2.0|Outward offset of cut path from sticker edge in mm.|
|`--padding MM`|3.0|Gap between sticker footprints on canvas in mm.|
|`--left-margin MM`|0.0|Extra left paper margin in mm (shifts all cuts away from left edge).|
|`--kp N`|42|Knife pressure for kiss-cut PLT output.|
|`--repeat N`|1|Place each input image N times.|
|`--bg-white`|—|Treat white pixels as transparent (JPEGs / white-bg PNGs).|
|`--paginate`|—|Create extra sheets for overflow images.|
|`--perf-cut`|—|Add dashed outer perf-cut contours at higher KP.|
|`--perf-kp N`|53|Knife pressure for perf-cut lines.|
|`--perf-dash MM`|8.0|Dash length in mm.|
|`--perf-gap MM`|0.05|Gap between dashes in mm.|

Then send to the printer:

```zsh
python3 pixcut_cli.py send --jpg output/layout.jpg --plt output/layout.plt
```

#### Perf-cut explained

A perf-cut scores the backing paper in a dashed pattern along the kiss-cut line, letting stickers pop out cleanly by hand. The perf-cut runs at a higher knife pressure in alternating short bursts — the cut segments score through the backing while the gaps leave bridges that hold the sheet together. Liene does not support perf-cut in their official software, but the hardware is capable of it — this CLI generates the necessary PLT paths.

- **Recommended settings**: `--perf-kp 53 --perf-dash 8 --perf-gap 0.05`
- Verify with the SVG preview at actual size before cutting — kiss-cut contours appear in red.
- Perf-cut scores through the backing paper, which will wear the cutting strip under the blade over time. That strip is not officially user-replaceable on the PixCut S1, but it can be replaced with a similarly-sized 8mm cutting strip (such as those sold for Graphtec/Roland cutters).

---

### convert (SVG → PLT offline)

Converts SVG vector paths into the PixCut's HPGL-like PLT format. Supports color-coded perf-cut paths in the same SVG file.

```zsh
python3 pixcut_cli.py convert --svg input.svg --out output.plt --kp 42
```

Draw your kiss-cut paths in any color and your perf-cut paths with stroke color **`#ff8800`** (orange). The converter automatically separates them:

- Kiss-cut paths → PLT at `--kp` (default 42)
- Orange paths → PLT at `--perf-kp` (default 60) with dashed segments

```zsh
python3 pixcut_cli.py convert \
  --svg my_design.svg \
  --kp 42 \
  --perf-kp 53 \
  --perf-dash 8 \
  --perf-gap 0.05
```

The same perf-cut flags work on `send --svg`.

|Flag|Default|Description|
|---|---|---|
|`--kp N`|42|Kiss-cut knife pressure|
|`--perf-color HEX`|`ff8800`|Stroke hex color marking perf-cut paths (set to `""` to disable)|
|`--perf-kp N`|53|Perf-cut knife pressure|
|`--perf-dash MM`|8.0|Dash length in mm|
|`--perf-gap MM`|0.05|Gap between dashes in mm|

Defaults (not user-tuned): DPI 96, units/inch 1016, rotate −90°, target 4×7 in. Translation offsets `--tx/--ty` available for manual nudging.

---

### query

Ad-hoc single request.

- Props: `python3 pixcut_cli.py query --props printer-state printer-sub-state`
- Identity bundle: `--identity`
- Job info: `--job-id 54`
- Custom: `--method get-prop --params '["big-data"]'` — on Windows cmd.exe use double quotes: `--params "[\"big-data\"]"` (PowerShell accepts single quotes as-is)
- Repeat: `--repeat 5 --interval 2.0`

---

### probe (experimental/read-only by default)

Sweeps common properties in batches and logs responses.

```zsh
python3 pixcut_cli.py probe
python3 pixcut_cli.py probe --experimental        # add broader/less certain props
python3 pixcut_cli.py probe --methods get-job-info --job-id 54
```

`--dangerous --set-prop key=value [...]` to send a `set-prop` mutation (example: `auto-off-interval=600`).

---

### printer (pause/resume)

```zsh
python3 pixcut_cli.py printer --pause
python3 pixcut_cli.py printer --resume
```

---

### scan

```zsh
python3 pixcut_cli.py scan
```

---

### Progress & monitoring

- Uploads log per-chunk progress with bytes/percent for PLT and JPG.
- Poll loop logs concise state lines: job/printer state, print page, cut progress %, transfer %.
- Final `big-data` fetched after completion.

---

## pixcut-kiosk (Web UI Server)

**Files:** `server.py`, `static/`

An optional local web server for building sticker sheets interactively — no command line required during a session. The CLI remains fully independent.

**Platform note:** The kiosk is designed for **Raspberry Pi 4 or newer** running Raspberry Pi OS. A Pi 3B will struggle — Firefox is slow and Chromium won't launch on current Raspberry Pi OS on Pi 3B.

```bash
python server.py
# open http://localhost:8000 in a browser
```

![Kiosk UI](docs/screenshot.png)

Two-panel layout:

- **Left** — live JPEG preview of the 4×7″ canvas, updated after every change.
- **Right** — scrollable sticker grid loaded from the `stickers/` folder. Click a sticker to add it; use +/− to set quantity; drag the slider (25%–200%) to resize.

Controls:

- **Clear Canvas** — remove all stickers.
- **Print & Cut** — finalise the layout, send to the printer, and display a live status overlay (polling every 1.5 s).
- **Overflow banner** — appears when stickers don't fit; reduce count or size.

### PNG print endpoint

For web apps that generate the final sheet upstream, start the server on the
machine connected to the PixCut and POST a raw PNG body:

```bash
export PIXCUT_API_KEY="replace-with-a-long-random-secret"
python server.py --host 0.0.0.0 --port 8000

curl -X POST \
  -H "Content-Type: image/png" \
  -H "X-API-Key: replace-with-a-long-random-secret" \
  --data-binary @sheet.png \
  "http://localhost:8000/api/print/png"
```

The print response includes a server-side `request_id`:

```json
{
  "status": "started",
  "request_id": "Jq7U0h9kMiXc1myA",
  "contours": 7,
  "mask_source": "inferred-rgb"
}
```

Poll that job until `status` is `done` or `error`:

```bash
curl \
  -H "X-API-Key: replace-with-a-long-random-secret" \
  "http://localhost:8000/api/print/status?request_id=Jq7U0h9kMiXc1myA"
```

`/api/print/png` accepts only PNG input. Internally it converts the sheet to the
printer's JPEG format and creates PLT cut paths. If the PNG has a useful alpha
mask, alpha is used. If the PNG is flattened/fully opaque, the API infers sticker
islands from the visible RGB artwork.

Generated PNG contract:

- Canvas must be 4:7, for example `1200x2100` at 300 DPI or `2880x5040` at 720 DPI.
- Use a white or near-white page background.
- Keep stickers visually separated; the inference expands artwork outward by `infer_border_mm`, so leave at least a few mm of empty space between stickers.
- Render flat sticker artwork. Do not bake in mockup effects such as drop shadows, glow, bevels, or lighting.
- Treat logos/branding as sticker artwork too if they should be cut out; keep them visually separated from the other stickers.
- Avoid pale, borderless sticker edges on a white background; the API needs visible contrast to find each sticker island.

Optional alpha mask:

- Alpha `255` means cuttable sticker material, including the full white border.
- Alpha `0` means transparent/non-cut background.
- Alpha `254` means visible print-only pixels, useful for page branding or a logo that should print but not be cut.

Optional query params: `margin_mm=0..20` adds an outward cut offset, `kp=1..100`
overrides knife pressure, `infer_border_mm=0..20` controls how far flattened
artwork is grown into a cut shape, `infer_threshold=1..255` controls background
sensitivity, and `ignore_bottom_mm=0..177.8` can reserve a print-only bottom
band if you ever need one. The endpoint defaults to `ignore_bottom_mm=0`, so
all visible artwork can become stickers.

API key auth:

- Set `PIXCUT_API_KEY`, pass `--api-key`, or set `api_key` in `server.json`.
- When configured, all `/api/print/*` routes require either `X-API-Key: ...` or `Authorization: Bearer ...`.
- If the Lovable app is frontend-only, this key is visible to users. Prefer calling PixCut from a server-side Lovable action/proxy, Cloudflare Worker, or other backend that keeps the key secret.
- Use HTTPS when exposing beyond your LAN; a tunnel such as Cloudflare Tunnel, Tailscale Funnel, ngrok, or a reverse proxy can terminate TLS and forward to `http://127.0.0.1:8000`.

Lovable/browser fetch sketch:

```js
const printRes = await fetch(`${PIXCUT_BASE_URL}/api/print/png`, {
  method: "POST",
  headers: {
    "Content-Type": "image/png",
    "X-API-Key": PIXCUT_API_KEY,
  },
  body: pngBlob,
});
const job = await printRes.json();

const statusRes = await fetch(
  `${PIXCUT_BASE_URL}/api/print/status?request_id=${encodeURIComponent(job.request_id)}`,
  { headers: { "X-API-Key": PIXCUT_API_KEY } },
);
const status = await statusRes.json();
```

### Server options

```bash
python server.py [options]
```

Defaults are read from `server.json` in the project root. CLI flags always override the config file. Admin panel changes (knife pressure, margins, perf-cut settings, background image) are written back to `server.json` automatically.

`server.json` keys:

|Key|Default|Description|
|---|---|---|
|`host`|`"127.0.0.1"`|Bind address (`"0.0.0.0"` to expose on LAN)|
|`port`|`8000`|HTTP port|
|`stickers`|`"stickers"`|Path to stickers directory|
|`backgrounds`|`"backgrounds"`|Path to backgrounds directory|
|`dpi`|`300`|Layout resolution|
|`margin_mm`|`1.0`|Cut margin outside sticker edge|
|`padding_mm`|`2.0`|Gap between stickers on canvas|
|`left_margin_mm`|`3.0`|Left paper margin|
|`kp`|`42`|Knife pressure (1–100)|
|`usb`|`true`|Enable USB drive sticker scanning|
|`auto_detect`|`true`|Auto-detect printer VID/PID|
|`vid`|`null`|Explicit USB Vendor ID hex string, e.g. `"0x302C"`|
|`pid`|`null`|Explicit USB Product ID hex string, e.g. `"0x3101"`|
|`api_key`|`null`|Optional API key required for `/api/print/*` routes|
|`perf_cut`|`false`|Enable perf-cut (pop-out lines)|
|`perf_kp`|`53`|Perf-cut knife pressure|
|`perf_dash_mm`|`8.0`|Perf-cut dash length (kiss-cut bridges)|
|`perf_gap_mm`|`0.05`|Perf-cut gap length (full-cut segments)|
|`bg_image`|`null`|Background image filename (from `backgrounds/`)|

CLI flags (all correspond to the keys above):

|Flag|Description|
|---|---|
|`--config`|Path to config file (default: `server.json`)|

|`--host`|Bind address|
|`--port`|HTTP port|
|`--stickers DIR`|Stickers directory|
|`--dpi N`|Layout resolution|
|`--margin MM`|Cut margin in mm|
|`--padding MM`|Gap between stickers in mm|
|`--kp N`|Knife pressure|
|`--left-margin MM`|Left paper margin in mm|
|`--no-usb`|Disable USB drive scanning|
|`--no-auto-detect`|Disable USB auto-detect|
|`--vid / --pid`|Explicit USB VID/PID (hex)|
|`--api-key KEY`|Require an API key for `/api/print/*` routes|

### Sticker organisation

The kiosk loads PNG files from the `stickers/` directory. Subfolders appear as section headers in the grid:

```text
stickers/
  cat.png               # appears under no header (root)
  dogs/
    corgi.png           # appears under "dogs" header
    poodle.png
  2024-events/
    kernelcon.png       # appears under "2024-events" header
```

USB drives are automatically scanned for PNG files and appear under a `USB: <label>` header. The grid refreshes automatically within 5 seconds of a drive being plugged or unplugged. Supported mount roots: `/media` and `/mnt` (Linux), `/Volumes` (macOS), and External USB drives (Windows). Pass `--no-usb` to disable USB drive scanning entirely.

### Background images

Place background images (JPG or PNG) in `backgrounds/` under the project root. They are composited under the stickers in the print layer only — cut paths are unaffected.

To change the active background: tap the title **5 times** to open the admin panel, then pick from the **Background Image** dropdown.

The backgrounds directory is configurable:

```bash
python server.py --backgrounds /path/to/backgrounds
```

### Admin panel (secret: tap title 5× within 3 s)

|Setting|Default|Description|
|---|---|---|
|Knife Pressure|42|Kiss-cut KP for all sticker outlines|
|Cut Margin|1.0 mm|Outward offset of cut path from sticker edge|
|Sticker Gap|2.0 mm|Gap between sticker footprints on canvas|
|Left Paper Margin|3.0 mm|Extra margin to keep cuts off the left edge|
|Background Image|None|Print-layer background (no cut path)|
|Perf-Cut|off|Dashed scoring pattern along cut lines for easy pop-out|
|Perf-Cut KP|53|Knife pressure for perf lines|
|Dash Length|8.0 mm|Length of each perforated dash|
|Gap Length|0.05 mm|Gap between dashes (kiss-cut bridges)|

Export buttons: **JPEG Image** (print-ready composite), **SVG Cutlines** (cut path preview — kiss-cut in red), and **PLT File** (raw cutter instructions for debugging).

### Network security

The kiosk has no authentication. On untrusted networks (e.g. conference Wi-Fi) bind to localhost only:

```bash
# Bind to localhost only (edit the service file, then reload)
sudo sed -i 's/--host [0-9.]*/--host 127.0.0.1/' /etc/systemd/system/pixcut-kiosk.service
sudo systemctl daemon-reload && sudo systemctl restart pixcut-kiosk
```

To access the UI remotely when locked to localhost, SSH-tunnel it:

```bash
ssh -L 8000:localhost:8000 <PI_USER>@<PI_HOST>
# then open http://localhost:8000 in your browser
```

---

## Raspberry Pi Kiosk Deployment

**Files:** `deploy.sh`, `deploy/99-pixcut.rules`, `deploy/pixcut-kiosk.service`, `deploy/launch-kiosk.sh`

Automates syncing and configuring the kiosk on a Pi over SSH.

### First-time setup

```bash
# Defaults: host=raspberrypi.local  user=pi  (set PI_PASS env var if using sshpass)
./deploy.sh

# Override any value inline
PI_HOST=mypi.local PI_USER=pi PI_PASS=yourpassword ./deploy.sh
```

The script:

1. rsyncs the project (excluding `.venv/`, `__pycache__/`, etc.)
2. Installs system packages (`python3-venv`, `libusb-1.0-0`, `chromium`)
3. Installs udev rule + adds user to `plugdev`
4. Creates `.venv` and installs all Python dependencies
5. Installs and starts `pixcut-kiosk.service` (systemd)
6. Prompts whether to auto-launch Chromium at desktop login (see below)

After deploy: `http://<PI_HOST>:8000`

### Chromium kiosk browser

`deploy/launch-kiosk.sh` opens Chromium fullscreen pointing at the kiosk UI. The deploy script will ask:

> Auto-launch Chromium at desktop login? [y/N]

- **Y** — writes an XDG autostart entry (`~/.config/autostart/pixcut-kiosk-browser.desktop`). Chromium opens automatically whenever the desktop loads. **Only enable this on a dedicated touchscreen display** — kiosk mode is fullscreen with no browser chrome, and without a keyboard/mouse there is no way to exit.
- **N** — creates a `PixCut-Kiosk` desktop icon instead. Double-tap it to open the browser manually.

To change this after deploy, re-run `deploy.sh` and answer differently, or manage the autostart file directly:

```bash
# Remove autostart (revert to manual desktop icon)
rm ~/.config/autostart/pixcut-kiosk-browser.desktop

# Launch manually at any time
~/pixcut-app/deploy/launch-kiosk.sh
```

### Service management

```bash
ssh <PI_USER>@<PI_HOST>
sudo systemctl status pixcut-kiosk
journalctl -u pixcut-kiosk -f        # live logs
sudo systemctl restart pixcut-kiosk
```

---

## USB Protocol Reference

`docs/pixcut-usb-protocol.md` documents the reverse-engineered USB wire format — JSON control messages, bulk data framing, endpoint layout, and observed property names. Useful if you want to extend the CLI, add new commands, or port the protocol to another language.

---

## License

MIT License — see [LICENSE](LICENSE) for the full text.

No affiliation with the vendor. Use at your own risk.
