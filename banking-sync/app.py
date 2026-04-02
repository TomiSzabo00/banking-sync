"""
app.py — Entry point for Banking-Sync.

Starts:
  • Flask web server (OAuth callback + REST API)
  • APScheduler background job (4 scheduled syncs per day, configured timezone)

Sync schedule:
  08:00  — morning catch-up (transactions visible when you wake up)
  13:30  — midday
  18:30  — late afternoon
  23:59  — end-of-day sweep (full day captured before midnight)
"""
import logging
import os
import threading
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask

import db
import sync as sync_module
from api import bp as api_bp

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR = _BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "app.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Config loading ─────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("BANKING_CONFIG", str(_BASE_DIR / "config.yaml"))


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Flask app factory ──────────────────────────────────────────────────────────

def create_app(config: dict) -> Flask:
    app = Flask(__name__)
    app.secret_key = config.get("server", {}).get("secret_key", "changeme")
    app.config["APP_CONFIG"] = config
    app.register_blueprint(api_bp)

    # Seed webhooks from config.yaml into the DB (idempotent)
    _seed_webhooks_from_config(config)

    return app


def _seed_webhooks_from_config(config: dict) -> None:
    """
    On startup, ensure any webhooks declared in config.yaml exist in the DB.
    This makes config.yaml the source of truth while the DB handles runtime additions.
    """
    import json
    declared = config.get("webhooks", {}).get("endpoints", []) or []
    existing_urls = {h["url"] for h in db.get_webhooks()}

    for entry in declared:
        url = entry.get("url", "").strip()
        events = entry.get("events", [])
        secret = entry.get("secret")
        if url and url not in existing_urls:
            db.add_webhook(url, events, secret)
            logger.info("Registered webhook from config: %s → %s", events, url)


# ── Scheduler ──────────────────────────────────────────────────────────────────

_sync_lock = threading.Lock()


def scheduled_sync(config: dict, label: str = "manual") -> None:
    """Called by APScheduler. Guards against overlap with a non-blocking lock."""
    if not _sync_lock.acquire(blocking=False):
        logger.info("[%s] Sync already running — skipping", label)
        return
    try:
        logger.info("=== Sync starting [%s] ===", label)
        result = sync_module.run_sync(config)
        logger.info("=== Sync complete [%s]: %s ===", label, result)
    except Exception as exc:
        logger.error("Unhandled sync error [%s]: %s", label, exc, exc_info=True)
    finally:
        _sync_lock.release()


TZ = "UTC"  # overridden by config["sync"]["timezone"] at startup

# Fixed sync times in the configured timezone.
# Enable Banking allows a maximum of 4 fetches per 24-hour period.
SYNC_SCHEDULE = [
    ("08:00", "morning"),        # transactions visible when you wake up
    ("13:30", "midday"),         # midday sweep
    ("18:30", "late_afternoon"), # late afternoon sweep
    ("23:59", "end_of_day"),     # full-day capture before midnight
]


def start_scheduler(config: dict) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TZ)

    for time_str, label in SYNC_SCHEDULE:
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(
            func=scheduled_sync,
            kwargs={"config": config, "label": label},
            trigger=CronTrigger(hour=hour, minute=minute, timezone=TZ),
            id=f"sync_{label}",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("Scheduled sync '%s' at %s %s", label, time_str, TZ)

    scheduler.start()
    return scheduler


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = load_config()

    # Validate config
    eb = config.get("enable_banking", {})
    if eb.get("application_id") in (None, "", "YOUR_APPLICATION_ID"):
        logger.warning("enable_banking.application_id is not set \u2014 update config.yaml before authenticating")
    pk_path = eb.get("private_key_path", "")
    if pk_path and not Path(pk_path).exists():
        logger.warning("enable_banking.private_key_path does not exist: %s", pk_path)

    # Apply timezone from config
    TZ = config.get("sync", {}).get("timezone", "UTC")

    # Init DB
    db_path = config.get("database", {}).get("path", str(_BASE_DIR / "data" / "transactions.db"))
    db.init_db(db_path)
    logger.info("Database initialised at %s", db_path)

    # Start background scheduler
    scheduler = start_scheduler(config)

    # Run initial sync at startup if a session exists
    session = db.get_session()
    if session:
        logger.info("Active session found — running initial sync at startup")
        t = threading.Thread(target=scheduled_sync, kwargs={"config": config}, daemon=True)
        t.start()
    else:
        logger.info("No session found — visit http://<YOUR_IP>:8080/auth/start to authenticate")

    # Start Flask
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)

    app = create_app(config)

    logger.info("Starting Flask on %s:%s", host, port)
    try:
        # use_reloader=False is required when APScheduler runs in the same process
        app.run(host=host, port=port, use_reloader=False, threaded=True)
    finally:
        scheduler.shutdown(wait=False)
