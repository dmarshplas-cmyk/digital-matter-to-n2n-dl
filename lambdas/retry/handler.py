"""
Retry Lambda
------------
Triggered by the Delivery DLQ (EventSourceMapping with batch size 1).
Implements exponential backoff with up to MAX_ATTEMPTS total attempts.

Flow:
  attempt < MAX_ATTEMPTS → re-POST, then re-enqueue or succeed
  attempt >= MAX_ATTEMPTS → log failure, publish SNS alert, discard

This Lambda intentionally does NOT raise on final failure — the message
has already been through SQS retries on the delivery queue. We alert
and move on rather than looping forever.
"""
from __future__ import annotations

import json
import math
import os
import time

import boto3
import urllib3

from n2n_common import basic_auth_header, log_info, log_error, log_debug, log_warn

ssm = boto3.client("ssm")
sns = boto3.client("sns")
sqs = boto3.client("sqs")
http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5.0, read=15.0))

N2N_INGEST_URL = "https://www.nnnco.io/v3/api/core/devices/ingest-readings"
N2N_API_KEY_PARAM = os.environ["N2N_API_KEY_PARAM"]
DELIVER_QUEUE_URL = os.environ["DELIVER_QUEUE_URL"]
ALERT_TOPIC_ARN = os.environ["ALERT_TOPIC_ARN"]
MAX_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "5"))

_api_key_cache: str | None = None


def _get_api_key() -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    resp = ssm.get_parameter(Name=N2N_API_KEY_PARAM, WithDecryption=True)
    _api_key_cache = resp["Parameter"]["Value"]
    return _api_key_cache


def _post_to_n2n(payload: list[dict]) -> tuple[int, str]:
    api_key = _get_api_key()
    headers = {
        "Authorization": basic_auth_header(api_key),
        "Content-Type": "application/json",
    }
    resp = http.request("POST", N2N_INGEST_URL, body=json.dumps(payload).encode(), headers=headers)
    return resp.status, resp.data.decode("utf-8", errors="replace")


def _send_alert(device_sn: str, attempts: int, last_error: str) -> None:
    """Publish SNS alert when all retries are exhausted."""
    message = (
        f"DM→N2N Bridge: delivery permanently failed.\n"
        f"Device SN: {device_sn}\n"
        f"Attempts: {attempts}\n"
        f"Last error: {last_error}\n"
        f"Action required: check N2N-DL API status and DLQ in AWS console."
    )
    try:
        sns.publish(
            TopicArn=ALERT_TOPIC_ARN,
            Subject=f"[dm-n2n-bridge] Delivery failure – {device_sn}",
            Message=message,
        )
        log_info("SNS alert published", device_sn=device_sn)
    except Exception as e:
        log_error("Failed to publish SNS alert", error=str(e))


def _requeue(message: dict) -> None:
    """Put updated message (incremented attempt) back onto the delivery queue."""
    sqs.send_message(
        QueueUrl=DELIVER_QUEUE_URL,
        MessageBody=json.dumps(message),
        MessageGroupId=message.get("device_sn", "retry"),
        MessageDeduplicationId=f"retry-{message['device_sn']}-{message['attempt']}-{int(time.time())}",
    )


def handler(event: dict, context) -> None:
    records = event.get("Records", [])
    log_info("Retry Lambda invoked", record_count=len(records))

    for record in records:
        _process_record(record)


def _process_record(record: dict) -> None:
    message_id = record.get("messageId", "unknown")

    try:
        message = json.loads(record.get("body", "{}"))
    except json.JSONDecodeError as e:
        log_error("Cannot parse DLQ message", error=str(e), message_id=message_id)
        return  # Discard malformed messages

    device_sn = message.get("device_sn", "unknown")
    n2n_payload = message.get("n2n_payload", [])
    attempt = message.get("attempt", 1)

    log_info("Retry attempt", device_sn=device_sn, attempt=attempt, max=MAX_ATTEMPTS)

    if attempt >= MAX_ATTEMPTS:
        log_error("All retry attempts exhausted", device_sn=device_sn, attempt=attempt)
        _send_alert(device_sn, attempt, "Max attempts reached")
        return

    # Exponential backoff: 2^attempt seconds (capped at 30s inside Lambda timeout)
    backoff = min(2 ** attempt, 30)
    log_debug("Backing off before retry", seconds=backoff, device_sn=device_sn)
    time.sleep(backoff)

    last_error = ""
    try:
        status, response_body = _post_to_n2n(n2n_payload)
        last_error = f"HTTP {status}: {response_body[:200]}"
    except Exception as e:
        last_error = str(e)
        log_error("Retry HTTP request failed", error=last_error, device_sn=device_sn, attempt=attempt)
        # Re-enqueue for next retry
        message["attempt"] = attempt + 1
        _requeue(message)
        return

    if 200 <= status < 300:
        log_info("Retry succeeded", device_sn=device_sn, attempt=attempt, status=status)
    else:
        log_error(
            "Retry non-2xx response",
            device_sn=device_sn,
            attempt=attempt,
            status=status,
            response=response_body[:300],
        )
        next_attempt = attempt + 1
        if next_attempt >= MAX_ATTEMPTS:
            _send_alert(device_sn, next_attempt, last_error)
        else:
            message["attempt"] = next_attempt
            _requeue(message)
