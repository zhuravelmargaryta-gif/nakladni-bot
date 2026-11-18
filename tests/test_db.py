import sqlite3

from invoice_bot.db import Database, InvoiceFilter, NO_STATUS, counterparty_key
from tests.conftest import OTHER, USER, make_invoice


def test_add_and_get(db):
    i = make_invoice(db)
    inv = db.get_invoice(i)
    assert inv["number"] == "101" and inv["total"] == 1200.0 and inv["status"] is None
    assert [it["name"] for it in db.items(i)] == ["Цукор", "Борошно"]
    assert db.history(i)[0]["action"] == "created"


def test_duplicates(db):
    i = make_invoice(db)
    assert [d["id"] for d in db.find_duplicates("101", "12345678", "інша назва")] == [i]
    assert db.find_duplicates("101", "99999999", "ТОВ \"Ромашка\"") == []
    j = make_invoice(db, code="", cp="ФОП Петренко")
    assert [d["id"] for d in db.find_duplicates(" 101 ", "", "фоп петренко")] == [j]
    db.soft_delete(i, USER)
    assert db.find_duplicates("101", "12345678", "") == []
    assert db.find_duplicates("", "12345678", "") == []


def test_cyrillic_search_case_insensitive(db):
    a = make_invoice(db, cp="ТОВ \"Ромашка\"")
    b = make_invoice(db, number="A-77", cp="ФОП Їжаченко Євген", code="3012345678")
    ids = lambda q: {r["id"] for r in db.list_invoices(InvoiceFilter(query=q))}
    assert ids("ромашка") == {a}
    assert ids("РОМАШ") == {a}
    assert ids("їжаченко") == {b}
    assert ids("ЄВГЕН") == {b}
    assert ids("a-77") == {b}
    assert ids("30123") == {b}
    assert ids("немає такого") == set()


def test_filters_combine(db):
    a = make_invoice(db, number="1", date="2026-10-05", code="11111111", cp="Альфа")
    b = make_invoice(db, number="2", date="2026-11-05", code="11111111", cp="Альфа")
    c = make_invoice(db, number="3", date="2026-10-20", code="22222222", cp="Бета")
    make_invoice(db, number="4", date="", code="11111111", cp="Альфа")
    db.add_event(a, "sent", "2026-10-06", USER)
    db.add_event(c, "sent", "2026-10-21", USER)
    db.add_event(c, "received_signed", "2026-10-25", USER)

    flt = InvoiceFilter(cp_key="code:11111111", date_from="2026-10-01", date_to="2026-10-31", status="sent")
    assert [r["id"] for r in db.list_invoices(flt)] == [a]
    assert db.count_invoices(flt) == 1
    # без статусу — обидві жовтневі для «Альфи» → лише a
    assert [r["id"] for r in db.list_invoices(flt.without("status"))] == [a]
    # усі відправлені, але не повернулись підписаними
    assert [r["id"] for r in db.list_invoices(InvoiceFilter(status="sent"))] == [a]
    assert {r["id"] for r in db.list_invoices(InvoiceFilter(status=NO_STATUS))} >= {b}
    counts = db.status_counts(InvoiceFilter())
    assert counts == {"sent": 1, "received_signed": 1, NO_STATUS: 2}


def test_sort_newest_first_and_paging(db):
    ids = [make_invoice(db, number=str(n), date=f"2026-10-{n:02d}") for n in range(1, 11)]
    no_date = make_invoice(db, number="x", date="")
    rows = db.list_invoices(InvoiceFilter(), 0, 8)
    assert [r["id"] for r in rows] == list(reversed(ids))[:8]
    rest = db.list_invoices(InvoiceFilter(), 8, 8)
    assert [r["id"] for r in rest] == [ids[1], ids[0], no_date]


def test_counterparties_grouping(db):
    make_invoice(db, code="11111111", cp="Альфа")
    make_invoice(db, code="11111111", cp="Альфа ТОВ (інша назва)")
    rep = make_invoice(db, code="", cp="Без коду")
    make_invoice(db, code="", cp="без КОДУ ")
    rows = db.counterparties()
    assert len(rows) == 2 and db.count_counterparties() == 2
    assert sorted(r["n"] for r in rows) == [2, 2]
    key, label = db.counterparty_of(rep)
    assert key == counterparty_key("", "Без коду") and label == "Без коду"
    assert db.count_invoices(InvoiceFilter(cp_key=key)) == 2
    assert db.count_counterparties("альфа") == 1


def test_edit_history_and_soft_delete(db):
    i = make_invoice(db)
    db.update_field(i, "total", 1250.0, OTHER)
    db.update_field(i, "invoice_date", "2026-10-06", OTHER)
    db.update_field(i, "total", 1250.0, OTHER)  # без змін — без запису
    hist = db.history(i)
    edits = [h for h in hist if h["action"] == "edited"]
    assert [(h["field"], h["old_value"], h["new_value"], h["user_name"]) for h in edits] == [
        ("total", "1200.0", "1250.0", "Іван"), ("invoice_date", "2026-10-05", "2026-10-06", "Іван")]
    db.soft_delete(i, USER)
    assert db.count_invoices() == 0 and db.summary()["count"] == 0
    assert db.get_invoice(i)["deleted_at"]
    db.restore(i, USER)
    assert db.count_invoices() == 1
    assert [h["action"] for h in db.history(i)][-2:] == ["deleted", "restored"]
    assert db.journal(20)[0]["action"] == "restored"


def test_events_status(db):
    i = make_invoice(db)
    e1 = db.add_event(i, "sent", "2026-11-13", USER, comment="ТТН 2045")
    e2 = db.add_event(i, "refund", "2026-11-15", OTHER, amount=500.0, file_id="F", file_kind="photo")
    db.add_event(i, "goods_shipped", "2026-11-10", USER)  # раніша дата — не стає статусом
    inv = db.get_invoice(i)
    assert inv["status"] == "refund" and inv["status_date"] == "2026-11-15"
    db.delete_event(e2, OTHER)
    assert db.get_invoice(i)["status"] == "sent"
    assert [e["id"] for e in db.events(i)][-1] == e1
    assert len(db.events(i, include_deleted=True)) == 3
    assert db.delete_event(e2, OTHER) is None
    actions = [h["action"] for h in db.history(i)]
    assert actions.count("event_added") == 3 and actions.count("event_deleted") == 1


def test_summary(db):
    a = make_invoice(db, total=1200, vat=200, code="1", cp="А")
    b = make_invoice(db, total=600, vat=100, code="2", cp="Б")
    make_invoice(db, total=None, vat=None, code="3", cp="В")
    db.add_event(a, "paid", "2026-11-01", USER, amount=1200)
    db.add_event(b, "partial_paid", "2026-11-01", USER, amount=100)
    db.add_event(b, "partial_paid", "2026-11-02", USER, amount=50)
    db.add_event(b, "goods_lost", "2026-11-03", USER, amount=30)
    s = db.summary()
    assert (s["count"], s["total"], s["vat"], s["net"]) == (3, 1800.0, 300.0, 1500.0)
    assert s["top"][0]["name"] == "А"
    assert s["events"]["partial_paid"] == {"count": 2, "amount": 150.0}
    assert s["events"]["goods_lost"]["amount"] == 30.0


def test_user_filters_persist(db):
    flt = InvoiceFilter(cp_key="code:1", cp_label="Альфа", status="sent", query="рома")
    db.save_filter(1, flt)
    db.save_filter(2, InvoiceFilter(date_from="2026-01-01"))
    assert db.get_filter(1) == flt
    assert db.get_filter(2).date_from == "2026-01-01"
    assert db.get_filter(3).is_empty


def test_odoo_status_modified_on_edit(db):
    i = make_invoice(db)
    db.set_odoo(i, "sent", odoo_id=345)
    assert [r["id"] for r in db.invoices_for_sync()] == []
    db.update_field(i, "number", "101-A", USER)
    assert db.get_invoice(i)["odoo_status"] == "modified"
    assert [r["id"] for r in db.invoices_for_sync()] == [i]
    dup = make_invoice(db, is_duplicate=1)
    assert dup not in [r["id"] for r in db.invoices_for_sync()]


def test_migration_from_old_schema(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, total REAL)")
    con.execute("INSERT INTO invoices (number, total) VALUES ('7', 100)")
    con.commit()
    con.close()
    d = Database(path)
    inv = d.get_invoice(1)
    assert inv["number"] == "7" and inv["total"] == 100 and inv["currency"] == "UAH" and inv["deleted_at"] is None
    assert d.events(1) == [] and d.count_invoices() == 1
    d.close()
    Database(path).close()  # повторний запуск не ламається
