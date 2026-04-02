"""
sync.py — Core polling, normalization, deduplication, and salary detection logic.

This module is called by APScheduler every N hours.
It can also be triggered manually via the /api/sync/run endpoint.
"""
import json
import logging
from datetime import date, timedelta

import db
import webhooks
from enablebanking_client import EnableBankingClient, EnableBankingError

logger = logging.getLogger(__name__)


def run_sync(config: dict) -> dict:
    """
    Main sync entry point. Pulls transactions for all known accounts,
    deduplicates, persists, fires webhooks.
    Returns a summary dict.
    """
    session = db.get_session()
    if not session:
        logger.warning("No active session — re-authentication required")
        webhooks.fire_auth_required()
        return {"error": "no_session", "message": "Re-authentication required. Visit /auth/start"}

    client = EnableBankingClient(
        application_id=config["enable_banking"]["application_id"],
        private_key_path=config["enable_banking"]["private_key_path"],
    )
    access_token = session["access_token"]
    salary_names = [n.lower() for n in config.get("salary_detection", {}).get("debtor_names", [])]
    lookback_days = config["sync"].get("initial_lookback_days", 30)
    default_currency = config.get("sync", {}).get("default_currency", "EUR")

    summary = {"accounts_synced": 0, "new_transactions": 0, "errors": []}

    # ── 1. Discover accounts if we don't have them yet ─────────────────────────
    accounts = db.get_accounts()
    if not accounts:
        logger.info("No accounts in DB — fetching from Enable Banking...")
        try:
            raw_accounts = client.get_accounts(access_token)
            for raw in raw_accounts:
                acc = _normalize_account(raw)
                db.save_account(acc)
            accounts = db.get_accounts()
            logger.info("Discovered %d account(s)", len(accounts))
        except EnableBankingError as exc:
            if exc.status_code == 401:
                db.clear_session()
                webhooks.fire_auth_required()
                return {"error": "session_expired"}
            return {"error": str(exc)}

    # ── 2. For each account, fetch and process transactions ────────────────────
    for account in accounts:
        uid = account["uid"]
        try:
            new_count, fetched = _sync_account(
                client, access_token, uid, salary_names, lookback_days, default_currency
            )
            summary["accounts_synced"] += 1
            summary["new_transactions"] += new_count
            webhooks.fire_sync_completed(uid, new_count, fetched)
            logger.info("Account %s — %d new of %d fetched", uid, new_count, fetched)
        except EnableBankingError as exc:
            logger.error("Sync error for account %s: %s", uid, exc)
            if exc.status_code == 401:
                db.clear_session()
                webhooks.fire_auth_required()
                return {"error": "session_expired"}
            summary["errors"].append({"account_uid": uid, "error": str(exc)})

    return summary


def _sync_account(
    client: EnableBankingClient,
    access_token: str,
    account_uid: str,
    salary_names: list[str],
    lookback_days: int,
    default_currency: str = "EUR",
) -> tuple[int, int]:
    """
    Fetch, normalize, deduplicate and persist transactions for one account.
    Returns (new_transaction_count, total_fetched_count).
    """
    state = db.get_sync_state(account_uid)

    # Use last booked date as cursor; fall back to lookback window on first run
    if state and state.get("last_booked_date"):
        # Overlap by 2 days to catch late-posting transactions
        date_from = date.fromisoformat(state["last_booked_date"]) - timedelta(days=2)
    else:
        date_from = date.today() - timedelta(days=lookback_days)

    date_to = date.today()

    if state and state.get("last_booked_date"):
        raw = client.get_transactions(access_token, account_uid, date_from=date_from, date_to=date_to)
    else:
        raw = client.get_transactions(access_token, account_uid)

    all_txs = raw.get("transactions", [])
    booked_txs = [t for t in all_txs if t.get("status") == "BOOK"]
    pending_txs = [t for t in all_txs if t.get("status") == "PDNG"]
    total_fetched = len(booked_txs) + len(pending_txs)

    new_count = 0
    latest_booked_date: str | None = state["last_booked_date"] if state else None

    for raw_tx in booked_txs:
        tx = _normalize_tx(raw_tx, account_uid, status="booked", default_currency=default_currency)
        tx["is_salary"] = _is_salary(tx, salary_names)
        is_new = db.upsert_transaction(tx)
        if is_new:
            new_count += 1
            webhooks.fire_new_transaction(tx)
            if tx["is_salary"]:
                webhooks.fire_salary_detected(tx)
            # Track latest booking date for next cursor
            bd = tx.get("booking_date")
            if bd and (latest_booked_date is None or bd > latest_booked_date):
                latest_booked_date = bd

    for raw_tx in pending_txs:
        tx = _normalize_tx(raw_tx, account_uid, status="pending", default_currency=default_currency)
        tx["is_salary"] = _is_salary(tx, salary_names)
        is_new = db.upsert_transaction(tx)
        if is_new:
            new_count += 1
            webhooks.fire_new_transaction(tx)
            if tx["is_salary"]:
                webhooks.fire_salary_detected(tx)

    db.update_sync_state(account_uid, last_booked_date=latest_booked_date)
    return new_count, total_fetched


def _normalize_tx(raw: dict, account_uid: str, status: str, default_currency: str = "EUR") -> dict:
    """
    Map the Enable Banking transaction JSON to our internal schema.
    Enable Banking follows the Berlin Group / NextGenPSD2 naming conventions.
    """
    amount_data = raw.get("transaction_amount", raw.get("transactionAmount", {}))
    amount = float(amount_data.get("amount", 0))
    currency = amount_data.get("currency", default_currency)

    booking_date = (
        raw.get("booking_date")
        or raw.get("bookingDate")
        or raw.get("value_date")
        or raw.get("valueDate")
    )
    value_date = raw.get("value_date") or raw.get("valueDate")

    debtor = raw.get("debtor") or {}
    creditor = raw.get("creditor") or {}

    debtor_name = debtor.get("name") or ""
    creditor_name = creditor.get("name") or ""

    reference = (
        raw.get("remittance_information", [None])[0]
        or raw.get("entry_reference")
        or ""
    )

    description = reference

    bank_id = raw.get("transaction_id") or raw.get("transactionId") or raw.get("entry_reference")

    return {
        "hash": db.make_tx_hash(amount, booking_date, debtor_name, creditor_name, reference),
        "bank_id": bank_id,
        "account_uid": account_uid,
        "amount": amount,
        "currency": currency,
        "status": status,
        "booking_date": booking_date,
        "value_date": value_date,
        "debtor_name": debtor_name or None,
        "creditor_name": creditor_name or None,
        "reference": reference or None,
        "description": description or None,
        "raw_json": json.dumps(raw),
    }


def _normalize_account(raw: dict) -> dict:
    uid = raw.get("uid") or raw.get("resource_id") or raw.get("resourceId") or raw.get("id")
    iban_data = raw.get("account_id", raw.get("accountId", {})) or {}
    return {
        "uid": uid,
        "iban": iban_data.get("iban") or raw.get("iban"),
        "currency": raw.get("currency"),
        "name": raw.get("name") or raw.get("product"),
        "raw_json": json.dumps(raw),
    }


def _is_salary(tx: dict, salary_names: list[str]) -> bool:
    if not salary_names:
        return False
    debtor = (tx.get("debtor_name") or "").lower()
    return any(name in debtor for name in salary_names)
