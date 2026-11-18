"""Резервна копія бази (безпечно навіть під час роботи бота): python backup.py"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from invoice_bot.config import BASE_DIR, Config


def main() -> int:
    cfg = Config.load()
    src = Path(cfg.db_path)
    if not src.exists():
        print(f"❗ Базу не знайдено: {src}")
        return 1
    folder = BASE_DIR / "backups"
    folder.mkdir(exist_ok=True)
    dst = folder / f"{src.stem}_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    print(f"✅ Резервну копію збережено: {dst}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
