from invoice_bot import cards, event_types
from invoice_bot.db import InvoiceFilter
from tests.conftest import USER, make_invoice


def _all_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_card_contents(db):
    i = make_invoice(db)
    db.add_event(i, "sent", "2026-11-13", USER, comment="ТТН 20450000123456", file_id="F")
    db.add_event(i, "refund", "2026-11-15", USER, amount=500)
    inv = db.get_invoice(i)
    text = cards.invoice_card(inv, db.items(i), db.events(i), header="✅ Збережено!")
    assert "🧾 <b>Накладна № 101</b>  (запис #1)" in text
    assert "📅 Дата виписки: 05.10.2026" in text
    assert "Олена (@olena)" in text
    assert "1 200,00 грн" in text and "🧮 ПДВ: 200,00 грн" in text
    assert "📌 Статус: 💸 Повернення коштів · 15.11.2026" in text
    assert "13.11 📤 Відправлено контрагенту — «ТТН 2045" in text and "📎" in text
    assert "1. Цукор — 10 кг × 30,00 грн = 300,00 грн" in text
    assert "Odoo" not in text
    assert "🔗 Odoo: ➖ не відправлено" in cards.invoice_card(inv, [], [], odoo_enabled=True)


def test_card_escapes_html_and_fits_limit(db):
    items = [{"name": f"Товар <b>№{n}</b> " + "x" * 80, "quantity": 1, "unit": "шт", "price": 1, "total": 1}
             for n in range(200)]
    i = make_invoice(db, cp="<script>&", items=items)
    text = cards.invoice_card(db.get_invoice(i), db.items(i), [])
    assert len(text) <= cards.TG_LIMIT
    assert "&lt;script&gt;&amp;" in text and "<script>" not in text
    assert "і ще" in text


def test_callback_data_limits(db):
    i = make_invoice(db)
    inv = db.get_invoice(i) | {"id": 999999999, "odoo_id": 5}
    markups = [cards.invoice_keyboard(inv, True), cards.edit_keyboard(999999999),
               cards.event_type_keyboard(999999999), cards.period_keyboard(),
               cards.status_keyboard({k.key: 3 for k in event_types.EVENT_TYPES}),
               cards.invoice_keyboard(inv | {"deleted_at": "x"})]
    for m in markups:
        for data in _all_callbacks(m):
            assert len(data.encode()) <= 64, data
    for et in event_types.EVENT_TYPES:
        assert len(et.key) <= 16


def test_list_view(db):
    for n in range(20):
        make_invoice(db, number=str(n))
    flt = InvoiceFilter(cp_label="Ромашка", cp_key="code:12345678", status="sent", period_label="Цей місяць",
                        date_from="2026-11-01", date_to="2026-11-30")
    text, kb = cards.list_view(db.list_invoices(None, 0, 8), 0, 20, flt)
    assert "🏢 Ромашка" in text and "📅 Цей місяць" in text and "📌 📤 Відправлено контрагенту" in text
    calls = _all_callbacks(kb)
    assert sum(c.startswith("inv:") for c in calls) == 8 and "lp:1" in calls
    first = kb.inline_keyboard[0][0].text
    assert "05.10.2026 · №19 · ТОВ \"Ромашка\" · 1 200,00 грн" in first
    assert cards.filter_description(InvoiceFilter()) == "🔎 Фільтр: усі накладні"


def test_history_view(db):
    i = make_invoice(db)
    db.update_field(i, "total", 1250.0, {"id": 2, "name": "Іван"})
    text = cards.history_view(db.get_invoice(i), db.history(i))
    assert "— Іван" in text and "✏️ Сума з ПДВ: 1 200,00 грн → 1 250,00 грн" in text
    j = cards.journal_view(db.journal())
    assert "#1" in j


def test_summary_view(db):
    a = make_invoice(db)
    db.add_event(a, "paid", "2026-11-01", USER, amount=1200)
    text = cards.summary_view(db.summary(), InvoiceFilter())
    assert "Накладних: <b>1</b>" in text and "Без ПДВ: 1 000,00 грн" in text
    assert "💳 Оплачено: 1 200,00 грн (1)" in text and "Топ-10" in text
