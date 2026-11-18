"""Точка входу: python main.py"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from invoice_bot import __version__
from invoice_bot.bot import InvoiceBot
from invoice_bot.config import BASE_DIR, Config
from invoice_bot.db import Database


def setup_logging() -> None:
    logs = BASE_DIR / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(logs / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    for noisy in ("httpx", "httpcore", "google_genai", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    setup_logging()
    log = logging.getLogger("main")
    cfg = Config.load()
    if not cfg.bot_token or "ВСТАВТЕ" in cfg.bot_token:
        print("❗ Не заповнено BOT_TOKEN у файлі .env — див. README.md, крок 1.")
        return 1
    if not cfg.gemini_api_key:
        print("⚠️  Не заповнено GEMINI_API_KEY — бот працюватиме, але не розпізнаватиме фото.")
    if not cfg.allowed_users:
        print("⚠️  ALLOWED_USERS порожній — бот доступний БУДЬ-КОМУ, хто його знайде!")

    db = Database(cfg.db_path)
    recognizer = None
    if cfg.gemini_api_key:
        from invoice_bot.recognition import Recognizer
        recognizer = Recognizer(cfg.gemini_api_key, cfg.gemini_model)
    odoo = None
    if cfg.odoo_enabled:
        from invoice_bot.odoo import OdooSync
        odoo = OdooSync.from_config(cfg)

    app = InvoiceBot(cfg, db, recognizer, odoo).build()
    log.info("Бот v%s запущено. База: %s. Odoo: %s", __version__, cfg.db_path,
             "підключено" if cfg.odoo_enabled else "вимкнено")
    print("✅ Бот працює. Щоб зупинити — закрийте це вікно або натисніть Ctrl+C.")
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
