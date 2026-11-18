"""Наскрізні сценарії обробників без мережі: підроблені Telegram-об'єкти, ШІ та Odoo."""

from itertools import count
from types import SimpleNamespace

import pytest

from invoice_bot.bot import InvoiceBot, parse_field_value
from invoice_bot.config import Config
from invoice_bot.odoo import OdooClient, OdooSync
from invoice_bot.recognition import InvoiceData, InvoiceItem
from tests import fake_odoo as F

_ids = count(1000)


class Msg:
    def __init__(self, h, text=None, photo=None, document=None, caption=None):
        self.h, self.text, self.photo, self.document, self.caption = h, text, photo, document, caption
        self.message_id = next(_ids)
        self.edits = []

    async def reply_text(self, text, reply_markup=None, **kw):
        return self.h.out(text, reply_markup)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append(text)
        self.h.sent.append(("edit", text, reply_markup))


class FakeBot:
    def __init__(self, h):
        self.h = h

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        return self.h.out(text, reply_markup)

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.h.sent.append(("photo", photo, caption))

    async def send_document(self, chat_id, document=None, caption=None, filename=None, **kw):
        self.h.sent.append(("document", filename or document, caption))

    async def get_file(self, file_id):
        async def download():
            return bytearray(b"\xff\xd8 fake jpeg " + file_id.encode())
        return SimpleNamespace(download_as_bytearray=download)


class FakeRecognizer:
    def __init__(self):
        self.result = InvoiceData(
            is_invoice=True, number="101", invoice_date="2026-10-05", supplier_name="ТОВ \"Ромашка\"",
            supplier_code="12345678", buyer_name="ТОВ Наша", buyer_code="87654321", total=1200, vat=200,
            items=[InvoiceItem(name="Цукор", quantity=10, unit="кг", price=30, total=300)])
        self.calls = 0

    async def recognize(self, data, mime):
        self.calls += 1
        return self.result


class Harness:
    def __init__(self, db, cfg=None, odoo=None):
        self.cfg = cfg or Config(allowed_users=frozenset({1, 2}), admin_ids=frozenset({9}))
        self.sent = []
        self.rec = FakeRecognizer()
        self.app = InvoiceBot(self.cfg, db, self.rec, odoo)
        self.app._bot = FakeBot(self)
        self.user_data = {1: {}, 2: {}, 3: {}, 9: {}}

    def out(self, text, markup=None):
        self.sent.append(("msg", text, markup))
        return Msg(self)

    def _update(self, uid, message=None, query=None):
        user = SimpleNamespace(id=uid, full_name=f"User{uid}", username=f"u{uid}", first_name=f"User{uid}")
        return SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=uid), message=message,
                               effective_message=message, callback_query=query)

    def _ctx(self, uid):
        return SimpleNamespace(user_data=self.user_data.setdefault(uid, {}), bot=self.app._bot)

    async def text(self, uid, text):
        await self.app.on_text(self._update(uid, Msg(self, text=text)), self._ctx(uid))

    async def photo(self, uid, file_id="PH1", caption=None):
        m = Msg(self, photo=[SimpleNamespace(file_id=file_id, file_size=1000)], caption=caption)
        await self.app.on_file(self._update(uid, m), self._ctx(uid))

    async def click(self, uid, data):
        alerts = []

        async def answer(text=None, show_alert=False, **kw):
            alerts.append(text)

        q = SimpleNamespace(data=data, answer=answer, message=Msg(self))
        await self.app.on_callback(self._update(uid, query=q), self._ctx(uid))
        return alerts

    @property
    def last(self):
        return self.sent[-1][1]


@pytest.fixture
def h(db):
    return Harness(db)


async def test_photo_saved_and_duplicate_warning(h, db):
    await h.photo(1)
    assert h.sent[0][1] == "🔍 Розпізнаю…"
    assert "✅ <b>Збережено!</b>" in h.last and "Накладна № 101" in h.last
    inv = db.get_invoice(1)
    assert inv["counterparty_code"] == "12345678" and inv["user_name"] == "User1" and inv["photo_file_id"] == "PH1"
    await h.photo(2, "PH2")
    assert "Можливий дублікат" in h.last and "#1" in h.last
    assert db.get_invoice(2)["is_duplicate"] == 1


async def test_not_invoice(h, db):
    h.rec.result = InvoiceData(is_invoice=False)
    await h.photo(1)
    assert "не накладна" in h.last and db.count_invoices() == 0


async def test_access_denied(h, db):
    await h.text(3, "привіт")
    assert "Немає доступу" in h.last and "<code>3</code>" in h.last
    await h.app.cmd_myid(h._update(3, Msg(h, text="/myid")), h._ctx(3))
    assert "3" in h.last
    alerts = await h.click(3, "inv:1")
    assert alerts == ["⛔ Немає доступу"]


async def test_event_dialog_with_attachment(h, db):
    await h.photo(1)
    await h.click(2, "ev:1")
    await h.click(2, "et:1:refund")
    assert "Введіть суму" in h.last
    await h.text(2, "не сума")
    assert "Не схоже на суму" in h.last
    await h.text(2, "500,50")
    assert "Дата події" in h.last
    await h.click(2, "evd:0")
    assert "коментар" in h.last
    await h.photo(2, "EVFILE", caption="ТТН 2045")  # вкладення, а НЕ нова накладна
    assert db.count_invoices() == 1 and h.rec.calls == 1
    ev = db.events(1)[0]
    assert (ev["event_type"], ev["amount"], ev["comment"], ev["file_id"], ev["user_id"]) == \
           ("refund", 500.5, "ТТН 2045", "EVFILE", 2)
    assert db.get_invoice(1)["status"] == "refund"
    assert "✅ Подію додано." in h.last and "💸 Повернення коштів" in h.last
    # перегляд і видалення: автор (2) може, власник накладної (1) — ні
    await h.click(2, "mv:1")
    assert "📎 1" in [b.text for row in h.sent[-1][2].inline_keyboard for b in row]
    assert (await h.click(1, f"edl:{ev['id']}"))[0].startswith("⛔")
    await h.click(2, f"edl:{ev['id']}")
    await h.click(2, f"edy:{ev['id']}")
    assert db.events(1) == [] and db.get_invoice(1)["status"] is None


async def test_event_without_amount_and_comment(h, db):
    await h.photo(1)
    await h.click(1, "et:1:sent")
    assert "Дата події" in h.last
    await h.text(1, "13.11.2026")
    await h.click(1, "evn")
    ev = db.events(1)[0]
    assert (ev["event_type"], ev["event_date"], ev["amount"], ev["comment"]) == ("sent", "2026-11-13", None, "")


async def test_edit_permissions_and_history(h, db):
    await h.photo(1)
    assert (await h.click(2, "ed:1"))[0].startswith("⛔")
    assert (await h.click(2, "dl:1"))[0].startswith("⛔")
    await h.click(1, "ef:1:total")
    await h.text(1, "abc")
    assert "Не схоже на суму" in h.last
    await h.text(1, "1 250,50")
    assert "1 200,00 грн → 1 250,50 грн" in h.last
    await h.click(9, "ef:1:invoice_date")  # адмін може чуже
    await h.text(9, "06.10.2026")
    assert db.get_invoice(1)["invoice_date"] == "2026-10-06"
    await h.click(1, "hi:1")
    assert "User9" in h.last and "05.10.2026 → 06.10.2026" in h.last


async def test_delete_restore(h, db):
    await h.photo(1)
    await h.click(1, "dl:1")
    assert "можна буде відновити" in h.last
    await h.click(1, "dly:1")
    assert db.count_invoices() == 0 and "Відновити" in str(h.sent[-1][2].inline_keyboard)
    await h.click(1, "rs:1")
    assert db.count_invoices() == 1


async def test_search_filters_and_menu(h, db):
    await h.photo(1)
    await h.text(1, "РОМАШ")
    assert "Знайдено накладних: <b>1</b>" in h.last and "🔎 «РОМАШ»" in h.last
    await h.text(1, "#1")
    assert "Накладна № 101" in h.last
    await h.text(1, "#77")
    assert "немає" in h.last
    await h.text(1, "📋 Усі накладні")
    assert "Знайдено накладних: <b>1</b>" in h.last
    await h.click(1, "pr:custom")
    await h.text(1, "01.11.2026-30.11.2026")
    assert "Знайдено накладних: <b>0</b>" in h.last and "01.11.2026 – 30.11.2026" in h.last
    await h.text(1, "♻️ Скинути фільтр")
    assert "Фільтр: усі накладні" in h.last
    await h.text(1, "🏢 За контрагентом")
    await h.click(1, "cp:1")
    assert "🔎 Фільтр: 🏢 ТОВ \"Ромашка\"" in h.last and "Знайдено накладних: <b>1</b>" in h.last
    await h.click(1, "st:sent")
    assert "Знайдено накладних: <b>0</b>" in h.last
    await h.text(1, "📊 Підсумки")
    assert "Накладних: <b>0</b>" in h.last
    await h.text(1, "📥 Експорт в Excel")
    assert h.sent[-1][0] == "document" and h.sent[-1][1].endswith(".xlsx")
    await h.text(1, "🕓 Журнал дій")
    assert "Журнал дій" in h.last
    # фільтр одного користувача не впливає на іншого
    assert db.get_filter(2).is_empty and not db.get_filter(1).is_empty


def test_parse_field_value():
    assert parse_field_value("money", "1 200,5") == (1200.5, None)
    assert parse_field_value("date", "05.10.2026") == ("2026-10-05", None)
    assert parse_field_value("code", "1234 5678") == ("12345678", None)
    assert parse_field_value("code", "-") == ("", None)
    assert parse_field_value("code", "123")[1]
    assert parse_field_value("text", "  ")[1]


async def test_odoo_auto_sync(db):
    server = F.FakeOdoo().start()
    try:
        cfg = Config(odoo_url=server.url, odoo_db=F.DB, odoo_user=F.LOGIN, odoo_api_key=F.KEY, odoo_auto_sync=True)
        h = Harness(db, cfg, OdooSync(OdooClient(server.url, F.DB, F.LOGIN, F.KEY)))
        await h.photo(1)
        inv = db.get_invoice(1)
        assert inv["odoo_status"] == "sent" and inv["odoo_id"]
        assert "🔗 Odoo: ✅" in h.last and "🔄 Оновити в Odoo" in str(h.sent[-1][2].inline_keyboard)
        assert len(server.records["ir.attachment"]) == 1
        # дублікат автоматично не відправляється
        await h.photo(1, "PH2")
        assert db.get_invoice(2)["odoo_status"] == ""
        # зміна після відправки → «змінено» → оновлення чернетки
        await h.click(1, "ef:1:number")
        await h.text(1, "101-A")
        assert db.get_invoice(1)["odoo_status"] == "modified" and "Оновити в Odoo" in h.last
        await h.click(1, "os:1")
        assert server.records["account.move"][inv["odoo_id"]]["ref"] == "101-A"
        assert db.get_invoice(1)["odoo_status"] == "sent"
        # подія → нотатка в чатері
        await h.click(1, "et:1:sent")
        await h.click(1, "evd:0")
        await h.click(1, "evn")
        import asyncio
        await asyncio.gather(*h.app._bg_tasks)
        assert server.messages and "Відправлено контрагенту" in server.messages[0][1]
        # проведений рахунок не чіпаємо
        server.records["account.move"][inv["odoo_id"]]["state"] = "posted"
        await h.click(1, "os:1")
        assert db.get_invoice(1)["odoo_status"] == "error" and "не чернетка" in h.last
        # /odoo_sync — лише адміни (тут ADMIN_IDS порожній → усі адміни)
        await h.app.cmd_odoo_sync(h._update(1, Msg(h, text="/odoo_sync")), h._ctx(1))
        assert "Синхронізація з Odoo завершена" in h.last
        actions = [x["action"] for x in db.history(1)]
        assert "odoo_sent" in actions and "odoo_updated" in actions and "odoo_error" in actions
    finally:
        server.stop()


async def test_no_odoo_buttons_without_config(h, db):
    await h.photo(1)
    assert "Odoo" not in h.last
    assert "Odoo" not in str(h.sent[-1][2].inline_keyboard)
    await h.app.cmd_odoo_test(h._update(1, Msg(h, text="/odoo_test")), h._ctx(1))
    assert "не підключено" in h.last


def test_application_builds_offline(db):
    app = InvoiceBot(Config(bot_token="123456:TEST"), db).build()
    assert len(app.handlers[0]) == 8 and app.error_handlers
