"""Pi IR Cam — web interface for viewing the camera and triggering recordings.

Run on the Pi:   python server.py
Run on a dev machine (no camera): python server.py --mock
"""

import argparse
import contextlib
import csv
import json
import math
import os
import time
import urllib.request
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RECORDINGS_DIR = Path(os.environ.get("PI_IR_CAM_RECORDINGS", BASE_DIR / "recordings"))

USE_MOCK = os.environ.get("PI_IR_CAM_MOCK") == "1"

cam = None


def create_camera():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if not USE_MOCK:
        try:
            from camera import PiCamera

            return PiCamera(RECORDINGS_DIR)
        except ImportError as exc:
            print(f"picamera2 not available ({exc}); using mock camera")
    from mock_camera import MockCamera

    return MockCamera(RECORDINGS_DIR)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global cam
    cam = create_camera()
    print(f"Camera backend: {'mock' if cam.is_mock else 'picamera2'}")
    yield
    cam.close()


app = FastAPI(title="Pi IR Cam", lifespan=lifespan)


# ---- pages / streams ----


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/stream.mjpg")
def stream():
    def gen():
        output = cam.stream_output
        while True:
            with output.condition:
                output.condition.wait(timeout=5)
                frame = output.frame
            if frame is None:
                continue
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(frame)).encode()
                + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/snapshot.jpg")
def snapshot():
    return Response(content=cam.capture_jpeg(), media_type="image/jpeg")


# ---- status & controls ----


@app.get("/api/status")
def status():
    return {
        "mock": cam.is_mock,
        "has_af": cam.has_af,
        "recording": cam.recording_status(),
        "controls": dict(cam.controls),
        "metadata": cam.get_metadata(),
        "stats": cam.get_stats(),
    }


class ControlsPatch(BaseModel):
    ae_enable: bool | None = None
    exposure_us: int | None = Field(None, ge=30, le=66000)
    gain: float | None = Field(None, ge=1.0, le=16.0)
    awb_enable: bool | None = None
    af_continuous: bool | None = None
    lens_position: float | None = Field(None, ge=0.0, le=10.0)


@app.post("/api/controls")
def set_controls(patch: ControlsPatch):
    return cam.set_controls(**patch.model_dump())


# ---- recording ----


class RecordStart(BaseModel):
    label: str = ""
    notes: str = ""


@app.post("/api/record/start")
def record_start(body: RecordStart):
    try:
        name = cam.start_recording(body.label, body.notes)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"file": name}


@app.post("/api/record/stop")
def record_stop():
    try:
        name = cam.stop_recording()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"file": name}


class SnapshotSave(BaseModel):
    label: str = ""


@app.post("/api/snapshot")
def snapshot_save(body: SnapshotSave):
    from base import slugify

    slug = slugify(body.label)
    name = time.strftime("%Y%m%d-%H%M%S") + (f"-{slug}" if slug else "") + ".jpg"
    (RECORDINGS_DIR / name).write_bytes(cam.capture_jpeg())
    return {"file": name}


# ---- recordings library ----

RECORDING_EXTS = (".mp4", ".mjpeg", ".jpg", ".csv", ".json")
VIDEO_EXTS = (".mp4", ".mjpeg")
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".csv": "text/csv",
    ".json": "application/json",
    ".mp4": "video/mp4",
    ".mjpeg": "video/x-motion-jpeg",
}


def _safe_recording_path(name: str) -> Path:
    path = (RECORDINGS_DIR / name).resolve()
    if path.parent != RECORDINGS_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="No such recording")
    return path


@app.get("/api/recordings")
def list_recordings():
    groups = {}
    for p in RECORDINGS_DIR.iterdir():
        if not p.is_file() or p.suffix not in RECORDING_EXTS:
            continue
        g = groups.setdefault(
            p.stem,
            {"base": p.stem, "video": None, "csv": None, "image": None,
             "device": "", "notes": "", "size": 0, "mtime": 0},
        )
        st = p.stat()
        g["size"] += st.st_size
        g["mtime"] = max(g["mtime"], st.st_mtime)
        if p.suffix in VIDEO_EXTS:
            g["video"] = p.name
        elif p.suffix == ".csv":
            g["csv"] = p.name
        elif p.suffix == ".jpg":
            g["image"] = p.name
        elif p.suffix == ".json":
            try:
                meta = json.loads(p.read_text())
                g["device"] = meta.get("device", "")
                g["notes"] = meta.get("notes", "")
            except (OSError, ValueError):
                pass
    entries = sorted(groups.values(), key=lambda g: g["mtime"], reverse=True)
    active = cam.active_recording_file()
    return {
        "recordings": entries,
        "active_base": Path(active).stem if active else None,
    }


@app.get("/recordings/{name}")
def download_recording(name: str):
    path = _safe_recording_path(name)
    media_type = MEDIA_TYPES.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.delete("/api/recordings/{base}")
def delete_recording(base: str):
    active = cam.active_recording_file()
    if active and Path(active).stem == base:
        raise HTTPException(status_code=409, detail="Recording is in progress")
    deleted = []
    for ext in RECORDING_EXTS:
        path = (RECORDINGS_DIR / f"{base}{ext}").resolve()
        if path.parent == RECORDINGS_DIR.resolve() and path.is_file():
            path.unlink()
            deleted.append(path.name)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such recording")
    return {"deleted": deleted}


# ---- analysis & agent-friendly orchestration ----


def _read_meta(base: str) -> dict:
    path = RECORDINGS_DIR / f"{base}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _require_not_active(base: str):
    active = cam.active_recording_file()
    if active and Path(active).stem == base:
        raise HTTPException(status_code=409, detail="Recording is in progress")


def summarize_recording(base: str) -> dict:
    """Digest a recording's CSV into burst-level numbers an agent can act on."""
    csv_path = _safe_recording_path(f"{base}.csv")
    with csv_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {"samples": 0}

    roi = np.array([float(r["roi_mean"]) for r in rows])
    roi_max = np.array([float(r["roi_max"]) for r in rows])
    clip = np.array([float(r["roi_clipped_pct"]) for r in rows])
    elapsed = np.array([float(r["elapsed_s"]) for r in rows])

    baseline = float(np.percentile(roi, 10))
    peak_i = int(np.argmax(roi))
    peak = float(roi[peak_i])
    summary = {
        "samples": len(rows),
        "duration_s": round(float(elapsed[-1]), 3),
        "roi_baseline_mean": round(baseline, 1),
        "roi_peak": {
            "mean": round(peak, 1),
            "max": int(roi_max[peak_i]),
            "elapsed_s": round(float(elapsed[peak_i]), 3),
        },
        "max_clipped_pct": round(float(clip.max()), 2),
        "clipping": bool(clip.max() > 1.0),
    }

    # "Active" = frames meaningfully above baseline, i.e. the IR burst itself.
    rise = peak - baseline
    if rise >= 5.0:
        threshold = baseline + 0.2 * rise
        mask = roi >= threshold
        interval = float(np.median(np.diff(elapsed))) if len(elapsed) > 1 else 0.0
        summary["active"] = {
            "threshold": round(threshold, 1),
            "frames": int(mask.sum()),
            "approx_duration_s": round(float(mask.sum()) * interval, 3),
            "roi_mean": round(float(roi[mask].mean()), 1),
            "first_elapsed_s": round(float(elapsed[mask][0]), 3),
        }
    else:
        summary["active"] = None  # nothing rose above baseline — no burst seen
    return summary


@app.get("/api/recordings/{base}/summary")
def recording_summary(base: str):
    _require_not_active(base)
    return {"base": base, "meta": _read_meta(base), "summary": summarize_recording(base)}


class TestRun(BaseModel):
    label: str = ""
    notes: str = ""
    duration_s: float = Field(5.0, gt=0, le=120)
    settle_s: float = Field(0.5, ge=0, le=10)
    trigger_url: str | None = None
    trigger_payload: dict | None = None


def _post_trigger(url: str, payload):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


@app.post("/api/test-run")
def test_run(body: TestRun):
    """One-shot test: record, fire the trigger (e.g. a Home Assistant webhook
    that makes the blaster transmit), keep recording for duration_s, then
    stop and return the brightness summary."""
    try:
        name = cam.start_recording(body.label, body.notes)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    base = Path(name).stem

    t0 = time.monotonic()
    trigger = None
    try:
        if body.settle_s:
            time.sleep(body.settle_s)
        if body.trigger_url:
            trigger = {
                "url": body.trigger_url,
                "sent_at_elapsed_s": round(time.monotonic() - t0, 3),
            }
            _post_trigger(body.trigger_url, body.trigger_payload)
    except Exception as exc:
        cam.stop_recording()
        raise HTTPException(
            status_code=502,
            detail=f"Trigger failed: {exc} (partial recording {base} kept)",
        )
    remaining = body.duration_s - (time.monotonic() - t0)
    if remaining > 0:
        time.sleep(remaining)
    cam.stop_recording()

    if trigger:  # stamp the trigger moment into the metadata sidecar
        meta = _read_meta(base)
        meta["trigger"] = trigger
        (RECORDINGS_DIR / f"{base}.json").write_text(json.dumps(meta, indent=2))

    return {
        "base": base,
        "video": name,
        "trigger": trigger,
        "meta": _read_meta(base),
        "summary": summarize_recording(base),
    }


# ---- exposure calibration ----

EXP_MIN, EXP_MAX = 30, 33000
GAIN_MIN, GAIN_MAX = 1.0, 16.0
CLIP_LIMIT_PCT = 0.5  # max acceptable ROI clipping during the burst


class CalibrateRequest(BaseModel):
    trigger_url: str
    trigger_payload: dict | None = None
    gain: float | None = Field(None, ge=GAIN_MIN, le=GAIN_MAX)  # starting gain
    target_peak: float = Field(220.0, ge=120, le=245)  # target for the ROI's brightest pixel
    burst_s: float = Field(3.0, ge=0.5, le=15)  # how long the automation transmits
    settle_s: float = Field(0.4, ge=0.1, le=5)
    off_measure_s: float = Field(0.6, ge=0.2, le=5)
    on_measure_s: float = Field(1.5, ge=0.3, le=10)
    max_iterations: int = Field(6, ge=1, le=12)


def _measure_roi(seconds: float) -> dict:
    """Poll ROI stats for a window: mean-of-means, brightest frame-mean,
    brightest single pixel, worst clipping."""
    t_end = time.monotonic() + seconds
    means, peak_mean, peak_px, clip = [], 0.0, 0, 0.0
    while time.monotonic() < t_end:
        roi = cam.get_stats()["roi"]
        means.append(roi["mean"])
        peak_mean = max(peak_mean, roi["mean"])
        peak_px = max(peak_px, roi["max"])
        clip = max(clip, roi["clipped_pct"])
        time.sleep(0.05)
    return {"mean": sum(means) / len(means), "peak_mean": peak_mean,
            "peak_px": peak_px, "clip": clip}


@app.post("/api/calibrate")
def calibrate(body: CalibrateRequest):
    """Find and lock exposure/gain for valid IR comparisons: repeatedly fire
    the trigger (an HA automation that transmits for ~burst_s seconds),
    measure the burst against ambient, and search the exposure×gain space
    until the burst's brightest ROI pixel sits near target_peak with no
    clipping. Bisects between the last non-clipping and first clipping
    settings so it can't oscillate."""
    if cam.recording_status()["active"]:
        raise HTTPException(status_code=409, detail="Recording is in progress")

    md = cam.get_metadata()
    if cam.controls["ae_enable"]:  # start from what AE settled on
        exposure = float(md.get("exposure_us") or cam.controls["exposure_us"])
        gain = body.gain or max(GAIN_MIN, float(md.get("gain") or 2.0))
    else:
        exposure = float(cam.controls["exposure_us"])
        gain = body.gain or float(cam.controls["gain"])

    # Search over the exposure*gain product; prefer long exposure / low gain.
    P_MIN, P_MAX = EXP_MIN * GAIN_MIN, EXP_MAX * GAIN_MAX
    product = min(max(exposure * gain, P_MIN), P_MAX)
    p_good = None  # largest product known not to clip
    p_bad = None   # smallest product known to clip
    best = None    # iteration entry measured at p_good

    def split(p):
        g = min(max(p / EXP_MAX, GAIN_MIN), GAIN_MAX)
        return int(min(max(p / g, EXP_MIN), EXP_MAX)), round(g, 2)

    def shrink(p, factor):
        nonlocal p_bad
        p_bad = min(p_bad, p) if p_bad else p
        return math.sqrt(p_good * p) if p_good else p * factor

    iterations = []
    converged = None
    burst_ends_at = 0.0  # the burst may outlast the on-measurement window
    for _ in range(body.max_iterations):
        exp_i, gain_i = split(product)
        cam.set_controls(ae_enable=False, exposure_us=exp_i, gain=gain_i)
        time.sleep(body.settle_s)
        cooldown = burst_ends_at + 0.3 - time.monotonic()
        if cooldown > 0:  # let the previous burst finish before measuring "off"
            time.sleep(cooldown)
        off = _measure_roi(body.off_measure_s)

        entry = {"exposure_us": exp_i, "gain": gain_i, "off_mean": round(off["mean"], 1)}
        iterations.append(entry)

        if off["clip"] > CLIP_LIMIT_PCT or off["mean"] >= 245:
            # Ambient alone saturates — reduce without wasting a burst.
            entry["action"] = "ambient clipping - reducing"
            new_p = shrink(product, 0.35)
        else:
            try:
                burst_ends_at = time.monotonic() + body.burst_s
                _post_trigger(body.trigger_url, body.trigger_payload)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Trigger failed: {exc}")
            on = _measure_roi(body.on_measure_s)
            entry.update({
                "on_peak_px": on["peak_px"],
                "on_peak_mean": round(on["peak_mean"], 1),
                "on_clipped_pct": round(on["clip"], 2),
                "rise_px": round(on["peak_px"] - off["peak_px"], 1),
            })
            if on["clip"] > CLIP_LIMIT_PCT or on["peak_px"] >= 250:
                entry["action"] = "clipping - reducing"
                new_p = shrink(product, 0.35 if on["clip"] > 10.0 else 0.7)
            elif on["peak_px"] < 0.8 * body.target_peak:
                p_good = max(p_good, product) if p_good else product
                best = entry
                # Luma is gamma-encoded (~sqrt of linear), so the linear
                # exposure factor is roughly the luma ratio squared.
                factor = min(4.0, (body.target_peak / max(on["peak_px"], 1)) ** 2)
                entry["action"] = f"scaling x{factor:.2f}"
                new_p = product * factor
                if p_bad:  # never jump past a known-clipping point
                    new_p = min(new_p, math.sqrt(product * p_bad))
            elif entry["rise_px"] < 15:
                # In the target band, but the trigger barely changed anything:
                # we're measuring ambient, not the IR burst. Escalating further
                # would only clip ambient — fail informatively instead.
                entry["action"] = "in band but no IR rise"
                return {
                    "success": False,
                    "reason": "The ROI reaches target brightness but barely "
                              "changes when the trigger fires — the burst wasn't "
                              "detected. Check that the automation transmits for "
                              f"~{body.burst_s:g} s and the device is aimed into "
                              "the ROI; the bright spot may just be ambient.",
                    "iterations": iterations,
                    "controls": dict(cam.controls),
                }
            else:
                entry["action"] = "converged"
                p_good = max(p_good, product) if p_good else product
                best = converged = entry
                break

        new_p = min(max(new_p, P_MIN), P_MAX)
        if abs(new_p - product) / product < 0.02:
            entry["action"] += " (pinned at limits)"
            break
        product = new_p

    # Never leave clipping settings locked: fall back to the best safe point.
    if best and not converged:
        cam.set_controls(ae_enable=False,
                         exposure_us=best["exposure_us"], gain=best["gain"])

    ref = converged or best
    result = None
    if ref:
        result = {
            "off_mean": ref["off_mean"],
            "on_peak_px": ref["on_peak_px"],
            "on_peak_mean": ref["on_peak_mean"],
            "on_clipped_pct": ref["on_clipped_pct"],
            "headroom_pct": round((250 - ref["on_peak_px"]) / 250 * 100, 1),
        }
    if converged:
        return {"success": True, "iterations": iterations,
                "controls": dict(cam.controls), "result": result}
    if best is None:
        reason = ("Every attempt clipped, even at minimum exposure/gain — "
                  "darken the room or move/re-aim the device.")
    elif best["rise_px"] < 3:
        reason = ("No IR rise detected over ambient — check that the trigger "
                  "automation transmits for ~3 s, the device is aimed at the "
                  "camera, and the webhook URL is right.")
    else:
        reason = ("Did not land in the target band within max_iterations; "
                  "locked the best non-clipping settings found.")
    return {"success": False, "reason": reason, "iterations": iterations,
            "controls": dict(cam.controls), "result": result}


class MockBlast(BaseModel):
    seconds: float = Field(3.0, gt=0, le=30)


@app.post("/api/mock/blast")
def mock_blast(body: MockBlast | None = None):
    """Mock-only stand-in for an IR device: point calibration's trigger_url here."""
    body = body or MockBlast()
    if not cam.is_mock:
        raise HTTPException(status_code=404, detail="Only available with the mock camera")
    cam.mock_blast(body.seconds)
    return {"blasting_for_s": body.seconds}


def main():
    global USE_MOCK
    parser = argparse.ArgumentParser(description="Pi IR Cam server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mock", action="store_true", help="use synthetic camera")
    args = parser.parse_args()
    if args.mock:
        USE_MOCK = True

    import uvicorn

    # Open MJPEG streams never end on their own — don't let them stall shutdown.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
