#!/usr/bin/env python3
"""Create/update the pi-ir-cam test automations in Home Assistant.

Every hardware-specific detail — which blasters exist, their entity ids,
which TVs they aim at — lives in a local config file (default `devices.json`
next to this script), which is gitignored. `devices.example.json` documents
the schema with placeholder devices; copy it and fill in your own setup.

For each blaster in the config this creates:
  - "IR test burst - <device>"      ~3 s of continuous IR, used by pi-ir-cam's
                                    /api/calibrate and /api/test-run
  - "IR vol x100 <tv> - <device>"   volume_up at each configured TV, 100 times
and one "IR test all devices" sweep that fires every blaster in fixed slots.

Usage:
  HA_TOKEN=<long-lived access token> ./create_automations.py [--dry-run]
                                     [--config PATH]
  (HA_URL defaults to http://homeassistant.local:8123)
"""

import argparse
import base64
import json
import os
import struct
import sys
import urllib.request
from pathlib import Path

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
DEFAULT_CONFIG = Path(__file__).resolve().parent / "devices.json"


def load_config(path):
    if not path.exists():
        sys.exit(
            f"No config at {path}.\n"
            f"Copy {path.parent / 'devices.example.json'} to {path.name} "
            "and fill in your own devices."
        )
    with path.open() as fh:
        cfg = json.load(fh)
    if not cfg.get("devices"):
        sys.exit(f"{path}: no 'devices' defined")
    return cfg


def sustained_carrier_b64(seconds, mark_ms=100):
    """Raw IR packet holding a continuous 38 kHz carrier for ~`seconds`.

    (mark_ms mark, ~30 us gap) pairs — no learned command needed; usable as
    remote.send_command's `b64:` payload on any remote integration that
    accepts that packet format. Durations are in 2^-15 s ticks; values >255
    are encoded as 0x00 + 16-bit big-endian; pulse data ends with 0x0d 0x05.
    """
    ticks_per_us = 269 / 8192

    def enc(us):
        t = max(1, round(us * ticks_per_us))
        return bytes([t]) if t < 256 else b"\x00" + struct.pack(">H", t)

    pair = enc(mark_ms * 1000) + enc(30)
    payload = pair * round(seconds * 1000 / mark_ms) + b"\x0d\x05"
    packet = bytes([0x26, 0x00]) + struct.pack("<H", len(payload)) + payload
    packet += bytes((16 - len(packet) % 16) % 16)
    return base64.b64encode(packet).decode()


def sustained_raw_code(seconds):
    # Continuous 38 kHz carrier: (30 ms mark, 10 us gap) pairs. The gaps are
    # far below a camera frame, so the light reads as constant-on.
    return [30000, -10] * round(seconds * 1000 / 30)


def _press_repeatedly(entity, count, delay_ms):
    return {
        "repeat": {
            "count": count,
            "sequence": [
                {"action": "button.press", "target": {"entity_id": entity}},
                {"delay": {"milliseconds": delay_ms}},
            ],
        },
    }


def _select_slot(select_entity, option):
    return {"action": "select.select_option",
            "target": {"entity_id": select_entity}, "data": {"option": option}}


def _volume_repeat(entity, count, delay_ms):
    return {
        "repeat": {
            "count": count,
            "sequence": [
                {"action": "media_player.volume_up",
                 "target": {"entity_id": entity}},
                {"delay": {"milliseconds": delay_ms}},
            ],
        },
    }


def burst_actions(dev, seconds=3):
    """~`seconds` of continuous IR for calibration / strength measurement."""
    kind, entity, opts = dev["kind"], dev["entity"], dev.get("options", {})

    if kind == "remote_raw":
        # Raw sustained-carrier packet — no learned command required.
        return [{
            "action": "remote.send_command",
            "target": {"entity_id": entity},
            "data": {"command": "b64:" + sustained_carrier_b64(seconds)},
        }]
    if kind == "remote_learned":
        # For devices that drop raw b64 sends: replay a learned frame
        # back-to-back. The device ACKs after each transmit, so repeats
        # self-pace at roughly frame duration.
        return [{
            "action": "remote.send_command",
            "target": {"entity_id": entity},
            "data": {"device": opts["learn_device"],
                     "command": opts["learn_command"],
                     "num_repeats": opts.get("repeats_per_second", 5) * seconds,
                     "delay_secs": 0},
        }]
    if kind == "select_and_press":
        # Replays a learned signal slot (no raw injection): pick the slot,
        # then press Send. For a near-continuous burst, learn a long signal
        # into the burst slot.
        return [_select_slot(opts["select_entity"], opts["slots"]["burst"]),
                _press_repeatedly(entity, 10 * seconds, 100)]
    if kind == "raw_action":
        return [{
            "action": opts["raw_action"],
            "data": {"code": sustained_raw_code(seconds),
                     "carrier_frequency": opts.get("carrier_frequency", 38000),
                     **opts.get("raw_data", {})},
        }]
    if kind == "media_player":
        return [_volume_repeat(entity, 5 * seconds, 200)]
    sys.exit(f"{dev['name']}: unknown kind {kind!r}")


def vol100_actions(dev, tv):
    """100x volume_up at `tv`, sent from `dev`."""
    kind, entity, opts = dev["kind"], dev["entity"], dev.get("options", {})
    tv_slug = tv["slug"]

    if kind in ("remote_raw", "remote_learned"):
        return [{
            "action": "remote.send_command",
            "target": {"entity_id": entity},
            "data": {"device": opts.get("tv_command_devices", {}).get(tv_slug, tv_slug),
                     "command": "volume_up",
                     "num_repeats": 100, "delay_secs": 0.15},
        }]
    if kind == "select_and_press":
        return [_select_slot(opts["select_entity"], opts["slots"][tv_slug]),
                _press_repeatedly(entity, 100, 150)]
    if kind == "raw_action":
        # A TV reachable through a media_player proxy takes volume_up
        # directly; anything else needs its own raw/NEC codes.
        proxy = opts.get("tv_media_players", {}).get(tv_slug)
        if proxy:
            return [_volume_repeat(proxy, 100, 150)]
        codes = opts.get("tv_codes", {}).get(tv_slug)
        if codes is None:
            sys.exit(f"{dev['name']}: no tv_codes or tv_media_players for {tv_slug!r}")
        return [{
            "repeat": {
                "count": 100,
                "sequence": [
                    {"action": opts["code_action"], "data": codes},
                    {"delay": {"milliseconds": 150}},
                ],
            },
        }]
    if kind == "media_player":
        return [_volume_repeat(entity, 100, 150)]
    sys.exit(f"{dev['name']}: unknown kind {kind!r}")


def automation(object_id, alias, description, webhook_id, actions):
    return object_id, {
        "alias": alias,
        "description": description,
        "triggers": [{
            "trigger": "webhook",
            "webhook_id": webhook_id,
            "local_only": True,
            "allowed_methods": ["POST"],
        }],
        "conditions": [],
        "actions": actions,
        "mode": "single",
    }


def _tolerant(actions):
    """Mark actions continue_on_error so one offline device can't abort a sweep."""
    out = []
    for a in actions:
        a = dict(a)
        if "action" in a or "repeat" in a:
            a["continue_on_error"] = True
        out.append(a)
    return out


def send_all_automation(devices, seconds, gap_s):
    actions, order, t = [], [], 0
    for dev in devices:
        order.append(f"{dev['name']} at {t}-{t + seconds}s")
        # parallel with a fixed delay = the slot always lasts `seconds`,
        # whether the device transmits, errors instantly, or is
        # fire-and-forget — so recording time maps to device deterministically.
        actions.append({"parallel": [
            {"sequence": _tolerant(burst_actions(dev, seconds))},
            {"delay": {"seconds": seconds}},
        ]})
        actions.append({"delay": {"seconds": gap_s}})
        t += seconds + gap_s
    return automation(
        "pi_ir_cam_test_all",
        "IR test all devices",
        f"pi-ir-cam sweep: the same {seconds} s test signal from every "
        f"blaster in fixed {seconds + gap_s} s slots "
        f"(schedule holds even if a device errors): {'; '.join(order)}. "
        f"Total ~{t} s — record it with /api/test-run duration_s={t + 3}. "
        "Steps continue on error so offline devices don't abort the sweep.",
        "ir-test-all",
        actions,
    ), t


def build_all(cfg):
    devices, tvs = cfg["devices"], cfg.get("tvs", [])
    sweep = cfg.get("sweep", {})
    seconds = sweep.get("seconds", 10)
    gap_s = sweep.get("gap_seconds", 2)

    autos = []
    for dev in devices:
        slug, name = dev["slug"], dev["name"]
        note = dev.get("note", "")
        autos.append(automation(
            f"pi_ir_cam_test_{slug.replace('-', '_')}",
            f"IR test burst - {name}",
            f"pi-ir-cam strength test: ~3 s continuous IR from the {name}. "
            f"Used as trigger_url by /api/calibrate and /api/test-run. {note}",
            f"ir-test-{slug}",
            burst_actions(dev),
        ))
        for tv in tvs:
            autos.append(automation(
                f"pi_ir_cam_vol100_{tv['slug'].replace('-', '_')}_"
                f"{slug.replace('-', '_')}",
                f"IR vol x100 {tv['name']} - {name}",
                f"pi-ir-cam functional test: send volume_up to the {tv['name']} "
                f"100 times from the {name}. {tv.get('verify', '')} {note}",
                f"ir-vol100-{tv['slug']}-{slug}",
                vol100_actions(dev, tv),
            ))
    autos.append(send_all_automation(devices, seconds, gap_s)[0])
    return autos


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help=f"device config JSON (default: {DEFAULT_CONFIG.name})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the automations instead of posting them")
    args = ap.parse_args()

    cfg = load_config(args.config)
    autos = build_all(cfg)

    if args.dry_run:
        for object_id, body in autos:
            print(f"--- {object_id}\n{json.dumps(body, indent=2)}")
        print(f"\n{len(autos)} automations")
        return

    token = os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("Set HA_TOKEN to a long-lived access token")
    for object_id, body in autos:
        req = urllib.request.Request(
            f"{HA_URL}/api/config/automation/config/{object_id}",
            data=json.dumps({"id": object_id, **body}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"{object_id}: {resp.status} {json.loads(resp.read()).get('result')}")
    print(f"\n{len(autos)} automations created/updated.")
    print("Webhook URLs look like: "
          f"{HA_URL}/api/webhook/ir-test-{cfg['devices'][0]['slug']}")


if __name__ == "__main__":
    main()
