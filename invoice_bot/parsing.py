"""Розбір і форматування дат, сум, кодів. Без залежностей від Telegram."""

from __future__ import annotations

import calendar
import html
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y")


# ---------- дати ----------

def parse_date(text: str | None) -> date | None:
    """'05.10.2026', '5.10.26', '2026-10-05', '05/10/2026' → date. Інакше None."""
    if not text:
        return None
    s = text.strip().rstrip(".").replace(" ", "")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


_DATE_TOKEN = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}")


def parse_period(text: str) -> tuple[date, date] | None:
    """'01.10.2026-31.10.2026', '01.10.2026 по 31.10.2026' або одна дата → (від, до)."""
    s = (text or "").strip()
    single = parse_date(s)
    if single:
        return single, single
    tokens = _DATE_TOKEN.findall(s)
    if len(tokens) == 2:
        d1, d2 = parse_date(tokens[0]), parse_date(tokens[1])
        if d1 and d2:
            return (d1, d2) if d1 <= d2 else (d2, d1)
    return None


def iso_to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalize_iso_date(value: str | None) -> str:
    """Будь-яка дата від ШІ → 'YYYY-MM-DD' або ''."""
    if not value:
        return ""
    d = iso_to_date(value) or parse_date(value)
    return d.isoformat() if d else ""


def fmt_date(value: str | date | None) -> str:
    d = value if isinstance(value, date) else iso_to_date(value)
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_short_date(value: str | date | None) -> str:
    d = value if isinstance(value, date) else iso_to_date(value)
    return d.strftime("%d.%m") if d else "—"


def fmt_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def period_bounds(key: str, today: date | None = None) -> tuple[date | None, date | None, str]:
    """Готові періоди: today, week, month, prev_month, year, any."""
    t = today or date.today()
    if key == "today":
        return t, t, "Сьогодні"
    if key == "week":
        start = t - timedelta(days=t.weekday())
        return start, start + timedelta(days=6), "Цей тиждень"
    if key == "month":
        return t.replace(day=1), t.replace(day=calendar.monthrange(t.year, t.month)[1]), "Цей місяць"
    if key == "prev_month":
        last = t.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, "Минулий місяць"
    if key == "year":
        return date(t.year, 1, 1), date(t.year, 12, 31), "Цей рік"
    return None, None, "Будь-яка дата"


# ---------- суми ----------

def parse_amount(text: str | None) -> float | None:
    """'1 200,50' / '1200.50' / '1,200.50' / '-50' / '1 200 грн' → float. Інакше None."""
    if text is None:
        return None
    s = str(text).strip().lower()
    for junk in ("грн", "uah", "₴", " ", " ", " ", "'"):
        s = s.replace(junk, "")
    s = s.rstrip(".")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(",") == 1:
        s = s.replace(",", ".")
    elif s.count(",") > 1:
        s = s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail if len(tail) != 3 else s.replace(".", "")
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return None
    try:
        return float(Decimal(s).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def currency_label(currency: str | None) -> str:
    c = (currency or "UAH").upper()
    return "грн" if c in ("UAH", "ГРН") else c


def fmt_number(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    s = f"{float(value):,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


def fmt_money(value: float | int | None, currency: str | None = "UAH") -> str:
    if value is None:
        return "—"
    return f"{fmt_number(value)} {currency_label(currency)}"


def fmt_qty(value: float | int | None) -> str:
    if value is None:
        return ""
    v = float(value)
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")


# ---------- інше ----------

def digits_only(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"
