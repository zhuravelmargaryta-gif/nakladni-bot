"""SQLite-сховище: накладні, товари, історія змін, рух накладної, фільтри користувачів.

Схема описана декларативно (TABLES) — при старті відсутні таблиці та колонки
додаються автоматично, тож стара база після оновлення бота не ламається.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable

from . import parsing

# ---------------------------------------------------------------- схема

TABLES: dict[str, list[tuple[str, str]]] = {
    "invoices": [
        ("number", "TEXT DEFAULT ''"),
        ("invoice_date", "TEXT DEFAULT ''"),          # YYYY-MM-DD
        ("sent_at", "TEXT DEFAULT ''"),               # YYYY-MM-DDTHH:MM:SS (місцевий час)
        ("user_id", "INTEGER"),
        ("user_name", "TEXT DEFAULT ''"),
        ("user_username", "TEXT DEFAULT ''"),
        ("counterparty_name", "TEXT DEFAULT ''"),
        ("counterparty_code", "TEXT DEFAULT ''"),
        ("supplier_name", "TEXT DEFAULT ''"),
        ("supplier_code", "TEXT DEFAULT ''"),
        ("buyer_name", "TEXT DEFAULT ''"),
        ("buyer_code", "TEXT DEFAULT ''"),
        ("total", "REAL"),
        ("vat", "REAL"),
        ("currency", "TEXT DEFAULT 'UAH'"),
        ("photo_file_id", "TEXT DEFAULT ''"),
        ("photo_kind", "TEXT DEFAULT ''"),            # photo | document
        ("photo_mime", "TEXT DEFAULT ''"),
        ("photo_name", "TEXT DEFAULT ''"),
        ("is_duplicate", "INTEGER DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("deleted_by", "TEXT"),
        ("odoo_id", "INTEGER"),
        ("odoo_status", "TEXT DEFAULT ''"),           # '' | sent | modified | error
        ("odoo_error", "TEXT DEFAULT ''"),
        ("odoo_synced_at", "TEXT"),
        ("raw_json", "TEXT DEFAULT ''"),
    ],
    "invoice_items": [
        ("invoice_id", "INTEGER NOT NULL"),
        ("position", "INTEGER DEFAULT 0"),
        ("name", "TEXT DEFAULT ''"),
        ("quantity", "REAL"),
        ("unit", "TEXT DEFAULT ''"),
        ("price", "REAL"),
        ("total", "REAL"),
    ],
    "invoice_history": [
        ("invoice_id", "INTEGER NOT NULL"),
        ("action", "TEXT NOT NULL"),
        ("field", "TEXT DEFAULT ''"),
        ("old_value", "TEXT DEFAULT ''"),
        ("new_value", "TEXT DEFAULT ''"),
        ("user_id", "INTEGER"),
        ("user_name", "TEXT DEFAULT ''"),
        ("created_at", "TEXT NOT NULL"),
    ],
    "invoice_events": [
        ("invoice_id", "INTEGER NOT NULL"),
        ("event_type", "TEXT NOT NULL"),
        ("event_date", "TEXT NOT NULL"),
        ("amount", "REAL"),
        ("comment", "TEXT DEFAULT ''"),
        ("file_id", "TEXT DEFAULT ''"),
        ("file_kind", "TEXT DEFAULT ''"),
        ("user_id", "INTEGER"),
        ("user_name", "TEXT DEFAULT ''"),
        ("created_at", "TEXT NOT NULL"),
        ("deleted_at", "TEXT"),
        ("deleted_by", "TEXT"),
    ],
    "user_filters": [
        ("user_id", "INTEGER UNIQUE"),
        ("data", "TEXT DEFAULT '{}'"),
    ],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_inv_date ON invoices(invoice_date)",
    "CREATE INDEX IF NOT EXISTS ix_inv_code ON invoices(counterparty_code)",
    "CREATE INDEX IF NOT EXISTS ix_items_inv ON invoice_items(invoice_id)",
    "CREATE INDEX IF NOT EXISTS ix_hist_inv ON invoice_history(invoice_id)",
    "CREATE INDEX IF NOT EXISTS ix_ev_inv ON invoice_events(invoice_id)",
]

# Поля, які можна редагувати: ключ → (назва, тип)
EDITABLE_FIELDS: dict[str, tuple[str, str]] = {
    "number": ("Номер", "text"),
    "invoice_date": ("Дата виписки", "date"),
    "counterparty_name": ("Контрагент", "text"),
    "counterparty_code": ("ЄДРПОУ/ІПН", "code"),
    "total": ("Сума з ПДВ", "money"),
    "vat": ("ПДВ", "money"),
}

ACTIONS = {
    "created": "➕ Додано",
    "edited": "✏️ Змінено",
    "deleted": "🗑 Видалено",
    "restored": "↩️ Відновлено",
    "event_added": "📌 Додано подію",
    "event_deleted": "❌ Видалено подію",
    "odoo_sent": "🔗 Відправлено в Odoo",
    "odoo_updated": "🔄 Оновлено в Odoo",
    "odoo_error": "⚠️ Помилка Odoo",
}

STATUS_SQL = (
    "(SELECT e.event_type FROM invoice_events e WHERE e.invoice_id = i.id AND e.deleted_at IS NULL "
    "ORDER BY e.event_date DESC, e.id DESC LIMIT 1)"
)
STATUS_DATE_SQL = STATUS_SQL.replace("SELECT e.event_type", "SELECT e.event_date")
CP_KEY_SQL = (
    "CASE WHEN COALESCE(i.counterparty_code, '') <> '' THEN 'code:' || i.counterparty_code "
    "ELSE 'name:' || ulower(TRIM(COALESCE(i.counterparty_name, ''))) END"
)
NO_STATUS = "none"


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ulower(value):
    """LOWER із підтримкою кирилиці (вбудований у SQLite розуміє лише ASCII)."""
    return value.casefold() if isinstance(value, str) else value


def counterparty_key(code: str | None, name: str | None) -> str:
    code = parsing.digits_only(code)
    if code:
        return f"code:{code}"
    return "name:" + (name or "").strip().casefold()


# ---------------------------------------------------------------- фільтр

@dataclass
class InvoiceFilter:
    cp_key: str = ""
    cp_label: str = ""
    date_from: str = ""      # YYYY-MM-DD
    date_to: str = ""
    period_label: str = ""
    status: str = ""         # ключ події, 'none' (без подій) або ''
    query: str = ""

    @property
    def is_empty(self) -> bool:
        return not any([self.cp_key, self.date_from, self.date_to, self.status, self.query])

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "InvoiceFilter":
        try:
            data = json.loads(raw or "{}")
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            return cls()

    def without(self, *names: str) -> "InvoiceFilter":
        data = asdict(self)
        for n in names:
            data[n] = ""
            if n == "cp_key":
                data["cp_label"] = ""
            if n in ("date_from", "date_to"):
                data["period_label"] = ""
        return InvoiceFilter(**data)


def build_where(flt: InvoiceFilter | None) -> tuple[str, list[Any]]:
    flt = flt or InvoiceFilter()
    clauses = ["i.deleted_at IS NULL"]
    params: list[Any] = []
    if flt.cp_key:
        clauses.append(f"({CP_KEY_SQL}) = ?")
        params.append(flt.cp_key)
    if flt.date_from:
        clauses.append("i.invoice_date >= ?")
        params.append(flt.date_from)
    if flt.date_to:
        clauses.append("i.invoice_date <> '' AND i.invoice_date <= ?")
        params.append(flt.date_to)
    if flt.status == NO_STATUS:
        clauses.append(f"{STATUS_SQL} IS NULL")
    elif flt.status:
        clauses.append(f"{STATUS_SQL} = ?")
        params.append(flt.status)
    if flt.query:
        q = flt.query.strip().casefold()
        q_digits = parsing.digits_only(q)
        sub = [
            "instr(ulower(i.number), ?) > 0",
            "instr(ulower(i.counterparty_name), ?) > 0",
            "instr(ulower(i.supplier_name), ?) > 0",
            "instr(ulower(i.buyer_name), ?) > 0",
        ]
        params += [q, q, q, q]
        if q_digits and len(q_digits) >= 3:
            sub += ["instr(i.counterparty_code, ?) > 0"]
            params.append(q_digits)
        clauses.append("(" + " OR ".join(sub) + ")")
    return " AND ".join(clauses), params


ORDER_SQL = (
    "ORDER BY CASE WHEN COALESCE(i.invoice_date, '') = '' THEN 1 ELSE 0 END, "
    "i.invoice_date DESC, i.id DESC"
)


# ---------------------------------------------------------------- база

class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("ulower", 1, _ulower, deterministic=True)
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- службове
    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, list(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, list(params))
            self.conn.commit()
            return cur.lastrowid

    def migrate(self) -> None:
        with self._lock:
            for table, columns in TABLES.items():
                self.conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns:
                    if name not in existing:
                        # SQLite не дозволяє ADD COLUMN з UNIQUE/NOT NULL без default — спрощуємо
                        safe = decl.replace(" UNIQUE", "").replace(" NOT NULL", "")
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {safe}")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_filters_user ON user_filters(user_id)")
            for sql in INDEXES:
                self.conn.execute(sql)
            self.conn.commit()

    # -- історія
    def log(self, invoice_id: int, action: str, user: dict | None, field_name: str = "",
            old: Any = "", new: Any = "") -> None:
        user = user or {}
        self._exec(
            "INSERT INTO invoice_history (invoice_id, action, field, old_value, new_value, user_id, user_name, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, action, field_name, "" if old is None else str(old), "" if new is None else str(new),
             user.get("id"), user.get("name", ""), now_str()),
        )

    def history(self, invoice_id: int) -> list[dict]:
        return self._q("SELECT * FROM invoice_history WHERE invoice_id = ? ORDER BY created_at, id", (invoice_id,))

    def journal(self, limit: int = 20) -> list[dict]:
        return self._q(
            "SELECT h.*, i.number AS invoice_number, i.counterparty_name AS invoice_cp "
            "FROM invoice_history h LEFT JOIN invoices i ON i.id = h.invoice_id "
            "ORDER BY h.created_at DESC, h.id DESC LIMIT ?",
            (limit,),
        )

    # -- накладні
    def add_invoice(self, data: dict, items: list[dict], user: dict) -> int:
        cols = [c for c, _ in TABLES["invoices"] if c in data]
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO invoices ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [data[c] for c in cols],
            )
            inv_id = cur.lastrowid
            for pos, item in enumerate(items or [], 1):
                self.conn.execute(
                    "INSERT INTO invoice_items (invoice_id, position, name, quantity, unit, price, total)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (inv_id, pos, item.get("name") or "", item.get("quantity"), item.get("unit") or "",
                     item.get("price"), item.get("total")),
                )
            self.conn.commit()
        self.log(inv_id, "created", user)
        return inv_id

    def get_invoice(self, invoice_id: int) -> dict | None:
        return self._one(
            f"SELECT i.*, {STATUS_SQL} AS status, {STATUS_DATE_SQL} AS status_date FROM invoices i WHERE i.id = ?",
            (invoice_id,),
        )

    def items(self, invoice_id: int) -> list[dict]:
        return self._q("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,))

    def items_for(self, invoice_ids: list[int]) -> list[dict]:
        if not invoice_ids:
            return []
        out: list[dict] = []
        for chunk in _chunks(invoice_ids, 500):
            out += self._q(
                f"SELECT * FROM invoice_items WHERE invoice_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY invoice_id, position, id", chunk)
        return out

    def find_duplicates(self, number: str, code: str, name: str, exclude_id: int | None = None) -> list[dict]:
        number = (number or "").strip()
        if not number:
            return []
        return self._q(
            f"SELECT i.* FROM invoices i WHERE i.deleted_at IS NULL AND ulower(TRIM(i.number)) = ? "
            f"AND ({CP_KEY_SQL}) = ? AND i.id <> ? ORDER BY i.id",
            (number.casefold(), counterparty_key(code, name), exclude_id or 0),
        )

    def update_field(self, invoice_id: int, field_name: str, value: Any, user: dict) -> tuple[Any, Any]:
        if field_name not in EDITABLE_FIELDS:
            raise ValueError(f"Поле {field_name} не можна редагувати")
        inv = self.get_invoice(invoice_id)
        if not inv:
            raise LookupError(invoice_id)
        old = inv.get(field_name)
        if old == value:
            return old, value
        sets = [f"{field_name} = ?"]
        params: list[Any] = [value]
        if inv.get("odoo_status") == "sent":
            sets.append("odoo_status = 'modified'")
        self._exec(f"UPDATE invoices SET {', '.join(sets)} WHERE id = ?", params + [invoice_id])
        self.log(invoice_id, "edited", user, field_name, old, value)
        return old, value

    def soft_delete(self, invoice_id: int, user: dict) -> None:
        self._exec("UPDATE invoices SET deleted_at = ?, deleted_by = ? WHERE id = ? AND deleted_at IS NULL",
                   (now_str(), user.get("name", ""), invoice_id))
        self.log(invoice_id, "deleted", user)

    def restore(self, invoice_id: int, user: dict) -> None:
        self._exec("UPDATE invoices SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (invoice_id,))
        self.log(invoice_id, "restored", user)

    # -- списки та фільтри
    def list_invoices(self, flt: InvoiceFilter | None = None, offset: int = 0, limit: int | None = None) -> list[dict]:
        where, params = build_where(flt)
        sql = (f"SELECT i.*, {STATUS_SQL} AS status, {STATUS_DATE_SQL} AS status_date FROM invoices i "
               f"WHERE {where} {ORDER_SQL}")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return self._q(sql, params)

    def count_invoices(self, flt: InvoiceFilter | None = None) -> int:
        where, params = build_where(flt)
        return self._one(f"SELECT COUNT(*) AS n FROM invoices i WHERE {where}", params)["n"]

    def counterparties(self, query: str = "", offset: int = 0, limit: int | None = None) -> list[dict]:
        """Контрагенти (згруповані за ЄДРПОУ, інакше за назвою) з кількістю накладних."""
        where, params = build_where(InvoiceFilter(query=query) if query else None)
        sql = (f"SELECT {CP_KEY_SQL} AS cp_key, MAX(i.id) AS rep_id, COUNT(*) AS n, "
               f"SUM(COALESCE(i.total, 0)) AS total FROM invoices i WHERE {where} "
               f"GROUP BY cp_key ORDER BY n DESC, MAX(i.id) DESC")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        rows = self._q(sql, params)
        for r in rows:
            rep = self._one("SELECT counterparty_name, counterparty_code FROM invoices WHERE id = ?", (r["rep_id"],))
            r["name"] = (rep or {}).get("counterparty_name") or "(без назви)"
            r["code"] = (rep or {}).get("counterparty_code") or ""
        return rows

    def count_counterparties(self, query: str = "") -> int:
        where, params = build_where(InvoiceFilter(query=query) if query else None)
        return self._one(f"SELECT COUNT(DISTINCT {CP_KEY_SQL}) AS n FROM invoices i WHERE {where}", params)["n"]

    def counterparty_of(self, invoice_id: int) -> tuple[str, str]:
        """(ключ, підпис) контрагента накладної."""
        inv = self._one("SELECT counterparty_name, counterparty_code FROM invoices WHERE id = ?", (invoice_id,))
        if not inv:
            return "", ""
        key = counterparty_key(inv["counterparty_code"], inv["counterparty_name"])
        label = inv["counterparty_name"] or inv["counterparty_code"] or "(без назви)"
        return key, label

    def status_counts(self, flt: InvoiceFilter | None = None) -> dict[str, int]:
        where, params = build_where((flt or InvoiceFilter()).without("status"))
        rows = self._q(f"SELECT COALESCE({STATUS_SQL}, '{NO_STATUS}') AS st, COUNT(*) AS n FROM invoices i "
                       f"WHERE {where} GROUP BY st", params)
        return {r["st"]: r["n"] for r in rows}

    def summary(self, flt: InvoiceFilter | None = None) -> dict:
        where, params = build_where(flt)
        tot = self._one(
            f"SELECT COUNT(*) AS n, SUM(COALESCE(i.total,0)) AS total, SUM(COALESCE(i.vat,0)) AS vat "
            f"FROM invoices i WHERE {where}", params)
        top = self._q(
            f"SELECT {CP_KEY_SQL} AS cp_key, MAX(i.counterparty_name) AS name, MAX(i.counterparty_code) AS code, "
            f"COUNT(*) AS n, SUM(COALESCE(i.total,0)) AS total FROM invoices i WHERE {where} "
            f"GROUP BY cp_key ORDER BY total DESC LIMIT 10", params)
        events = self._q(
            f"SELECT e.event_type, COUNT(*) AS n, SUM(COALESCE(e.amount,0)) AS amount FROM invoice_events e "
            f"JOIN invoices i ON i.id = e.invoice_id WHERE e.deleted_at IS NULL AND {where} "
            f"GROUP BY e.event_type", params)
        total = round(tot["total"] or 0, 2)
        vat = round(tot["vat"] or 0, 2)
        return {
            "count": tot["n"] or 0,
            "total": total,
            "vat": vat,
            "net": round(total - vat, 2),
            "top": top,
            "events": {r["event_type"]: {"count": r["n"], "amount": round(r["amount"] or 0, 2)} for r in events},
        }

    # -- фільтри користувачів
    def get_filter(self, user_id: int) -> InvoiceFilter:
        row = self._one("SELECT data FROM user_filters WHERE user_id = ?", (user_id,))
        return InvoiceFilter.from_json(row["data"] if row else None)

    def save_filter(self, user_id: int, flt: InvoiceFilter) -> None:
        self._exec("INSERT INTO user_filters (user_id, data) VALUES (?, ?) "
                   "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data", (user_id, flt.to_json()))

    # -- рух накладної
    def add_event(self, invoice_id: int, event_type: str, event_date: str, user: dict, amount: float | None = None,
                  comment: str = "", file_id: str = "", file_kind: str = "") -> int:
        ev_id = self._exec(
            "INSERT INTO invoice_events (invoice_id, event_type, event_date, amount, comment, file_id, file_kind,"
            " user_id, user_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, event_type, event_date, amount, comment or "", file_id or "", file_kind or "",
             user.get("id"), user.get("name", ""), now_str()),
        )
        self.log(invoice_id, "event_added", user, event_type, "", _event_summary(event_type, event_date, amount))
        return ev_id

    def get_event(self, event_id: int) -> dict | None:
        return self._one("SELECT * FROM invoice_events WHERE id = ?", (event_id,))

    def events(self, invoice_id: int, include_deleted: bool = False) -> list[dict]:
        cond = "" if include_deleted else " AND deleted_at IS NULL"
        return self._q(f"SELECT * FROM invoice_events WHERE invoice_id = ?{cond} ORDER BY event_date, id",
                       (invoice_id,))

    def events_for(self, invoice_ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(invoice_ids, 500):
            out += self._q(
                f"SELECT * FROM invoice_events WHERE deleted_at IS NULL AND invoice_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY invoice_id, event_date, id", chunk)
        return out

    def delete_event(self, event_id: int, user: dict) -> dict | None:
        ev = self.get_event(event_id)
        if not ev or ev["deleted_at"]:
            return None
        self._exec("UPDATE invoice_events SET deleted_at = ?, deleted_by = ? WHERE id = ?",
                   (now_str(), user.get("name", ""), event_id))
        self.log(ev["invoice_id"], "event_deleted", user, ev["event_type"],
                 _event_summary(ev["event_type"], ev["event_date"], ev["amount"]), "")
        return ev

    # -- Odoo
    def set_odoo(self, invoice_id: int, status: str, odoo_id: int | None = None, error: str = "") -> None:
        if odoo_id is not None:
            self._exec("UPDATE invoices SET odoo_status = ?, odoo_id = ?, odoo_error = ?, odoo_synced_at = ? WHERE id = ?",
                       (status, odoo_id, error, now_str(), invoice_id))
        else:
            self._exec("UPDATE invoices SET odoo_status = ?, odoo_error = ? WHERE id = ?", (status, error, invoice_id))

    def invoices_for_sync(self) -> list[dict]:
        return self._q(
            "SELECT i.* FROM invoices i WHERE i.deleted_at IS NULL AND ("
            " (COALESCE(i.odoo_status, '') = '' AND COALESCE(i.is_duplicate, 0) = 0)"
            " OR i.odoo_status IN ('modified', 'error')) ORDER BY i.id")


def _event_summary(event_type: str, event_date: str, amount: float | None) -> str:
    from . import event_types
    s = f"{event_types.get(event_type).label} · {parsing.fmt_date(event_date)}"
    if amount is not None:
        s += f" · {parsing.fmt_money(amount)}"
    return s


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def today_iso() -> str:
    return date.today().isoformat()
