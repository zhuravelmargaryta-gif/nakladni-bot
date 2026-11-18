from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook

from invoice_bot.db import InvoiceFilter
from invoice_bot.export import build_workbook
from tests.conftest import USER, make_invoice


def test_excel_matches_summary(db):
    a = make_invoice(db, total=1200.5, vat=200.08)
    b = make_invoice(db, number="102", total=600, vat=100, code="22222222", cp="Бета")
    c = make_invoice(db, number="103", total=999, vat=1)
    db.soft_delete(c, USER)
    db.add_event(a, "paid", "2026-11-01", USER, amount=1200.5, comment="банк")
    db.add_event(b, "sent", "2026-11-02", USER)

    data, count = build_workbook(db, InvoiceFilter())
    assert count == 2
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["Накладні", "Товари", "Рух накладних"]
    ws = wb["Накладні"]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref
    rows = list(ws.iter_rows(min_row=2, max_row=3, values_only=True))
    assert {r[0] for r in rows} == {a, b}
    first = rows[0]
    assert isinstance(first[2], datetime) and first[2].date() == date(2026, 10, 5)
    assert isinstance(first[5], (int, float)) and not isinstance(first[5], bool)
    assert isinstance(first[10], datetime)
    total_row = list(ws.iter_rows(min_row=4, max_row=4, values_only=True))[0]
    s = db.summary(InvoiceFilter())
    assert total_row[1] == "РАЗОМ" and total_row[5] == s["total"] and total_row[6] == s["vat"]
    assert ws.cell(2, 6).number_format == "#,##0.00" and ws.cell(2, 3).number_format == "DD.MM.YYYY"

    items = list(wb["Товари"].iter_rows(min_row=2, values_only=True))
    assert len(items) == 4 and items[0][5] in ("Цукор", "Борошно")
    events = list(wb["Рух накладних"].iter_rows(min_row=2, values_only=True))
    assert len(events) == 2 and "Оплачено" in events[0][4] + events[1][4]


def test_excel_respects_filter(db):
    make_invoice(db, code="1", cp="А")
    make_invoice(db, code="2", cp="Б")
    data, count = build_workbook(db, InvoiceFilter(cp_key="code:2"))
    assert count == 1
    ws = load_workbook(BytesIO(data))["Накладні"]
    assert ws.cell(2, 4).value == "Б"
