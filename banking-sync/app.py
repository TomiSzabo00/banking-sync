"""
app.py — Entry point for Banking-Sync (lightweight/stateless mode).

Starts:
  • Flask web server (OAuth callback + sync control API)
  • APScheduler is available but NOT started by default
    — enable via POST /api/sync/enable

Sync schedule (when enabled):
  08:00  — morning catch-up
  13:30  — midday
  18:30  — late afternoon
  23:59  — end-of-day sweep
"""
import logging
import os
import threading
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask

import notifications
import session_store
import sync as sync_module
import webhooks
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

    # Init session store and webhooks
    session_path = config.get("session", {}).get("path", str(_BASE_DIR / "data" / "session.json"))
    session_store.init(session_path)
    webhooks.init(config)
    notifications.init(config)

    app.register_blueprint(api_bp)
    return app


# ── Scheduler ──────────────────────────────────────────────────────────────────

_sync_lock = threading.Lock()
_scheduler_ref: dict = {"scheduler": None}

TZ = "UTC"

SYNC_SCHEDULE = [
    ("08:00", "morning"),
    ("13:30", "midday"),
    ("18:30", "late_afternoon"),
    ("23:59", "end_of_day"),
]


def scheduled_sync(config: dict, label: str = "auto") -> None:
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
        logger.warning("enable_banking.application_id is not set — update config.yaml before authenticating")
    pk_path = eb.get("private_key_path", "")
    if pk_path and not Path(pk_path).exists():
        logger.warning("enable_banking.private_key_path does not exist: %s", pk_path)

    # Apply timezone from config
    TZ = config.get("sync", {}).get("timezone", "UTC")

    # Create app (inits session store + webhooks)
    app = create_app(config)

    # Auto-sync is OFF by default — enable via POST /api/sync/enable
    session = session_store.get_session()
    if session:
        logger.info("Active session found (expires_at=%s)", session.get("expires_at"))
    else:
        logger.info("No session found — visit http://<YOUR_IP>:8080/auth/start to authenticate")
    logger.info("Auto-sync is OFF by default. POST /api/sync/enable to start scheduled syncs.")

    # Start Flask
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)

    logger.info("Starting Flask on %s:%s", host, port)
    app.run(host=host, port=port, use_reloader=False, threaded=True)
