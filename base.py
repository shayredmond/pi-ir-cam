"""Shared pieces used by both the real Picamera2 backend and the mock backend."""

import csv
import datetime
import io
import json
import re
import threading

import numpy as np

# Fraction of the frame (width and height) covered by the centered measurement ROI.
ROI_FRACTION = 0.2

# Luma values at or above this count as clipped/saturated.
CLIP_THRESHOLD = 250


class StreamingOutput(io.BufferedIOBase):
    """Holds the latest MJPEG frame; stream clients wait on the condition."""

    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", label.strip()).strip("-")
    return slug[:60]


def luma_stats(y: np.ndarray) -> dict:
    """Brightness stats for a luma (Y) plane: full frame plus centered ROI."""

    def region(a):
        return {
            "mean": round(float(a.mean()), 1),
            "max": int(a.max()),
            "clipped_pct": round(float((a >= CLIP_THRESHOLD).mean() * 100.0), 2),
        }

    h, w = y.shape
    rh = max(1, int(h * ROI_FRACTION / 2))
    rw = max(1, int(w * ROI_FRACTION / 2))
    cy, cx = h // 2, w // 2
    roi = y[cy - rh:cy + rh, cx - rw:cx + rw]
    return {"full": region(y), "roi": region(roi), "roi_fraction": ROI_FRACTION}


class RecordingLog:
    """Sidecar files for a recording: <base>.json (device/notes/settings)
    and <base>.csv (one brightness sample per frame)."""

    FLUSH_EVERY = 30

    def __init__(self, recordings_dir, base, device, notes, controls):
        meta = {
            "device": device,
            "notes": notes,
            "started": _now_iso("seconds"),
            "controls": dict(controls),
        }
        (recordings_dir / f"{base}.json").write_text(json.dumps(meta, indent=2))
        self._fh = open(recordings_dir / f"{base}.csv", "w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(
            ["time", "elapsed_s", "roi_mean", "roi_max", "roi_clipped_pct",
             "full_mean", "full_max", "full_clipped_pct", "exposure_us", "gain"]
        )
        self._count = 0

    def sample(self, elapsed_s, stats, exposure_us, gain):
        roi, full = stats["roi"], stats["full"]
        self._writer.writerow(
            [_now_iso("milliseconds"), round(elapsed_s, 3),
             roi["mean"], roi["max"], roi["clipped_pct"],
             full["mean"], full["max"], full["clipped_pct"],
             exposure_us, round(gain, 3) if gain is not None else ""]
        )
        self._count += 1
        if self._count % self.FLUSH_EVERY == 0:
            self._fh.flush()

    def close(self):
        self._fh.close()


def _now_iso(timespec):
    return datetime.datetime.now().astimezone().isoformat(timespec=timespec)


DEFAULT_CONTROLS = {
    "ae_enable": False,
    "exposure_us": 5000,
    "gain": 2.0,
    "awb_enable": True,
    "af_continuous": False,
    "lens_position": 1.0,
}
