"""Інтеграція з Odoo через стандартний XML-RPC API.

Усі методи блокуючі — у боті їх викликають через asyncio.to_thread, щоб не гальмувати чат.
"""

from __future__ import annotations

import base64
import http.client
import logging
import xmlrpc.client
from dataclasses import dataclass

log = logging.getLogger(__name__)


class OdooError(Exception):
    """Зрозуміла помилка Odoo (текст можна показати користувачу)."""


def _fault_text(exc: Exception) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        lines = [ln.strip() for ln in str(exc.faultString).strip().splitlines() if ln.strip()]
        return lines[-1] if lines else str(exc.faultCode)
    if isinstance(exc, xmlrpc.client.ProtocolError):
        return f"HTTP {exc.errcode} {exc.errmsg}"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return f"немає з'єднання з сервером Odoo ({exc.__class__.__name__}: {exc})"
    return str(exc) or exc.__class__.__name__


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class OdooClient:
    def __init__(self, url: str, db: str, user: str, api_key: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.timeout = timeout
        self._uid: int | None = None

    def _proxy(self, endpoint: str) -> xmlrpc.client.ServerProxy:
        transport = (_TimeoutSafeTransport if self.url.startswith("https") else _TimeoutTransport)(self.timeout)
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/{endpoint}", allow_none=True, transport=transport)

    def version(self) -> dict:
        try:
            return self._proxy("common").version()
        except Exception as exc:
            raise OdooError(_fault_text(exc)) from exc

    @property
    def uid(self) -> int:
        if self._uid is None:
            try:
                uid = self._proxy("common").authenticate(self.db, self.user, self.api_key, {})
            except Exception as exc:
                raise OdooError(_fault_text(exc)) from exc
            if not uid:
                raise OdooError("невірний логін або API-ключ Odoo (або назва бази)")
            self._uid = uid
        return self._uid

    def call(self, model: str, method: str, *args, **kwargs):
        try:
            return self._proxy("object").execute_kw(self.db, self.uid, self.api_key, model, method, list(args), kwargs)
        except OdooError:
            raise
        except Exception as exc:
            raise OdooError(_fault_text(exc)) from exc


@dataclass
class Attachment:
    data: bytes
    filename: str
    mimetype: str


class OdooSync:
    """Бізнес-логіка: контрагенти, чернетки рахунків постачальника, нотатки."""

    def __init__(self, client: OdooClient, partner_code_field: str = "company_registry",
                 purchase_tax_id: int | None = None, journal_id: int | None = None,
                 expense_account_id: int | None = None):
        self.client = client
        self.partner_code_field = partner_code_field
        self.purchase_tax_id = purchase_tax_id
        self.journal_id = journal_id
        self.expense_account_id = expense_account_id

    @classmethod
    def from_config(cls, cfg) -> "OdooSync":
        return cls(OdooClient(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key),
                   cfg.odoo_partner_code_field, cfg.odoo_purchase_tax_id, cfg.odoo_journal_id,
                   cfg.odoo_expense_account_id)

    # ---- перевірка
    def test(self) -> str:
        ver = self.client.version()
        uid = self.client.uid
        users = self.client.call("res.users", "read", [uid], fields=["name"])
        name = users[0]["name"] if users else self.client.user
        count = self.client.call("account.move", "search_count", [["move_type", "=", "in_invoice"]])
        return (f"Odoo {ver.get('server_version', '?')}, користувач: {name} (uid {uid}), "
                f"рахунків постачальників: {count}")

    # ---- контрагенти
    def find_or_create_partner(self, name: str, code: str) -> int:
        name = (name or "").strip()
        code = (code or "").strip()
        if code:
            ids = self.client.call("res.partner", "search", [[self.partner_code_field, "=", code]], limit=1)
            if ids:
                return ids[0]
        if name:
            ids = self.client.call("res.partner", "search", [["name", "=ilike", name]], limit=1)
            if ids:
                return ids[0]
        default_name = f"Контрагент {code}" if code else "Невідомий контрагент"
        vals = {"name": name or default_name, "is_company": True}
        if code:
            vals[self.partner_code_field] = code
        return self.client.call("res.partner", "create", vals)

    # ---- рахунки
    def _lines(self, inv: dict, items: list[dict]) -> list:
        taxes = [(6, 0, [self.purchase_tax_id])] if self.purchase_tax_id else [(6, 0, [])]
        lines = []
        for it in items:
            qty = it.get("quantity") or 1
            price = it.get("price")
            if price is None:
                price = round((it.get("total") or 0) / qty, 2) if qty else (it.get("total") or 0)
            vals = {"name": it.get("name") or "Товар", "quantity": qty, "price_unit": price, "tax_ids": taxes}
            if self.expense_account_id:
                vals["account_id"] = self.expense_account_id
            lines.append((0, 0, vals))
        if not lines:
            total = inv.get("total") or 0
            if self.purchase_tax_id:
                total = round(total - (inv.get("vat") or 0), 2)
            vals = {"name": f"Накладна № {inv.get('number') or inv['id']}", "quantity": 1, "price_unit": total,
                    "tax_ids": taxes}
            if self.expense_account_id:
                vals["account_id"] = self.expense_account_id
            lines.append((0, 0, vals))
        return lines

    def push_invoice(self, inv: dict, items: list[dict], attachment: Attachment | None = None) -> tuple[int, str]:
        """Створити або оновити чернетку. Повертає (odoo_id, 'created' | 'updated')."""
        partner_id = self.find_or_create_partner(inv.get("counterparty_name"), inv.get("counterparty_code"))
        vals = {
            "partner_id": partner_id,
            "ref": inv.get("number") or False,
            "invoice_date": inv.get("invoice_date") or False,
        }
        odoo_id = inv.get("odoo_id")
        if odoo_id:
            found = self.client.call("account.move", "read", [odoo_id], fields=["state"])
            if found:
                state = found[0].get("state")
                if state != "draft":
                    raise OdooError(f"рахунок #{odoo_id} в Odoo вже не чернетка (стан: {state}) — "
                                    f"оновлювати можна лише чернетки")
                vals["invoice_line_ids"] = [(5, 0, 0)] + self._lines(inv, items)
                self.client.call("account.move", "write", [odoo_id], vals)
                return odoo_id, "updated"
            log.warning("Рахунок #%s не знайдено в Odoo — створюю новий", odoo_id)
        vals["move_type"] = "in_invoice"
        vals["invoice_line_ids"] = self._lines(inv, items)
        if self.journal_id:
            vals["journal_id"] = self.journal_id
        new_id = self.client.call("account.move", "create", vals)
        if isinstance(new_id, list):
            new_id = new_id[0]
        if attachment:
            try:
                self.client.call("ir.attachment", "create", {
                    "name": attachment.filename,
                    "datas": base64.b64encode(attachment.data).decode("ascii"),
                    "res_model": "account.move",
                    "res_id": new_id,
                    "mimetype": attachment.mimetype,
                })
            except OdooError as exc:
                log.warning("Не вдалося прикріпити фото до рахунку #%s: %s", new_id, exc)
        return new_id, "created"

    def post_note(self, odoo_id: int, text: str) -> None:
        self.client.call("account.move", "message_post", [odoo_id], body=text, message_type="comment",
                         subtype_xmlid="mail.mt_note")
