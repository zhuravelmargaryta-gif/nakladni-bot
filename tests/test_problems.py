"""Стан проблеми: кожна накладна — відкрита проблема, доки її не позначать вирішеною."""

from datetime import date

from invoice_bot import cards
from invoice_bot.db import InvoiceFilter, is_resolved
from tests.conftest import OTHER, USER, make_invoice
from tests.test_bot_flow import Harness


def test_new_invoice_is_open_problem(db):
    i = make_invoice(db)
    inv = db.get_invoice(i)
    assert not is_resolved(inv)
    assert db.problem_counts() == {"open": 1, "resolved": 0, "all": 1}
    text = cards.invoice_card(inv, [], [])
    assert "🔴 <b>Проблема відкрита</b>" in text
    kb = cards.invoice_keyboard(inv)
    assert kb.inline_keyboard[0][0].callback_data == f"rv:{i}"


def test_resolve_reopen_and_filters(db):
    a = make_invoice(db, number="1")
    b = make_invoice(db, number="2")
    db.add_event(a, "claim", "2026-11-10", USER, amount=100)
    db.add_event(a, "resolved", "2026-11-12", OTHER, comment="довезли товар")
    inv = db.get_invoice(a)
    assert is_resolved(inv) and inv["problem_date"] == "2026-11-12"
    assert inv["status"] == "claim"  # статус руху не змінюється від «вирішено»
    assert [r["id"] for r in db.list_invoices(InvoiceFilter(problems="resolved"))] == [a]
    assert [r["id"] for r in db.list_invoices(InvoiceFilter(problems="open"))] == [b]
    text = cards.invoice_card(inv, [], db.events(a))
    assert "🟢 <b>Проблему вирішено</b> · 12.11.2026 — Іван" in text and "довезли товар" in text
    assert cards.invoice_keyboard(inv).inline_keyboard[0][0].callback_data == f"ro:{a}"
    # відкрили знову того ж дня — пізніша подія перемагає
    db.add_event(a, "reopened", "2026-11-12", USER)
    assert not is_resolved(db.get_invoice(a))
    assert db.problem_counts() == {"open": 2, "resolved": 0, "all": 2}
    s = db.summary()
    assert (s["open"], s["resolved"]) == (2, 0)


def test_deleting_resolved_event_reopens(db):
    a = make_invoice(db)
    ev = db.add_event(a, "resolved", "2026-11-12", USER)
    assert is_resolved(db.get_invoice(a))
    db.delete_event(ev, USER)
    assert not is_resolved(db.get_invoice(a))


def test_open_problems_days(db):
    old = make_invoice(db, number="old", sent_at="2026-11-01T10:00:00")
    new = make_invoice(db, number="new", sent_at="2026-11-18T10:00:00")
    done = make_invoice(db, number="done", sent_at="2026-10-01T10:00:00")
    db.add_event(done, "resolved", "2026-11-02", USER)
    re = make_invoice(db, number="re", sent_at="2026-10-01T10:00:00")
    db.add_event(re, "resolved", "2026-10-05", USER)
    db.add_event(re, "reopened", "2026-11-15", USER)
    rows = db.open_problems(date(2026, 11, 19))
    assert [(r["id"], r["days"]) for r in rows] == [(old, 18), (re, 4), (new, 1)]
    assert [r["id"] for r in db.open_problems(date(2026, 11, 19), min_days=5)] == [old]
    text, kb = cards.reminder_view(rows, 1)
    assert "невирішені проблеми (3)" in text and "<b>18 дн.</b>" in text
    assert kb.inline_keyboard[-1][0].callback_data == "pbl:open"


async def test_bot_resolve_flow(db):
    h = Harness(db)
    await h.photo(1)
    assert "🔴 <b>Проблема відкрита</b>" in h.last
    await h.click(2, "rv:1")  # будь-який співробітник може позначити
    assert "Позначаю проблему вирішеною" in h.last
    await h.text(2, "Довезли недостачу")
    assert "🟢 Проблему позначено вирішеною." in h.last and "Довезли недостачу" in h.last
    assert (await h.click(2, "rv:1"))[0].startswith("Проблему вже")
    # фото під час позначення — вкладення, а не нова накладна
    await h.click(1, "ro:1")
    assert not is_resolved(db.get_invoice(1)) and "відкрито знову" in h.last
    await h.click(1, "rv:1")
    await h.photo(1, "PROOF", caption="акт")
    assert db.count_invoices() == 1 and db.events(1)[-1]["file_id"] == "PROOF"
    # перемикач у списку
    await h.text(1, "📋 Усі накладні")
    toggle = h.sent[-1][2].inline_keyboard[0]
    assert [b.text for b in toggle] == ["🔴 Відкриті (0)", "🟢 Вирішені (1)", "• Усі (1)"]
    await h.click(1, "pb:open")
    assert "🔴 Відкриті" in h.last and "Знайдено накладних: <b>0</b>" in h.last
    # «Подія» не пропонує службові типи
    await h.click(1, "ev:1")
    calls = [b.callback_data for row in h.sent[-1][2].inline_keyboard for b in row]
    assert "et:1:resolved" not in calls and "et:1:sent" in calls


async def test_send_reminders(db):
    from invoice_bot.config import Config
    h = Harness(db, Config(allowed_users=frozenset({1, 2}), admin_ids=frozenset({9}), reminder_min_days=0))
    assert await h.app.send_reminders() == 0  # немає проблем — нічого не шлемо
    make_invoice(db)
    assert await h.app.send_reminders() == 1  # лише адміну (9)
    assert "Нагадування" in h.last
    await h.click(9, "pbl:open")
    assert "🔴 Відкриті" in h.last and "Знайдено накладних: <b>1</b>" in h.last


def test_reminder_config():
    from invoice_bot.config import Config
    assert Config().reminder_clock == (9, 0)
    assert Config(reminder_time="").reminder_clock is None
    assert Config(reminder_time="25:00").reminder_clock is None
    assert Config(allowed_users=frozenset({1})).reminder_recipients == {1}
    assert Config(allowed_users=frozenset({1}), admin_ids=frozenset({9})).reminder_recipients == {9}
    assert Config(admin_ids=frozenset({9}), reminder_user_ids=frozenset({5})).reminder_recipients == {5}
