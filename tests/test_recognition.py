from invoice_bot.recognition import InvoiceData, InvoiceItem, normalize


def _data(**kw):
    base = dict(is_invoice=True, number=" 101 ", invoice_date="05.10.2026", supplier_name="ТОВ Ромашка",
                supplier_code="UA 1234 5678", buyer_name="ТОВ Наша", buyer_code="87654321", total=1200,
                vat=200, currency=None, items=[InvoiceItem(name="Цукор", quantity=10, unit="кг", price=30, total=300),
                                               InvoiceItem()])
    base.update(kw)
    return InvoiceData(**base)


def test_normalize_supplier_is_counterparty():
    r = normalize(_data())
    assert r["number"] == "101" and r["invoice_date"] == "2026-10-05"
    assert (r["counterparty_name"], r["counterparty_code"]) == ("ТОВ Ромашка", "12345678")
    assert r["currency"] == "UAH" and len(r["items"]) == 1


def test_normalize_we_are_supplier():
    r = normalize(_data(), our_code="12345678")
    assert (r["counterparty_name"], r["counterparty_code"]) == ("ТОВ Наша", "87654321")


def test_normalize_empty_fields_not_invented():
    r = normalize(InvoiceData(is_invoice=True, invoice_date="нечитабельно"))
    assert r["invoice_date"] == "" and r["total"] is None and r["counterparty_name"] == ""
