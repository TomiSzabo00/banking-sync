"""
webhooks.py — Dispatch events to registered HTTP endpoints.

Payload structure for every event:
{
  "event":     "new_transaction" | "salary_detected" | "sync_completed" | "auth_required",
  "timestamp": "2025-01-15T14:30:00Z",
  "data":      { ... event-specific payload ... }
}

If an endpoint has a `secret` configured, the request also carries:
  X-Bank-Signature: <HMAC-SHA256 hex of the raw JSON body using the secret>
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import requests

import db

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


def fire(event: str, data: dict) -> None:
    """
    Find all webhooks subscribed to `event` and deliver the payload.
    Failures are logged but never raise — sync must not be interrupted by a
    flaky downstream service.
    """
    endpoints = db.get_webhooks(event=event)
    if not endpoints:
        return

    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    body = json.dumps(payload, default=str)

    for endpoint in endpoints:
        _deliver(endpoint, event, body)


def _deliver(endpoint: dict, event: str, body: str) -> None:
    url = endpoint["url"]
    secret = endpoint.get("secret")
    webhook_id = endpoint["id"]

    headers = {"Content-Type": "application/json", "X-Bank-Event": event}
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Bank-Signature"] = sig

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
        logger.info("Webhook %s → %s: HTTP %s", event, url, resp.status_code)
        db.log_delivery(webhook_id, event, body, resp.status_code, None)
    except requests.RequestException as exc:
        logger.warning("Webhook %s → %s failed: %s", event, url, exc)
        db.log_delivery(webhook_id, event, body, None, str(exc))


# ── Convenience fire functions ────────────────────────────────────────────────

def fire_new_transaction(tx: dict) -> None:
    # Strip raw_json to keep webhook payload clean
    clean = {k: v for k, v in tx.items() if k != "raw_json"}
    fire("new_transaction", clean)


def fire_salary_detected(tx: dict) -> None:
    clean = {k: v for k, v in tx.items() if k != "raw_json"}
    fire("salary_detected", {"transaction": clean})


def fire_sync_completed(account_uid: str, new_count: int, total_fetched: int) -> None:
    fire("sync_completed", {
        "account_uid": account_uid,
        "new_transactions": new_count,
        "total_fetched": total_fetched,
    })


def fire_auth_required() -> None:
    fire("auth_required", {
        "message": "Enable Banking session has expired. Visit /auth/start to re-authenticate.",
    })
