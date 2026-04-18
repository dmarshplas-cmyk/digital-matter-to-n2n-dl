"""
Ingest Lambda
-------------
Receives the raw Digital Matter webhook from API Gateway.
1. Validates the shared secret (X-DM-Secret header or Basic Auth)
2. Parses and basic-validates the JSON body
3. Saves raw payload to DynamoDB (30-day TTL)
4. Enqueues message to SQS for async transform + delivery
Returns 200 immediately to DM Device Manager so it does not retry on our side.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import boto3

from n2n_common import log_info, log_error, log_debug, log_warn, save_raw_event

sqs = boto3.client("sqs")
ssm = boto3.client("ssm")

INGEST_QUEUE_URL = os.environ["INGEST_QUEUE_URL"]
RAW_TABLE = os.environ["RAW_TABLE"]
_secret_cache: str = ""

def _get_secret() -> str:
    global _secret_cache
    if not _secret_cache:
        param = os.environ.get("DM_SECRET_PARAM", "")
        if param:
            resp = ssm.get_parameter(Name=param, WithDecryption=True)
            _secret_cache = resp["Parameter"]["Value"]
    return _secret_cache


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _validate_secret(headers: dict) -> bool:
    """
    Accept the webhook if either:
    - X-DM-Secret header matches our stored secret, OR
    - Authorization header is Basic with matching credentials.
    If DM_SECRET is empty, auth is skipped (not recommended for production).
    """
    DM_SECRET = _get_secret()
    if not DM_SECRET:
        log_warn("DM_WEBHOOK_SECRET not set – skipping auth validation")
        return True

    # Custom header approach (preferred – configure in DM Device Manager)
    secret_header = headers.get("x-dm-secret") or headers.get("X-DM-Secret", "")
    if secret_header and hmac.compare_digest(secret_header, DM_SECRET):
        return True

    # Basic auth fallback
    auth_header = headers.get("authorization") or headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, password = decoded.split(":", 1)
            if hmac.compare_digest(password, DM_SECRET):
                return True
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    request_id = context.aws_request_id
    log_info("Ingest Lambda invoked", request_id=request_id)

    # Normalise headers to lowercase keys
    raw_headers = event.get("headers") or {}
    headers = {k.lower(): v for k, v in raw_headers.items()}

    # Auth check
    if not _validate_secret(headers):
        log_error("Webhook auth failed", request_id=request_id)
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    # Parse body
    body_str = event.get("body") or ""
    try:
        payload = json.loads(body_str)
    except json.JSONDecodeError as e:
        log_error("Invalid JSON body", error=str(e), request_id=request_id)
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

    # Basic structural validation
    device = payload.get("device", {})
    device_sn = str(device.get("sn", ""))
    if not device_sn:
        log_error("Missing device.sn in payload", request_id=request_id)
        return {"statusCode": 400, "body": json.dumps({"error": "Missing device.sn"})}

    if "lat" not in payload or "lng" not in payload:
        log_warn("Payload missing lat/lng – may be non-GPS record", device_sn=device_sn)

    timestamp = payload.get("date", "")
    log_info("Valid DM webhook received", device_sn=device_sn, timestamp=timestamp)

    # Persist raw payload to DynamoDB (fire-and-forget, non-blocking on failure)
    try:
        save_raw_event(RAW_TABLE, device_sn, timestamp or str(int(time.time())), payload)
    except Exception as e:
        log_error("Failed to save raw event – continuing", error=str(e))

    # Enqueue to SQS
    message = {
        "device_sn": device_sn,
        "received_at": int(time.time() * 1000),
        "payload": payload,
    }
    try:
        resp = sqs.send_message(
            QueueUrl=INGEST_QUEUE_URL,
            MessageBody=json.dumps(message),
            MessageGroupId=device_sn,          # FIFO: per-device ordering
            MessageDeduplicationId=f"{device_sn}-{payload.get('sqn', 0)}",
        )
        log_info("Message enqueued", message_id=resp["MessageId"], device_sn=device_sn)
    except Exception as e:
        log_error("SQS send_message failed", error=str(e), device_sn=device_sn)
        # Return 500 so DM Device Manager will retry
        return {"statusCode": 500, "body": json.dumps({"error": "Queue unavailable"})}

    # Always return 200 quickly once queued
    return {"statusCode": 200, "body": json.dumps({"status": "accepted"})}
