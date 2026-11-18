from datetime import date

import pytest

from invoice_bot import parsing as p


@pytest.mark.parametrize("text,expected", [
    ("05.10.2026", date(2026, 10, 5)), ("5.10.26", date(2026, 10, 5)), ("2026-10-05", date(2026, 10, 5)),
    ("05/10/2026", date(2026, 10, 5)), (" 05.10.2026. ", date(2026, 10, 5)),
    ("32.10.2026", None), ("abc", None), ("", None), (None, None),
])
def test_parse_date(text, expected):
    assert p.parse_date(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("01.10.2026-31.10.2026", (date(2026, 10, 1), date(2026, 10, 31))),
    ("01.10.2026 – 31.10.2026", (date(2026, 10, 1), date(2026, 10, 31))),
    ("31.10.2026-01.10.2026", (date(2026, 10, 1), date(2026, 10, 31))),
    ("15.10.2026", (date(2026, 10, 15), date(2026, 10, 15))),
    ("щось", None),
])
def test_parse_period(text, expected):
    assert p.parse_period(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1200", 1200.0), ("1 200,50", 1200.5), ("1200.50", 1200.5), ("1,200.50", 1200.5), ("1.200,50", 1200.5),
    ("1 200 грн", 1200.0), ("-50,5", -50.5), ("1 234,00", 1234.0), ("1.234.567", 1234567.0),
    ("0", 0.0), ("abc", None), ("", None), ("12,34,56", 123456.0),
])
def test_parse_amount(text, expected):
    assert p.parse_amount(text) == expected


def test_formats():
    assert p.fmt_money(1200) == "1 200,00 грн"
    assert p.fmt_money(1234567.5, "USD") == "1 234 567,50 USD"
    assert p.fmt_money(-50) == "-50,00 грн"
    assert p.fmt_date("2026-10-05") == "05.10.2026"
    assert p.fmt_date("") == "—"
    assert p.fmt_datetime("2026-11-12T15:00:00") == "12.11.2026 15:00"
    assert p.fmt_qty(10.0) == "10"
    assert p.fmt_qty(2.5) == "2,5"
    assert p.digits_only("UA 1234-5678") == "12345678"
    assert p.esc("<b>&") == "&lt;b&gt;&amp;"


def test_period_bounds():
    t = date(2026, 11, 13)  # п'ятниця
    assert p.period_bounds("today", t)[:2] == (t, t)
    assert p.period_bounds("week", t)[:2] == (date(2026, 11, 9), date(2026, 11, 15))
    assert p.period_bounds("month", t)[:2] == (date(2026, 11, 1), date(2026, 11, 30))
    assert p.period_bounds("prev_month", t)[:2] == (date(2026, 10, 1), date(2026, 10, 31))
    assert p.period_bounds("prev_month", date(2026, 1, 15))[:2] == (date(2025, 12, 1), date(2025, 12, 31))
    assert p.period_bounds("year", t)[:2] == (date(2026, 1, 1), date(2026, 12, 31))
    assert p.period_bounds("any", t)[:2] == (None, None)
