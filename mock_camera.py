"""Mock camera backend for developing the UI without a Pi.

Generates synthetic frames with a pulsing "IR blob" in the center whose
brightness responds to the exposure/gain controls, so the whole UI
(stream, meters, controls, recording, overlay, CSV log) can be exercised
end to end. Mock recordings are concatenated JPEG frames written as
.mjpeg (playable with VLC / ffplay).
"""

import io
import math
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from base import DEFAULT_CONTROLS, RecordingLog, StreamingOutput, luma_stats, slugify
from overlay import TextOverlay

SIZE = (640, 360)
FPS = 12


class MockCamera:
    is_mock = True
    has_af = True

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir
        self.controls = dict(DEFAULT_CONTROLS)
        self.stream_output = StreamingOutput()

        self._lock = threading.Lock()
        self._last_y = np.zeros((SIZE[1], SIZE[0]), dtype=np.uint8)
        self._last_jpeg = b""
        self._rec_fh = None
        self._rec_name = None
        self._rec_label = ""
        self._rec_started = None
        self._rec_log = None

        self._overlay = TextOverlay(SIZE[0], font_size=13, pad=5)
        self._blast_until = 0.0  # mock "IR transmission" active until this time

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _make_frame(self, t: float) -> np.ndarray:
        w, h = SIZE
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        base = 20 + 15 * (xx / w)  # dim gradient background

        # Central blob = the IR LED: bright while "blasting" (see mock_blast),
        # a faint idle glow otherwise. Brightness follows exposure * gain when
        # AE is off, so the calibration loop behaves like the real sensor.
        blasting = time.monotonic() < self._blast_until
        if self.controls["ae_enable"]:
            strength = 160.0 if blasting else 30.0
        else:
            strength = 900.0 * (self.controls["exposure_us"] / 33000.0) * self.controls["gain"]
            if not blasting:
                strength *= 0.05
        pulse = 0.85 + 0.15 * math.sin(t * 8.0)
        d2 = (xx - w / 2) ** 2 + (yy - h / 2) ** 2
        blob = strength * pulse * np.exp(-d2 / (2 * 40.0**2))

        noise = np.random.default_rng(int(t * FPS)).normal(0, 2, (h, w))
        return np.clip(base + blob + noise, 0, 255).astype(np.uint8)

    def _run(self):
        t0 = time.monotonic()
        while not self._stop.is_set():
            t = time.monotonic() - t0
            y = self._make_frame(t)
            stats = luma_stats(y)

            with self._lock:
                self._last_y = y.copy()
                recording = self._rec_fh is not None
                if recording:
                    elapsed = time.monotonic() - self._rec_started
                    exposure = int(self.controls["exposure_us"])
                    gain = float(self.controls["gain"])
                    self._rec_log.sample(elapsed, stats, exposure, gain)
                    roi = stats["roi"]
                    lines = [
                        f"{self._rec_label or 'recording'}"
                        f"  {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        f"  REC {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}",
                        f"ROI mean {roi['mean']:5.1f}  peak {roi['max']:3d}"
                        f"  clip {roi['clipped_pct']:.1f}%"
                        f"  |  {exposure} us  gain {gain:.2f}x",
                    ]
                    TextOverlay.blit(y, self._overlay.render(lines))

            img = Image.fromarray(y, mode="L").convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="jpeg", quality=80)
            jpeg = buf.getvalue()

            with self._lock:
                self._last_jpeg = jpeg
                if self._rec_fh is not None:
                    self._rec_fh.write(jpeg)

            self.stream_output.write(jpeg)
            time.sleep(1.0 / FPS)

    def mock_blast(self, seconds: float):
        """Simulate an IR device transmitting for the given duration."""
        self._blast_until = time.monotonic() + seconds

    # ---- controls ----

    def set_controls(self, **kwargs) -> dict:
        for key, value in kwargs.items():
            if value is not None and key in self.controls:
                self.controls[key] = value
        return dict(self.controls)

    def get_metadata(self) -> dict:
        ae = self.controls["ae_enable"]
        return {
            "exposure_us": 8000 if ae else int(self.controls["exposure_us"]),
            "gain": 1.5 if ae else round(float(self.controls["gain"]), 2),
            "digital_gain": 1.0,
            "lux": 42.0,
            "lens_position": round(float(self.controls["lens_position"]), 2),
            "frame_duration_us": 33333,
            "sensor_temp": None,
        }

    def get_stats(self) -> dict:
        with self._lock:
            return luma_stats(self._last_y)

    # ---- recording ----

    def start_recording(self, label: str = "", notes: str = "") -> str:
        with self._lock:
            if self._rec_fh is not None:
                raise RuntimeError("Already recording")
            slug = slugify(label)
            base = time.strftime("%Y%m%d-%H%M%S") + (f"-{slug}" if slug else "")
            self._rec_log = RecordingLog(self.recordings_dir, base, label, notes, self.controls)
            self._rec_fh = open(self.recordings_dir / f"{base}.mjpeg", "wb")
            self._rec_name = f"{base}.mjpeg"
            self._rec_label = label
            self._rec_started = time.monotonic()
            return self._rec_name

    def stop_recording(self) -> str:
        with self._lock:
            if self._rec_fh is None:
                raise RuntimeError("Not recording")
            self._rec_fh.close()
            self._rec_log.close()
            name = self._rec_name
            self._rec_fh = None
            self._rec_name = None
            self._rec_label = ""
            self._rec_started = None
            self._rec_log = None
            return name

    def recording_status(self) -> dict:
        with self._lock:
            if self._rec_fh is None:
                return {"active": False}
            return {
                "active": True,
                "file": self._rec_name,
                "label": self._rec_label,
                "elapsed_s": round(time.monotonic() - self._rec_started, 1),
            }

    def active_recording_file(self):
        with self._lock:
            return self._rec_name

    # ---- stills ----

    def capture_jpeg(self) -> bytes:
        with self._lock:
            return self._last_jpeg

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        with self._lock:
            if self._rec_fh is not None:
                self._rec_fh.close()
                self._rec_fh = None
            if self._rec_log is not None:
                self._rec_log.close()
                self._rec_log = None
