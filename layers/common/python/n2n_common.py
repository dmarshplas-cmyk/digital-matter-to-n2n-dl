"""
Common utilities shared across all Lambda functions.
- N2N token sanitisation (mirrors the Tago.io toN2NToken logic)
- N2N device ID construction (md5 prefix + sanitised serial)
- Basic-auth header builder
- DynamoDB raw event persistence
- Structured logging
- DM payload → N2N reading list transformer (passthrough, no mapping config needed)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from base64 import b64encode
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_info(msg: str, **ctx) -> None:
    logger.info(json.dumps({"level": "INFO", "msg": msg, **ctx}))

def log_debug(msg: str, **ctx) -> None:
    logger.debug(json.dumps({"level": "DEBUG", "msg": msg, **ctx}))

def log_error(msg: str, **ctx) -> None:
    logger.error(json.dumps({"level": "ERROR", "msg": msg, **ctx}))

def log_warn(msg: str, **ctx) -> None:
    logger.warning(json.dumps({"level": "WARN", "msg": msg, **ctx}))


# ---------------------------------------------------------------------------
# N2N token sanitisation  /^[a-z0-9-]{3,}$/
# ---------------------------------------------------------------------------

def to_n2n_token(value: Any, fallback: str = "val") -> str:
    s = str(value or "").strip().lower()
    s = s.replace("_", "-")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    if len(s) < 3:
        fb = to_n2n_token(fallback, "val")
        s = (fb + "xxx")[:3]
    return s


def make_n2n_device_id(enterprise_id: str, device_serial: str) -> str:
    prefix = hashlib.md5(str(enterprise_id).encode()).hexdigest()[:5]
    safe_serial = to_n2n_token(device_serial, "dev")
    return f"{prefix}-{safe_serial}"


def basic_auth_header(api_key: str) -> str:
    encoded = b64encode(f"api:{api_key}".encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# DynamoDB — raw event persistence only (no mapping config table)
# ---------------------------------------------------------------------------

_dynamodb = None

def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
    return _dynamodb


def save_raw_event(table_name: str, device_sn: str, timestamp_iso: str, payload: dict) -> None:
    """Persist the raw DM payload with a 30-day TTL."""
    table = get_dynamodb().Table(table_name)
    ttl = int(time.time()) + (30 * 24 * 60 * 60)
    try:
        table.put_item(Item={
            "device_sn": device_sn,
            "timestamp": timestamp_iso,
            "payload": payload,
            "ttl": ttl,
        })
        log_debug("Saved raw event", device_sn=device_sn, timestamp=timestamp_iso)
    except ClientError as e:
        log_error("DynamoDB put_item failed", error=str(e), device_sn=device_sn)


# ---------------------------------------------------------------------------
# DM payload → N2N reading list (passthrough — no scale, no mapping config)
#
# Philosophy: forward everything with self-describing labels.
# Downstream systems own the semantic interpretation.
#
# Channels produced:
#   Position   : latitude, longitude, altitude, speed, heading, pos-accuracy
#   Analogues  : analogue-{id}   (raw integer value, no scale)
#   Counters   : counter-{id}    (raw integer value)
#   Digital in : input-bit-{n}   (0 or 1, decoded from the inputs bitmask)
#   Digital out: output-bit-{n}  (0 or 1, decoded from the outputs bitmask)
# ---------------------------------------------------------------------------

# Number of input/output bits to decode from the bitmask.
# DM devices typically use up to 32 bits; we scan all set bits up to this width.
_BITMASK_WIDTH = 32


def build_reading_list(dm_payload: dict) -> list[dict]:
    readings: list[dict] = []
    channel_id = 0

    def add(label: str, type_: str, value: float, unit: str = "") -> None:
        nonlocal channel_id
        readings.append({
            "channelId": channel_id,
            "type":      to_n2n_token(type_,  "sensor"),
            "label":     to_n2n_token(label,  "sensor"),
            "value":     round(float(value), 6),
            "unit":      unit,
        })
        channel_id += 1

    # ── Position (always present) ────────────────────────────────────────────
    add("latitude",    "latitude",  dm_payload["lat"],                   "deg")
    add("longitude",   "longitude", dm_payload["lng"],                   "deg")
    add("altitude",    "altitude",  dm_payload.get("alt",    0),         "m")
    add("speed",       "speed",     dm_payload.get("spd",    0) / 100,   "kmh")
    add("heading",     "heading",   dm_payload.get("head",   0),         "deg")
    add("pos-accuracy","accuracy",  dm_payload.get("posAcc", 0),         "m")

    # ── Analogues — raw passthrough ──────────────────────────────────────────
    for ch in dm_payload.get("analogues", []):
        add(f"analogue-{ch['id']}", "analogue", ch["val"])

    # ── Counters — raw passthrough ───────────────────────────────────────────
    for ct in dm_payload.get("counters", []):
        add(f"counter-{ct['id']}", "counter", ct["val"])

    # ── Digital inputs — expand bitmask into per-bit readings ────────────────
    # Only emit a reading for bits that are SET. This keeps the readingList
    # lean (no flood of zero-value readings for every possible bit position)
    # while still faithfully representing the device state.
    inputs = dm_payload.get("inputs")
    if inputs is not None:
        for bit in range(_BITMASK_WIDTH):
            if inputs & (1 << bit):
                add(f"input-bit-{bit}", "digital", 1)

    # ── Digital outputs — expand bitmask ─────────────────────────────────────
    outputs = dm_payload.get("outputs")
    if outputs is not None:
        for bit in range(_BITMASK_WIDTH):
            if outputs & (1 << bit):
                add(f"output-bit-{bit}", "digital", 1)

    return readings


def build_n2n_payload(dm_payload: dict, enterprise_id: str, device_type: str) -> list[dict]:
    """Full DM → N2N payload. Returns a list (N2N expects an array)."""
    device    = dm_payload.get("device", {})
    device_sn = str(device.get("sn", "unknown"))
    prod_id   = device.get("prod", 0)

    device_id   = make_n2n_device_id(enterprise_id, device_sn)
    device_type = to_n2n_token(device_type or f"dm-prod-{prod_id}", "dm-device")

    import dateutil.parser
    try:
        ts = int(dateutil.parser.parse(dm_payload.get("date", "")).timestamp() * 1000)
    except Exception:
        ts = int(time.time() * 1000)
        log_warn("Could not parse DM timestamp, using now")

    return [{
        "deviceId":     device_id,
        "enterpriseId": enterprise_id,
        "deviceType":   device_type,
        "ts":           ts,
        "readingList":  build_reading_list(dm_payload),
    }]
