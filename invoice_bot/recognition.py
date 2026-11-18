"""Розпізнавання накладної через Google Gemini (structured output за Pydantic-схемою)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

from . import parsing

log = logging.getLogger(__name__)


class InvoiceItem(BaseModel):
    name: Optional[str] = Field(None, description="Найменування товару/послуги, як у документі")
    quantity: Optional[float] = Field(None, description="Кількість")
    unit: Optional[str] = Field(None, description="Одиниця виміру (шт, кг, л, послуга...)")
    price: Optional[float] = Field(None, description="Ціна за одиницю")
    total: Optional[float] = Field(None, description="Сума рядка")


class InvoiceData(BaseModel):
    is_invoice: bool = Field(description="true, якщо це накладна/рахунок/акт/ТТН з реквізитами; false для інших зображень")
    number: Optional[str] = Field(None, description="Номер документа точно як надруковано")
    invoice_date: Optional[str] = Field(None, description="Дата виписки у форматі YYYY-MM-DD")
    supplier_name: Optional[str] = Field(None, description="Постачальник/продавець — повна назва")
    supplier_code: Optional[str] = Field(None, description="ЄДРПОУ або ІПН постачальника — лише цифри")
    buyer_name: Optional[str] = Field(None, description="Покупець/одержувач — повна назва")
    buyer_code: Optional[str] = Field(None, description="ЄДРПОУ або ІПН покупця — лише цифри")
    total: Optional[float] = Field(None, description="Загальна сума до сплати з ПДВ")
    vat: Optional[float] = Field(None, description="Сума ПДВ; якщо вказано 'Без ПДВ' — 0")
    currency: Optional[str] = Field(None, description="Код валюти ISO 4217, напр. UAH, USD, EUR")
    items: list[InvoiceItem] = Field(default_factory=list, description="Перелік товарних рядків")


PROMPT = """Ти — бухгалтер, що вводить українські первинні документи (видаткові накладні,
рахунки-фактури, акти, ТТН) в облікову систему. Уважно прочитай зображення і заповни JSON.

Правила:
- Нічого не вигадуй. Якщо поле нечитабельне або відсутнє — залиш його null.
- "Постачальник"/"Продавець"/"Виконавець" → supplier; "Покупець"/"Одержувач"/"Замовник"/"Платник" → buyer.
- Коди ЄДРПОУ (8 цифр) або ІПН/РНОКПП (10 цифр) — лише цифри. ІПН платника ПДВ (12 цифр) не плутай з ЄДРПОУ;
  якщо є і ЄДРПОУ, і ІПН платника ПДВ — бери ЄДРПОУ.
- Дата виписки — у форматі YYYY-MM-DD (наприклад, "5 жовтня 2026 р." → "2026-10-05").
- total — підсумкова сума з ПДВ ("Всього з ПДВ", "Разом до сплати"). vat — "У тому числі ПДВ" / "ПДВ".
  Якщо написано "Без ПДВ" або "ПДВ не передбачено" — vat = 0.
- Числа повертай як числа: "1 200,50" → 1200.50.
- Валюта за замовчуванням UAH, якщо не вказано іншу.
- items — усі рядки таблиці товарів/послуг (без рядків "Разом", "ПДВ", "Всього").
- Якщо на зображенні немає накладної/рахунку (фото людини, екрану, пейзаж, нечитабельне розмите фото),
  is_invoice = false і решта полів null.
"""


class RecognitionError(Exception):
    """Помилка після всіх повторів — текст можна показати користувачу."""


class Recognizer:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", retries: int = 3):
        from google import genai  # імпорт тут, щоб тести не залежали від мережі

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.retries = retries

    async def recognize(self, data: bytes, mime_type: str) -> InvoiceData:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=InvoiceData,
            temperature=0,
        )
        contents = [types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT]
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self.client.aio.models.generate_content(model=self.model, contents=contents, config=config),
                    timeout=60,
                )
                result = resp.parsed
                if isinstance(result, InvoiceData):
                    return result
                if resp.text:
                    return InvoiceData.model_validate_json(resp.text)
                raise ValueError("порожня відповідь моделі")
            except Exception as exc:  # мережа, ліміти, невалідний JSON
                last_exc = exc
                log.warning("Gemini, спроба %s/%s: %r", attempt, self.retries, exc)
                if attempt < self.retries:
                    await asyncio.sleep(2 * attempt)
        log.error("Gemini не відповів після %s спроб", self.retries, exc_info=last_exc)
        text = str(last_exc or "")
        if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
            raise RecognitionError("Вичерпано ліміт запитів до ШІ. Спробуйте за хвилину.")
        if "API key" in text or "API_KEY" in text or "PERMISSION_DENIED" in text:
            raise RecognitionError("Ключ Gemini недійсний — зверніться до адміністратора бота.")
        raise RecognitionError("Сервіс розпізнавання тимчасово недоступний. Спробуйте трохи пізніше.")


def normalize(data: InvoiceData, our_code: str = "") -> dict:
    """Результат ШІ → поля для таблиці invoices (+ items)."""
    supplier_code = parsing.digits_only(data.supplier_code)
    buyer_code = parsing.digits_only(data.buyer_code)
    supplier_name = (data.supplier_name or "").strip()
    buyer_name = (data.buyer_name or "").strip()
    if our_code and supplier_code == our_code:
        cp_name, cp_code = buyer_name, buyer_code
    else:
        cp_name, cp_code = supplier_name, supplier_code
    currency = (data.currency or "UAH").strip().upper()[:3] or "UAH"
    if currency in ("ГРН", "UAH"):
        currency = "UAH"
    items = []
    for it in data.items or []:
        if not (it.name or it.total or it.quantity):
            continue
        items.append({
            "name": (it.name or "").strip(),
            "quantity": it.quantity,
            "unit": (it.unit or "").strip(),
            "price": _round(it.price),
            "total": _round(it.total),
        })
    return {
        "number": (data.number or "").strip(),
        "invoice_date": parsing.normalize_iso_date(data.invoice_date),
        "supplier_name": supplier_name,
        "supplier_code": supplier_code,
        "buyer_name": buyer_name,
        "buyer_code": buyer_code,
        "counterparty_name": cp_name,
        "counterparty_code": cp_code,
        "total": _round(data.total),
        "vat": _round(data.vat),
        "currency": currency,
        "items": items,
        "raw_json": data.model_dump_json(),
    }


def _round(v: float | None) -> float | None:
    return None if v is None else round(float(v), 2)
