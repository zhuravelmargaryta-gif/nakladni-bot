import pytest

from invoice_bot.odoo import Attachment, OdooClient, OdooError, OdooSync
from tests import fake_odoo as F


@pytest.fixture
def server():
    s = F.FakeOdoo().start()
    yield s
    s.stop()


def sync_for(server, **kw):
    return OdooSync(OdooClient(server.url, F.DB, F.LOGIN, F.KEY, timeout=5), **kw)


INV = {"id": 1, "number": "101", "invoice_date": "2026-10-05", "counterparty_name": "ТОВ Ромашка",
       "counterparty_code": "12345678", "total": 1200.0, "vat": 200.0, "odoo_id": None}
ITEMS = [{"name": "Цукор", "quantity": 10, "price": 30.0, "total": 300.0},
         {"name": "Послуга", "quantity": None, "price": None, "total": 900.0}]


def test_connection(server):
    assert "17.0-fake" in sync_for(server).test()


def test_bad_credentials(server):
    s = OdooSync(OdooClient(server.url, F.DB, F.LOGIN, "wrong"))
    with pytest.raises(OdooError, match="API-ключ"):
        s.test()


def test_unreachable():
    s = OdooSync(OdooClient("http://127.0.0.1:1", "db", "u", "k", timeout=2))
    with pytest.raises(OdooError, match="з'єднання"):
        s.test()


def test_create_draft_with_partner_and_attachment(server):
    s = sync_for(server)
    odoo_id, action = s.push_invoice(INV, ITEMS, Attachment(b"jpgdata", "n.jpg", "image/jpeg"))
    assert action == "created"
    move = server.records["account.move"][odoo_id]
    assert move["move_type"] == "in_invoice" and move["ref"] == "101" and move["invoice_date"] == "2026-10-05"
    assert move["state"] == "draft"
    assert [ln["price_unit"] for ln in move["lines"]] == [30.0, 900.0]
    assert move["lines"][0]["tax_ids"] == [[6, 0, []]]
    partner = server.records["res.partner"][move["partner_id"]]
    assert partner["company_registry"] == "12345678" and partner["name"] == "ТОВ Ромашка"
    att = list(server.records["ir.attachment"].values())[0]
    assert att["res_id"] == odoo_id and att["res_model"] == "account.move"

    # другий раз контрагент знаходиться за ЄДРПОУ, а не створюється
    s.push_invoice(INV | {"counterparty_name": "Ромашка (інша назва)"}, [])
    assert len(server.records["res.partner"]) == 1


def test_partner_by_name_and_tax_options(server):
    s = sync_for(server, purchase_tax_id=9, journal_id=3, expense_account_id=44)
    inv = INV | {"counterparty_code": ""}
    pid = s.find_or_create_partner("ТОВ Без Коду", "")
    assert s.find_or_create_partner("тов без коду", "") == pid
    odoo_id, _ = s.push_invoice(inv, [])
    move = server.records["account.move"][odoo_id]
    assert move["journal_id"] == 3
    line = move["lines"][0]
    assert line["price_unit"] == 1000.0 and line["tax_ids"] == [[6, 0, [9]]] and line["account_id"] == 44


def test_update_only_draft(server):
    s = sync_for(server)
    odoo_id, _ = s.push_invoice(INV, ITEMS)
    new_id, action = s.push_invoice(INV | {"odoo_id": odoo_id, "number": "101-A"}, ITEMS[:1])
    assert (new_id, action) == (odoo_id, "updated")
    move = server.records["account.move"][odoo_id]
    assert move["ref"] == "101-A" and len(move["lines"]) == 1
    move["state"] = "posted"
    with pytest.raises(OdooError, match="не чернетка"):
        s.push_invoice(INV | {"odoo_id": odoo_id}, ITEMS)
    assert server.records["account.move"][odoo_id]["ref"] == "101-A"


def test_note_and_fault_text(server):
    s = sync_for(server)
    odoo_id, _ = s.push_invoice(INV, [])
    s.post_note(odoo_id, "📤 Відправлено контрагенту")
    assert server.messages == [(odoo_id, "📤 Відправлено контрагенту")]
    bad = sync_for(server, partner_code_field="nonexistent_field")
    with pytest.raises(OdooError, match="Invalid field"):
        bad.find_or_create_partner("Нова", "55555555")
