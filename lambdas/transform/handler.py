"""
Transform Lambda
----------------
Triggered by SQS (ingest queue).
Fetches enterpriseId and deviceType from SSM (cached per container),
converts the raw DM payload to N2N-DL format (passthrough), and
enqueues for delivery.
"""
from __future__ import annotations

import json
import os

import boto3

from n2n_common import build_n2n_payload, log_info, log_error, log_debug

sqs = boto3.client("sqs")
ssm = boto3.client("ssm")

DELIVER_QUEUE_URL    = os.environ["DELIVER_QUEUE_URL"]
ENTERPRISE_ID_PARAM  = os.environ["N2N_ENTERPRISE_ID_PARAM"]
DEVICE_TYPE_PARAM    = os.environ["N2N_DEVICE_TYPE_PARAM"]

# SSM values cached for the lifetime of the Lambda container
_enterprise_id: str | None = None
_device_type:   str | None = None


def _get_ssm(param_name: str) -> str:
    resp = ssm.get_parameter(Name=param_name, WithDecryption=False)
    return resp["Parameter"]["Value"]


def _get_config() -> tuple[str, str]:
    global _enterprise_id, _device_type
    if not _enterprise_id:
        _enterprise_id = _get_ssm(ENTERPRISE_ID_PARAM)
        log_info("Loaded enterpriseId from SSM", enterprise_id=_enterprise_id)
    if not _device_type:
        _device_type = _get_ssm(DEVICE_TYPE_PARAM)
        log_info("Loaded deviceType from SSM", device_type=_device_type)
    return _enterprise_id, _device_type


def handler(event: dict, context) -> None:
    records = event.get("Records", [])
    log_info("Transform Lambda invoked", record_count=len(records))
    for record in records:
        _process(record)


def _process(record: dict) -> None:
    message_id = record.get("messageId", "unknown")

    try:
        message = json.loads(record.get("body", "{}"))
    except json.JSONDecodeError as e:
        log_error("Invalid SQS message body", error=str(e), message_id=message_id)
        raise

    device_sn  = message.get("device_sn", "unknown")
    dm_payload = message.get("payload", {})

    log_info("Transforming", device_sn=device_sn, message_id=message_id)

    try:
        enterprise_id, device_type = _get_config()
        n2n_payload = build_n2n_payload(dm_payload, enterprise_id, device_type)
    except Exception as e:
        log_error("Transform failed", error=str(e), device_sn=device_sn)
        raise

    log_info("Transform complete",
             device_sn=device_sn,
             device_id=n2n_payload[0]["deviceId"],
             enterprise_id=n2n_payload[0]["enterpriseId"],
             device_type=n2n_payload[0]["deviceType"],
             readings=len(n2n_payload[0].get("readingList", [])))

    deliver_msg = {"device_sn": device_sn, "n2n_payload": n2n_payload, "attempt": 1}
    try:
        resp = sqs.send_message(
            QueueUrl=DELIVER_QUEUE_URL,
            MessageBody=json.dumps(deliver_msg),
            MessageGroupId=device_sn,
            MessageDeduplicationId=f"deliver-{message_id}",
        )
        log_info("Enqueued for delivery", message_id=resp["MessageId"], device_sn=device_sn)
    except Exception as e:
        log_error("Failed to enqueue", error=str(e), device_sn=device_sn)
        raise
