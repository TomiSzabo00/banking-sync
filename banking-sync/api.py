"""
api.py — Flask routes:

  Auth flow (browser-driven):
    GET  /auth/start          → initiates OAuth, redirects browser to the bank's OAuth page
    GET  /callback            → receives the OAuth code from the bank's redirect
    GET  /auth/status         → check if a session is active

  Sync control:
    POST /api/sync/run        → manually trigger a sync (today's transactions)
    POST /api/sync/backfill   → fetch historical transactions from a given date
    POST /api/sync/enable     → start the 4x/day auto-sync schedule
    POST /api/sync/disable    → stop auto-sync
    GET  /api/sync/status     → check if auto-sync is enabled

  Health:
    GET  /health              → liveness probe
"""
import logging
import threading
from datetime import date

from markupsafe import escape
from flask import Blueprint, current_app, jsonify, redirect, request

import session_store
import sync as sync_module
from enablebanking_client import EnableBankingClient

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__)

_pending_auth: dict = {}
_sync_lock = threading.Lock()


# ── Health ─────────────────────────────────────────────────────────────────────

@bp.get("/health")
def health():
    session = session_store.get_session()
    return jsonify({
        "status": "ok",
        "session_active": session is not None,
    })


# ── Auth flow ──────────────────────────────────────────────────────────────────

@bp.get("/auth/start")
def auth_start():
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

    return redirect(redirect_url)


@bp.get("/callback")
def oauth_callback():
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

    accounts = [sync_module._normalize_account(raw) for raw in session_data.get("accounts", [])]

    session_store.save_session(
        access_token,
        expires_at=expires_at,
        authorization_id=authorization_id,
        accounts=accounts,
    )

    logger.info("Session saved — expires_at=%s, accounts=%d", expires_at, len(accounts))

    return f"""
    <h2>Authentication Successful</h2>
    <p><strong>Session valid until:</strong> {expires_at or 'up to 90 days'}</p>
    <p>You can close this tab.</p>
    <p><a href="/auth/status">Check auth status</a></p>
    """


@bp.get("/auth/status")
def auth_status():
    session = session_store.get_session()
    if not session:
        return jsonify({"authenticated": False, "message": "No active session. Visit /auth/start"})
    return jsonify({
        "authenticated": True,
        "expires_at": session.get("expires_at"),
        "authorization_id": session.get("authorization_id"),
        "accounts": len(session.get("accounts", [])),
    })


# ── Sync control ──────────────────────────────────────────────────────────────

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


@bp.post("/api/sync/backfill")
def backfill():
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"error": "sync_already_running"}), 409
    try:
        cfg = current_app.config["APP_CONFIG"]
        date_from_str = request.args.get("date_from")
        if not date_from_str and request.is_json:
            date_from_str = request.json.get("date_from")
        date_from = date.fromisoformat(date_from_str) if date_from_str else None
        result = sync_module.run_backfill(cfg, date_from=date_from)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": f"Invalid date_from: {exc}"}), 400
    finally:
        _sync_lock.release()


@bp.post("/api/sync/enable")
def enable_auto_sync():
    from app import start_scheduler, _scheduler_ref
    cfg = current_app.config["APP_CONFIG"]
    if _scheduler_ref.get("scheduler") and _scheduler_ref["scheduler"].running:
        return jsonify({"auto_sync": "already_enabled"})
    scheduler = start_scheduler(cfg)
    _scheduler_ref["scheduler"] = scheduler
    return jsonify({"auto_sync": "enabled"})


@bp.post("/api/sync/disable")
def disable_auto_sync():
    from app import _scheduler_ref
    scheduler = _scheduler_ref.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        _scheduler_ref["scheduler"] = None
        return jsonify({"auto_sync": "disabled"})
    return jsonify({"auto_sync": "already_disabled"})


@bp.get("/api/sync/status")
def sync_status():
    from app import _scheduler_ref
    scheduler = _scheduler_ref.get("scheduler")
    return jsonify({
        "auto_sync_enabled": bool(scheduler and scheduler.running),
    })
