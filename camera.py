"""Real camera backend: Picamera2 driving a Camera Module 3 (NoIR Wide).

Runs two hardware encoders at once on a Pi 4:
  - MJPEG on the low-res stream, feeding the browser live view
  - H.264 on the full-res stream, started/stopped per recording (muxed to MP4 via ffmpeg)

A pre_callback runs per frame: it computes luma stats from the lores buffer
(shared with the web UI meter), and while recording it appends a CSV sample
and burns a text overlay (device, time, ROI luma, shutter/gain) into the
main stream's Y plane — so the overlay lands in recordings/snapshots but
never in the live view or the measurements.
"""

import io
import threading
import time
from pathlib import Path

import numpy as np
from picamera2 import MappedArray, Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import FfmpegOutput, FileOutput

from base import DEFAULT_CONTROLS, RecordingLog, StreamingOutput, luma_stats, slugify
from overlay import TextOverlay

MAIN_SIZE = (1920, 1080)
LORES_SIZE = (640, 360)
FRAME_RATE = 30
RECORD_BITRATE = 10_000_000
OVERLAY_REFRESH_FRAMES = 3  # re-render overlay text every N frames

# libcamera AfMode values
AF_MODE_MANUAL = 0
AF_MODE_CONTINUOUS = 2


def yuv420_to_rgb(arr: np.ndarray, size) -> np.ndarray:
    """Planar YUV420 (as returned by capture_array) -> RGB, BT.601-ish."""
    w, h = size
    y = arr[:h, :w].astype(np.float32)
    u = arr[h:h + h // 4, :w].reshape(h // 2, w // 2).astype(np.float32) - 128.0
    v = arr[h + h // 4:h + h // 2, :w].reshape(h // 2, w // 2).astype(np.float32) - 128.0
    u = u.repeat(2, axis=0).repeat(2, axis=1)
    v = v.repeat(2, axis=0).repeat(2, axis=1)
    rgb = np.stack(
        [y + 1.402 * v, y - 0.344136 * u - 0.714136 * v, y + 1.772 * u], axis=-1
    )
    return np.clip(rgb, 0, 255).astype(np.uint8)


class PiCamera:
    is_mock = False

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir
        self.controls = dict(DEFAULT_CONTROLS)

        # _rec_lock guards the recording state fields and is taken by the
        # per-frame callback, so it must NEVER be held while calling into
        # picamera2 (start_encoder/stop_encoder) — the camera thread runs the
        # callback while holding picamera2's own lock, and holding _rec_lock
        # across start_encoder deadlocks against it. _op_lock (never touched
        # by the callback) serializes whole start/stop operations instead.
        self._op_lock = threading.Lock()
        self._rec_lock = threading.Lock()
        self._rec_encoder = None
        self._rec_name = None
        self._rec_label = ""
        self._rec_started = None
        self._rec_log = None

        self._overlay = TextOverlay(MAIN_SIZE[0], font_size=30)
        self._overlay_strip = None
        self._frame_count = 0
        self._last_stats = None

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": MAIN_SIZE, "format": "YUV420"},
            lores={"size": LORES_SIZE, "format": "YUV420"},
            controls={"FrameRate": FRAME_RATE},
        )
        self.picam2.configure(config)
        self.picam2.pre_callback = self._on_frame
        self.picam2.start()
        self._apply_controls(self.controls)

        self.stream_output = StreamingOutput()
        self._mjpeg_encoder = MJPEGEncoder()
        self.picam2.start_encoder(
            self._mjpeg_encoder, FileOutput(self.stream_output), name="lores"
        )

    # ---- per-frame callback ----

    def _on_frame(self, request):
        try:
            with MappedArray(request, "lores") as m:
                stats = luma_stats(m.array[:LORES_SIZE[1], :LORES_SIZE[0]])
            self._last_stats = stats

            strip = None
            with self._rec_lock:
                if self._rec_encoder is not None:
                    elapsed = time.monotonic() - self._rec_started
                    md = request.get_metadata()
                    exposure = md.get("ExposureTime")
                    gain = md.get("AnalogueGain")
                    self._rec_log.sample(elapsed, stats, exposure, gain)
                    self._frame_count += 1
                    if (
                        self._overlay_strip is None
                        or self._frame_count % OVERLAY_REFRESH_FRAMES == 0
                    ):
                        roi = stats["roi"]
                        lines = [
                            f"{self._rec_label or 'recording'}"
                            f"  {time.strftime('%Y-%m-%d %H:%M:%S')}"
                            f"  REC {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}",
                            f"ROI mean {roi['mean']:5.1f}  peak {roi['max']:3d}"
                            f"  clip {roi['clipped_pct']:.1f}%"
                            f"  |  {exposure if exposure is not None else '?'} us"
                            f"  gain {f'{gain:.2f}' if gain is not None else '?'}x",
                        ]
                        self._overlay_strip = self._overlay.render(lines)
                    strip = self._overlay_strip

            if strip is not None:
                with MappedArray(request, "main") as m:
                    TextOverlay.blit(m.array, strip)
        except Exception as exc:  # never let a hiccup kill the camera loop
            print(f"frame callback error: {exc}")

    # ---- controls ----

    def _apply_controls(self, state: dict):
        ctrls = {
            "AeEnable": bool(state["ae_enable"]),
            "AwbEnable": bool(state["awb_enable"]),
        }
        if not state["ae_enable"]:
            ctrls["ExposureTime"] = int(state["exposure_us"])
            ctrls["AnalogueGain"] = float(state["gain"])
        if state["af_continuous"]:
            ctrls["AfMode"] = AF_MODE_CONTINUOUS
        else:
            ctrls["AfMode"] = AF_MODE_MANUAL
            ctrls["LensPosition"] = float(state["lens_position"])
        self.picam2.set_controls(ctrls)

    def set_controls(self, **kwargs) -> dict:
        for key, value in kwargs.items():
            if value is not None and key in self.controls:
                self.controls[key] = value
        self._apply_controls(self.controls)
        return dict(self.controls)

    def get_metadata(self) -> dict:
        md = self.picam2.capture_metadata()
        return {
            "exposure_us": md.get("ExposureTime"),
            "gain": round(md.get("AnalogueGain", 0.0), 2),
            "digital_gain": round(md.get("DigitalGain", 0.0), 2),
            "lux": round(md["Lux"], 1) if "Lux" in md else None,
            "lens_position": round(md["LensPosition"], 2) if "LensPosition" in md else None,
            "frame_duration_us": md.get("FrameDuration"),
            "sensor_temp": md.get("SensorTemperature"),
        }

    def get_stats(self) -> dict:
        if self._last_stats is None:
            arr = self.picam2.capture_array("lores")
            return luma_stats(arr[:LORES_SIZE[1], :LORES_SIZE[0]])
        return self._last_stats

    # ---- recording ----

    def start_recording(self, label: str = "", notes: str = "") -> str:
        with self._op_lock:
            if self._rec_encoder is not None:
                raise RuntimeError("Already recording")
            slug = slugify(label)
            base = time.strftime("%Y%m%d-%H%M%S") + (f"-{slug}" if slug else "")
            log = RecordingLog(self.recordings_dir, base, label, notes, self.controls)
            encoder = H264Encoder(bitrate=RECORD_BITRATE)
            output = FfmpegOutput(str(self.recordings_dir / f"{base}.mp4"))
            try:
                self.picam2.start_encoder(encoder, output, name="main")
            except Exception:
                log.close()
                raise
            with self._rec_lock:
                self._rec_encoder = encoder
                self._rec_name = f"{base}.mp4"
                self._rec_label = label
                self._rec_started = time.monotonic()
                self._rec_log = log
                self._frame_count = 0
                self._overlay_strip = None
            return f"{base}.mp4"

    def stop_recording(self) -> str:
        with self._op_lock:
            if self._rec_encoder is None:
                raise RuntimeError("Not recording")
            with self._rec_lock:
                encoder = self._rec_encoder
                name = self._rec_name
                log = self._rec_log
                self._rec_encoder = None
                self._rec_name = None
                self._rec_label = ""
                self._rec_started = None
                self._rec_log = None
                self._overlay_strip = None
            # Callback no longer sees the recording; safe to call picamera2
            # and close the log outside _rec_lock.
            self.picam2.stop_encoder(encoder)
            log.close()
            return name

    def recording_status(self) -> dict:
        with self._rec_lock:
            if self._rec_encoder is None:
                return {"active": False}
            return {
                "active": True,
                "file": self._rec_name,
                "label": self._rec_label,
                "elapsed_s": round(time.monotonic() - self._rec_started, 1),
            }

    def active_recording_file(self):
        with self._rec_lock:
            return self._rec_name

    # ---- stills ----

    def capture_jpeg(self) -> bytes:
        from PIL import Image

        arr = self.picam2.capture_array("main")
        rgb = yuv420_to_rgb(arr, MAIN_SIZE)
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="jpeg", quality=93)
        return buf.getvalue()

    def close(self):
        try:
            if self._rec_encoder is not None:
                self.stop_recording()
        finally:
            self.picam2.stop_encoder()
            self.picam2.stop()
            self.picam2.close()
