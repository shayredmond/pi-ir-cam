# Pi IR Cam

Web interface for a Raspberry Pi 4 + Camera Module 3 NoIR (Wide), built for
testing and comparing the IR output of remote-control blaster devices. Live
MJPEG view in the browser, one-click H.264/MP4 recordings, manual
exposure/gain/focus lock, and a live brightness meter over a center ROI so
device-to-device comparisons are quantitative.

Each recording is a self-documenting bundle of three files sharing a base name:

- `<base>.mp4` — the video, with a text overlay burned into the top of every
  frame: device name, wall time, elapsed, ROI mean/peak/clip %, shutter and
  gain. The overlay exists only in recordings (and stills taken while
  recording) — the live view and the brightness measurements stay clean.
- `<base>.csv` — one row per frame: timestamp, elapsed, ROI and full-frame
  luma stats, actual shutter/gain. Graph it or compute per-device averages
  without re-watching footage.
- `<base>.json` — device name, free-form scenario notes, start time, and the
  camera settings in force when the recording started.

The web UI has fields for the device under test and scenario notes; both are
shown in the recordings list, and deleting an entry removes all three files.

## Why manual exposure matters

With auto-exposure on, the camera renormalizes the scene and every blaster
looks about the same. For valid comparisons:

1. Lock exposure — easiest via **Calibrate exposure** in the UI (or
   `POST /api/calibrate`): give it the webhook URL of an HA automation that
   makes your **brightest** device transmit for ~3 s (e.g. `num_repeats: 30`).
   It measures ambient vs. burst, iterates shutter/gain until the burst peaks
   at ~200/255 with zero clipping, and locks the result. Manual alternative:
   turn **Auto exposure off** and pick a shutter/gain where the brightest
   device does **not clip** (the meter warns when >1% of ROI pixels
   saturate); start around 1–5 ms shutter, gain 1–2×.
2. Turn **Continuous autofocus off** and set a fixed lens position
   (dioptres = 100 / distance-in-cm).
3. Keep geometry fixed: same mount position, distance, and angle for every
   device. IR LEDs are directional, so angle changes swamp real differences.
4. Compare the **ROI mean luma** while each device transmits, and record a
   clip per device (label it with the device name) for later review.

Note on modulation: IR blasters modulate at ~38 kHz, far above the frame rate,
so each frame integrates thousands of carrier cycles — frame brightness is a
good proxy for average radiated power × duty cycle. Protocol bursts are
millisecond-scale though, so brightness varies frame to frame; use a repeating
transmission (or scrub the recording for peak frames) when comparing.

## Install on the Pi (Raspberry Pi OS Bookworm or later)

```bash
sudo apt update
sudo apt install -y python3-picamera2 ffmpeg python3-venv rsync
```

Copy the project over from your machine:

```bash
rsync -av --exclude .venv --exclude recordings ~/src/NabuCasa/pi-ir-cam/ pi@<pi-host>:~/pi-ir-cam/
```

On the Pi:

```bash
cd ~/pi-ir-cam
python3 -m venv --system-site-packages .venv   # system site pkgs -> picamera2 visible
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Open `http://<pi-host>:8000`. Recordings land in `./recordings/` (override
with `PI_IR_CAM_RECORDINGS=/path`).

Sanity checks if the camera doesn't come up: `rpicam-hello --list-cameras`
should list the imx708; make sure nothing else (e.g. another streamer) holds
the camera.

### Run as a service

```bash
sudo cp pi-ir-cam.service /etc/systemd/system/   # edit User/paths if not 'pi'
sudo systemctl daemon-reload
sudo systemctl enable --now pi-ir-cam
```

## Develop without a Pi (mock camera)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python server.py --mock
```

The mock backend renders a pulsing synthetic "IR blob" that responds to the
exposure/gain controls, so the whole UI can be exercised. Mock recordings are
`.mjpeg` (concatenated JPEGs — playable with `ffplay`/VLC). The server also
falls back to the mock automatically when picamera2 isn't importable.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | web UI |
| GET | `/stream.mjpg` | MJPEG live stream (640×360) |
| GET | `/snapshot.jpg` | full-res JPEG still |
| GET | `/api/status` | recording state, controls, sensor metadata, luma stats |
| POST | `/api/controls` | patch controls: `ae_enable`, `exposure_us`, `gain`, `awb_enable`, `af_continuous`, `lens_position` |
| POST | `/api/record/start` | body `{"label": "blaster-a", "notes": "1 m, on-axis"}` → starts 1080p30 H.264/MP4 + CSV/JSON sidecars |
| POST | `/api/record/stop` | stop and finalize the MP4 + sidecars |
| POST | `/api/snapshot` | save a labeled full-res still into recordings |
| GET | `/api/recordings` | list recordings, grouped per base name with device/notes |
| GET | `/api/recordings/{base}/summary` | analyzed CSV: baseline vs. burst brightness, peak, clipping |
| GET | `/recordings/{name}` | download any file (mp4/csv/json/jpg) |
| DELETE | `/api/recordings/{base}` | delete a whole bundle (refused while recording) |
| POST | `/api/test-run` | one-shot: record → fire a trigger URL (HA webhook) → stop → summary |
| POST | `/api/calibrate` | auto-find & lock exposure/gain: fires a trigger URL repeatedly, measures off/on, converges on unclipped burst ≈ `target_peak` |
| POST | `/api/mock/blast` | mock mode only: simulate an IR device transmitting (use as calibration trigger in dev) |
| GET | `/openapi.json`, `/docs` | machine-readable API spec / interactive docs |

Everything is scriptable, e.g. an automated per-device sweep:

```bash
curl -X POST localhost:8000/api/record/start -H 'Content-Type: application/json' \
  -d '{"label":"blaster-a","notes":"1 m distance, on-axis, NEC repeat @1 Hz"}'
# ... trigger the blaster ...
curl -X POST localhost:8000/api/record/stop
```

## Remote control by a Claude agent (with Home Assistant firing the blasters)

The whole rig is plain JSON-over-HTTP, so any agent that can make HTTP
requests (e.g. Claude Code with `curl`) can drive it end to end. Point the
agent at `http://<pi-host>:8000/openapi.json` and it can discover the full
API by itself.

**Recommended setup — one-call test runs.** Give each blaster an HA
automation with a webhook trigger (webhooks need no auth token):

```yaml
automation:
  - alias: "IR test burst - Blaster A"
    triggers:
      - trigger: webhook
        webhook_id: ir-test-blaster-a
        local_only: true
        allowed_methods: [POST]
    actions:
      - action: remote.send_command
        target: { entity_id: remote.blaster_a }
        data: { device: test, command: power, num_repeats: 10 }
```

Then one API call runs the whole experiment — the Pi starts recording, lets
exposure settle, fires the webhook (the trigger moment is stamped into the
`.json` sidecar), records for `duration_s`, stops, and returns the analysis:

```bash
curl -X POST http://<pi-host>:8000/api/test-run -H 'Content-Type: application/json' -d '{
  "label": "blaster-a",
  "notes": "1 m distance, on-axis, power x10 repeats",
  "duration_s": 6,
  "trigger_url": "http://homeassistant.local:8123/api/webhook/ir-test-blaster-a"
}'
```

The response's `summary` gives `roi_baseline_mean` (ambient), `roi_peak`,
`active.roi_mean` (average brightness during the burst — the headline number
for comparing devices), burst duration, and a `clipping` flag telling the
agent the run is invalid and exposure must be lowered.

A typical agent loop: `POST /api/calibrate` against the brightest device's
webhook (locks a clip-free exposure automatically) → test-run each device →
rank devices by `active.roi_mean` → cite the mp4/csv files as evidence. The
calibration automation should transmit for ~3 s (e.g. `num_repeats: 30`) so
the burst spans the measurement window.

**Bulk-creating the automations.** `ha/create_automations.py` posts a burst
automation, per-TV volume tests, and an all-device sweep to HA for every
blaster you list. Your hardware lives in `ha/devices.json`, which is
gitignored — copy `ha/devices.example.json`, fill in your own entity ids, and
check it with `--dry-run` before posting:

```bash
cp ha/devices.example.json ha/devices.json   # then edit
python3 ha/create_automations.py --dry-run
HA_TOKEN=<long-lived access token> python3 ha/create_automations.py
```

**Agent-orchestrated alternative.** The agent can sequence things itself:
`POST /api/record/start`, then call HA's REST API directly
(`POST /api/services/automation/trigger` with an `Authorization: Bearer
<long-lived-token>` header), then `POST /api/record/stop` and
`GET /api/recordings/{base}/summary`. Same result, looser timing.

`trigger_url` is generic — anything that fires on an HTTP POST works, not
just HA (`trigger_payload` is forwarded as the JSON body if given).

## Notes & limits

- Shutter is capped at ~33 ms by the 30 fps frame rate (fine for IR work,
  where you usually want *shorter* exposures to stay under saturation).
- The live stream/meter uses the 640×360 lores stream; recordings and
  snapshots use the full 1920×1080 main stream. Both encoders run in hardware
  on the Pi 4, so streaming stays live while recording.
- No auth — intended for a trusted LAN. Put it behind a reverse proxy with
  auth if it needs to be reachable more widely.
