"""Експорт накладних у .xlsx (openpyxl): справжні дати й числа, закріплена шапка, автофільтр."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import event_types
from .cards import status_label, user_display
from .db import Database, InvoiceFilter
from .parsing import iso_to_date

MONEY = "#,##0.00"
DATE = "DD.MM.YYYY"
DATETIME = "DD.MM.YYYY HH:MM"
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
ODOO_STATUS = {"": "не відправлено", "sent": "відправлено", "modified": "змінено після відправки", "error": "помилка"}


def _dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sheet(wb: Workbook, title: str, headers: list[tuple[str, int, str | None]], rows: list[list], first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append([h for h, _, _ in headers])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = HEADER_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for idx, (_, width, fmt) in enumerate(headers, 1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = width
        if fmt:
            for cell in ws[letter][1:]:
                cell.number_format = fmt
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(rows) + 1)}"
    return ws


def build_workbook(db: Database, flt: InvoiceFilter | None) -> tuple[bytes, int]:
    invoices = db.list_invoices(flt)
    ids = [i["id"] for i in invoices]
    by_id = {i["id"]: i for i in invoices}
    wb = Workbook()

    inv_headers = [("ID", 7, None), ("Номер", 14, None), ("Дата виписки", 13, DATE), ("Контрагент", 40, None),
                   ("ЄДРПОУ/ІПН", 14, None), ("Сума з ПДВ", 15, MONEY), ("ПДВ", 13, MONEY), ("Валюта", 8, None),
                   ("Поточний статус", 30, None), ("Дата статусу", 13, DATE), ("Надіслано в бот", 17, DATETIME),
                   ("Хто надіслав", 24, None), ("Постачальник", 34, None), ("Код постачальника", 14, None),
                   ("Покупець", 34, None), ("Код покупця", 14, None), ("Статус Odoo", 22, None), ("ID в Odoo", 10, None)]
    rows = []
    for i in invoices:
        rows.append([
            i["id"], i["number"], iso_to_date(i["invoice_date"]), i["counterparty_name"], i["counterparty_code"],
            i["total"], i["vat"], i["currency"] or "UAH",
            status_label(i["status"]) if i.get("status") else "Без подій", iso_to_date(i.get("status_date")),
            _dt(i["sent_at"]), user_display(i["user_name"], i["user_username"]),
            i["supplier_name"], i["supplier_code"], i["buyer_name"], i["buyer_code"],
            ODOO_STATUS.get(i["odoo_status"] or "", i["odoo_status"]), i["odoo_id"],
        ])
    ws = _sheet(wb, "Накладні", inv_headers, rows, first=True)
    total_row = len(rows) + 2
    ws.cell(total_row, 2, "РАЗОМ").font = Font(bold=True)
    for col, key in ((6, "total"), (7, "vat")):
        c = ws.cell(total_row, col, round(sum(i[key] or 0 for i in invoices), 2))
        c.number_format = MONEY
        c.font = Font(bold=True)

    item_headers = [("ID накладної", 9, None), ("Номер накладної", 14, None), ("Дата виписки", 13, DATE),
                    ("Контрагент", 34, None), ("№", 5, None), ("Товар", 50, None), ("Кількість", 11, "#,##0.###"),
                    ("Од.", 7, None), ("Ціна", 13, MONEY), ("Сума", 14, MONEY)]
    item_rows = []
    for it in db.items_for(ids):
        inv = by_id[it["invoice_id"]]
        item_rows.append([inv["id"], inv["number"], iso_to_date(inv["invoice_date"]), inv["counterparty_name"],
                          it["position"], it["name"], it["quantity"], it["unit"], it["price"], it["total"]])
    _sheet(wb, "Товари", item_headers, item_rows)

    ev_headers = [("ID накладної", 9, None), ("Номер накладної", 14, None), ("Контрагент", 34, None),
                  ("Дата події", 13, DATE), ("Подія", 36, None), ("Сума", 14, MONEY), ("Коментар", 40, None),
                  ("Вкладення", 10, None), ("Хто додав", 20, None), ("Додано", 17, DATETIME)]
    ev_rows = []
    for e in db.events_for(ids):
        inv = by_id[e["invoice_id"]]
        ev_rows.append([inv["id"], inv["number"], inv["counterparty_name"], iso_to_date(e["event_date"]),
                        event_types.get(e["event_type"]).label, e["amount"], e["comment"],
                        "так" if e["file_id"] else "", e["user_name"], _dt(e["created_at"])])
    _sheet(wb, "Рух накладних", ev_headers, ev_rows)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue(), len(invoices)
