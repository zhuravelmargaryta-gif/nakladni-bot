import pytest

from invoice_bot.db import Database

USER = {"id": 1, "name": "Олена", "username": "olena"}
OTHER = {"id": 2, "name": "Іван", "username": "ivan"}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def make_invoice(db, number="101", date="2026-10-05", cp="ТОВ \"Ромашка\"", code="12345678", total=1200.0,
                 vat=200.0, user=USER, items=None, **extra):
    data = {"number": number, "invoice_date": date, "counterparty_name": cp, "counterparty_code": code,
            "supplier_name": cp, "supplier_code": code, "total": total, "vat": vat, "currency": "UAH",
            "sent_at": "2026-11-12T15:00:00", "user_id": user["id"], "user_name": user["name"],
            "user_username": user["username"], "photo_file_id": "PHOTO", "photo_kind": "photo",
            "photo_mime": "image/jpeg"}
    data.update(extra)
    items = items if items is not None else [
        {"name": "Цукор", "quantity": 10, "unit": "кг", "price": 30.0, "total": 300.0},
        {"name": "Борошно", "quantity": 15, "unit": "кг", "price": 60.0, "total": 900.0},
    ]
    return db.add_invoice(data, items, user)
