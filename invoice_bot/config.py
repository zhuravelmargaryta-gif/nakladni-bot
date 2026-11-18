"""Налаштування бота з файлу .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _ids(value: str | None) -> frozenset[int]:
    result = set()
    for part in (value or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return frozenset(result)


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "так", "on")


@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    our_company_code: str = ""
    allowed_users: frozenset[int] = field(default_factory=frozenset)
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    db_path: str = "invoices.db"
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_user: str = ""
    odoo_api_key: str = ""
    odoo_auto_sync: bool = True
    odoo_partner_code_field: str = "company_registry"
    odoo_purchase_tax_id: int | None = None
    odoo_journal_id: int | None = None
    odoo_expense_account_id: int | None = None

    @property
    def odoo_enabled(self) -> bool:
        return all([self.odoo_url, self.odoo_db, self.odoo_user, self.odoo_api_key])

    def is_allowed(self, user_id: int) -> bool:
        return not self.allowed_users or user_id in self.allowed_users or user_id in self.admin_ids

    def is_admin(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Config":
        load_dotenv(env_file or BASE_DIR / ".env", override=False)
        g = os.getenv
        db_path = (g("DB_PATH") or "invoices.db").strip()
        if not os.path.isabs(db_path):
            db_path = str(BASE_DIR / db_path)
        return cls(
            bot_token=(g("BOT_TOKEN") or "").strip(),
            gemini_api_key=(g("GEMINI_API_KEY") or "").strip(),
            gemini_model=(g("GEMINI_MODEL") or "gemini-2.5-flash").strip(),
            our_company_code="".join(ch for ch in (g("OUR_COMPANY_CODE") or "") if ch.isdigit()),
            allowed_users=_ids(g("ALLOWED_USERS")),
            admin_ids=_ids(g("ADMIN_IDS")),
            db_path=db_path,
            odoo_url=(g("ODOO_URL") or "").strip().rstrip("/"),
            odoo_db=(g("ODOO_DB") or "").strip(),
            odoo_user=(g("ODOO_USER") or "").strip(),
            odoo_api_key=(g("ODOO_API_KEY") or "").strip(),
            odoo_auto_sync=_bool(g("ODOO_AUTO_SYNC"), True),
            odoo_partner_code_field=(g("ODOO_PARTNER_CODE_FIELD") or "company_registry").strip(),
            odoo_purchase_tax_id=_int_or_none(g("ODOO_PURCHASE_TAX_ID")),
            odoo_journal_id=_int_or_none(g("ODOO_JOURNAL_ID")),
            odoo_expense_account_id=_int_or_none(g("ODOO_EXPENSE_ACCOUNT_ID")),
        )
