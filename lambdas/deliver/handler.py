"""
Deliver Lambda
--------------
Triggered by SQS (delivery queue).
1. POSTs the N2N-DL payload to the ingestion API
2. On success: done
3. On non-2xx or exception: raises so SQS retries up to maxReceiveCount,
   then drops to DLQ for the Retry Lambda.
"""
from __future__ import annotations

import json
import os

import boto3
import urllib3

from n2n_common import basic_auth_header, log_info, log_error, log_debug, log_warn

ssm = boto3.client("ssm")
http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5.0, read=10.0))

N2N_INGEST_URL = "https://www.nnnco.io/v3/api/core/devices/ingest-readings"
N2N_API_KEY_PARAM = os.environ["N2N_API_KEY_PARAM"]   # SSM parameter name

_api_key_cache: str | None = None


def _get_api_key() -> str:
    """Fetch N2N API key from SSM Parameter Store (cached per Lambda container)."""
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    try:
        resp = ssm.get_parameter(Name=N2N_API_KEY_PARAM, WithDecryption=True)
        _api_key_cache = resp["Parameter"]["Value"]
        log_info("Fetched N2N API key from SSM")
        return _api_key_cache
    except Exception as e:
        log_error("Failed to fetch N2N API key from SSM", error=str(e))
        raise


def _post_to_n2n(payload: list[dict]) -> tuple[int, str]:
    """POST payload to N2N-DL. Returns (status_code, response_body)."""
    api_key = _get_api_key()
    headers = {
        "Authorization": basic_auth_header(api_key),
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8")

    log_debug(
        "POSTing to N2N-DL",
        url=N2N_INGEST_URL,
        payload_bytes=len(body),
    )

    resp = http.request(
        "POST",
        N2N_INGEST_URL,
        body=body,
        headers=headers,
    )

    body_str = resp.data.decode("utf-8", errors="replace")
    return resp.status, body_str


def handler(event: dict, context) -> None:
    records = event.get("Records", [])
    log_info("Deliver Lambda invoked", record_count=len(records))

    for record in records:
        _process_record(record)


def _process_record(record: dict) -> None:
    message_id = record.get("messageId", "unknown")
    body_str = record.get("body", "{}")

    try:
        message = json.loads(body_str)
    except json.JSONDecodeError as e:
        log_error("Invalid delivery message body", error=str(e), message_id=message_id)
        raise

    device_sn = message.get("device_sn", "unknown")
    n2n_payload = message.get("n2n_payload", [])
    attempt = message.get("attempt", 1)

    log_info("Delivering to N2N-DL", device_sn=device_sn, attempt=attempt, message_id=message_id)

    try:
        status, response_body = _post_to_n2n(n2n_payload)
    except Exception as e:
        log_error("HTTP request to N2N-DL failed", error=str(e), device_sn=device_sn)
        raise  # Let SQS retry

    if 200 <= status < 300:
        log_info(
            "Successfully delivered to N2N-DL",
            device_sn=device_sn,
            status=status,
            attempt=attempt,
        )
    else:
        log_error(
            "Non-2xx response from N2N-DL",
            device_sn=device_sn,
            status=status,
            response=response_body[:500],
            attempt=attempt,
        )
        # Raise so SQS retries and eventually routes to DLQ
        raise RuntimeError(f"N2N-DL returned HTTP {status}: {response_body[:200]}")
