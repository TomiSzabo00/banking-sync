"""
api.py — Flask routes:

  Auth flow (browser-driven):
    GET  /auth/start          → initiates OAuth, redirects browser to the actual bank's OAuth page
    GET  /callback            → receives the OAuth code from the bank's redirect
    GET  /auth/status         → check if a session is active

  Data:
    GET  /api/transactions    → query transactions (filters: account, status, salary, limit, offset)
    GET  /api/accounts        → list known accounts
    GET  /api/sync/status     → last sync info per account

  Webhooks:
    GET    /api/webhooks       → list registered webhooks
    POST   /api/webhooks       → register a new webhook
    DELETE /api/webhooks/<id>  → remove a webhook

  Admin:
    POST /api/sync/run        → manually trigger a sync cycle
    GET  /health              → liveness probe
"""
import json
import logging
import threading

from markupsafe import escape
from flask import Blueprint, current_app, jsonify, redirect, request

import db
import sync as sync_module
import webhooks as wh_module
from enablebanking_client import EnableBankingClient

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__)

# Shared state for the OAuth dance (authorization_id is short-lived)
_pending_auth: dict = {}
_sync_lock = threading.Lock()


# ── Health ─────────────────────────────────────────────────────────────────────

@bp.get("/health")
def health():
    session = db.get_session()
    return jsonify({
        "status": "ok",
        "session_active": session is not None,
    })


# ── Auth flow ──────────────────────────────────────────────────────────────────

@bp.get("/auth/start")
def auth_start():
    """Initiate the OAuth flow. Visit this in a browser."""
    cfg = current_app.config["APP_CONFIG"]
    eb = cfg["enable_banking"]
    client = EnableBankingClient(
        application_id=eb["application_id"],
        private_key_path=eb["private_key_path"],
    )

    try:
        result = client.initiate_auth(
            aspsp_name=eb["aspsp_name"],
            country=eb["country"],
            redirect_url=eb["redirect_url"],
            credentials=eb.get("credentials"),
        )
    except Exception as exc:
        logger.error("Auth initiation failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    authorization_id = result.get("authorization_id")
    redirect_url = result.get("url")

    _pending_auth["authorization_id"] = authorization_id
    logger.info("Auth initiated — authorization_id=%s", authorization_id)
    logger.info("Redirecting user to Enable Banking auth URL")

    # Redirect the browser to the bank's auth page
    return redirect(redirect_url)


@bp.get("/callback")
def oauth_callback():
    """
    Enable Banking redirects here after SCA approval.
    Query params: code=<authorization_code>&state=bank-sync-oauth
    """
    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        logger.error("OAuth callback error: %s — %s", error, request.args.get("error_description"))
        return f"""
        <h2>Authentication Failed</h2>
        <p><strong>Error:</strong> {escape(error)}</p>
        <p>{escape(request.args.get('error_description', ''))}</p>
        <p><a href="/auth/start">Try again</a></p>
        """, 400

    if not code:
        return "<h2>No authorization code received.</h2>", 400

    cfg = current_app.config["APP_CONFIG"]
    client = EnableBankingClient(
        application_id=cfg["enable_banking"]["application_id"],
        private_key_path=cfg["enable_banking"]["private_key_path"],
    )

    try:
        session_data = client.exchange_code_for_session(code)
    except Exception as exc:
        logger.error("Token exchange failed: %s", exc)
        return f"<h2>Token exchange failed</h2><p>{escape(str(exc))}</p>", 500

    access_token = session_data.get("session_id")
    expires_at = session_data.get("access", {}).get("valid_until")
    authorization_id = _pending_auth.get("authorization_id")

    db.save_session(access_token, expires_at=expires_at, authorization_id=authorization_id)
    accounts = session_data.get("accounts", [])
    for raw in accounts:
        db.save_account(sync_module._normalize_account(raw))

    logger.info("Session saved — expires_at=%s, accounts=%d", expires_at, len(accounts))

    return f"""
    <h2>✅ Authentication Successful</h2>
    <p><strong>Session valid until:</strong> {expires_at or 'up to 90 days'}</p>
    <p>You can close this tab.</p>
    <p><a href="/auth/status">Check auth status</a></p>
    """


@bp.get("/auth/status")
def auth_status():
    session = db.get_session()
    if not session:
        return jsonify({"authenticated": False, "message": "No active session. Visit /auth/start"})
    return jsonify({
        "authenticated": True,
        "expires_at": session.get("expires_at"),
        "created_at": session.get("created_at"),
        "authorization_id": session.get("authorization_id"),
    })


# ── Transactions ───────────────────────────────────────────────────────────────

@bp.get("/api/transactions")
def get_transactions():
    account_uid = request.args.get("account")
    status = request.args.get("status")             # booked | pending
    is_salary_param = request.args.get("salary")    # true | false
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))

    is_salary = None
    if is_salary_param is not None:
        is_salary = is_salary_param.lower() == "true"

    txs = db.get_transactions(
        account_uid=account_uid,
        status=status,
        is_salary=is_salary,
        limit=limit,
        offset=offset,
    )
    # Strip raw_json from API responses
    for tx in txs:
        tx.pop("raw_json", None)

    return jsonify({"transactions": txs, "count": len(txs), "offset": offset})


@bp.get("/api/accounts")
def get_accounts():
    accounts = db.get_accounts()
    for acc in accounts:
        acc.pop("raw_json", None)
    return jsonify({"accounts": accounts})


@bp.get("/api/sync/status")
def sync_status():
    accounts = db.get_accounts()
    result = []
    for acc in accounts:
        state = db.get_sync_state(acc["uid"])
        result.append({
            "account_uid": acc["uid"],
            "iban": acc.get("iban"),
            "last_sync_at": state["last_sync_at"] if state else None,
            "last_booked_date": state["last_booked_date"] if state else None,
        })
    return jsonify(result)


# ── Manual sync trigger ────────────────────────────────────────────────────────

@bp.post("/api/sync/run")
def manual_sync():
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"error": "sync_already_running"}), 409
    try:
        cfg = current_app.config["APP_CONFIG"]
        result = sync_module.run_sync(cfg)
        return jsonify(result)
    finally:
        _sync_lock.release()


# ── Webhooks ───────────────────────────────────────────────────────────────────

VALID_EVENTS = {"new_transaction", "salary_detected", "sync_completed", "auth_required"}


@bp.get("/api/webhooks")
def list_webhooks():
    hooks = db.get_webhooks()
    for h in hooks:
        h.pop("secret", None)   # don't expose secrets
        h["events"] = json.loads(h["events"])
    return jsonify({"webhooks": hooks})


@bp.post("/api/webhooks")
def create_webhook():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    events = body.get("events", [])
    secret = body.get("secret")

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not events:
        return jsonify({"error": "events list is required"}), 400

    unknown = set(events) - VALID_EVENTS
    if unknown:
        return jsonify({"error": f"Unknown events: {unknown}. Valid: {VALID_EVENTS}"}), 400

    wh_id = db.add_webhook(url, events, secret)
    return jsonify({"id": wh_id, "url": url, "events": events}), 201


@bp.delete("/api/webhooks/<int:webhook_id>")
def delete_webhook(webhook_id: int):
    db.delete_webhook(webhook_id)
    return jsonify({"deleted": webhook_id})



@bp.post("/api/debug/inject-transaction")
def inject_transaction():
    """
    Inject a fake transaction through the full pipeline — normalization,
    deduplication, salary detection, and webhook firing.
    Body fields (all optional, sensible defaults provided):
      amount, currency, status, booking_date, debtor_name, description, is_salary
    """
    from datetime import date

    body = request.get_json(silent=True, force=True) or {}

    cfg = current_app.config["APP_CONFIG"]
    salary_names = [n.lower() for n in cfg.get("salary_detection", {}).get("debtor_names", [])]

    accounts = db.get_accounts()
    if not accounts:
        return jsonify({"error": "no_accounts"}), 400

    account_uid = body.get("account_uid") or accounts[0]["uid"]
    debtor_name = body.get("debtor_name", "TEST EMPLOYER")
    default_currency = cfg.get("sync", {}).get("default_currency", "EUR")
    amount = float(body.get("amount", 1000.0))
    currency = body.get("currency", default_currency)
    status = body.get("status", "booked")
    booking_date = body.get("booking_date", date.today().isoformat())
    description = body.get("description", "SALARY TEST")

    tx = {
        "hash": db.make_tx_hash(amount, booking_date, debtor_name, "", description),
        "bank_id": None,
        "account_uid": account_uid,
        "amount": amount,
        "currency": currency,
        "status": status,
        "booking_date": booking_date,
        "value_date": None,
        "debtor_name": debtor_name,
        "creditor_name": None,
        "reference": description,
        "description": description,
        "raw_json": None,
    }
    tx["is_salary"] = sync_module._is_salary(tx, salary_names)

    is_new = db.upsert_transaction(tx)
    if is_new:
        wh_module.fire_new_transaction(tx)
        if tx["is_salary"]:
            wh_module.fire_salary_detected(tx)

    return jsonify({
        "injected": is_new,
        "duplicate": not is_new,
        "is_salary": tx["is_salary"],
        "transaction": {k: v for k, v in tx.items() if k != "raw_json"},
    })
