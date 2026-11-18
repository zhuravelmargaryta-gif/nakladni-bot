"""Типи подій руху накладної.

Щоб додати новий тип — допишіть рядок у EVENT_TYPES. Ключ (key) — латиницею,
не довше 16 символів, і його не можна змінювати після початку використання.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventType:
    key: str
    emoji: str
    title: str
    has_amount: bool

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.title}"


EVENT_TYPES: list[EventType] = [
    EventType("sent", "📤", "Відправлено контрагенту", False),
    EventType("received_signed", "📥", "Повернулась підписана від контрагента", False),
    EventType("goods_received", "🚚", "Товар отримано", False),
    EventType("goods_shipped", "📦", "Товар відвантажено", False),
    EventType("paid", "💳", "Оплачено", True),
    EventType("partial_paid", "💵", "Часткова оплата", True),
    EventType("correction", "🔧", "Коригування", True),
    EventType("refund", "💸", "Повернення коштів", True),
    EventType("goods_return", "↩️", "Повернення товару", True),
    EventType("goods_lost", "❓", "Товар загубився / недостача", True),
    EventType("damaged", "💥", "Пошкодження товару", True),
    EventType("claim", "⚠️", "Претензія / спір", True),
    EventType("closed", "🏁", "Закрито", False),
    EventType("other", "📝", "Інше", True),
]

BY_KEY: dict[str, EventType] = {e.key: e for e in EVENT_TYPES}

UNKNOWN = EventType("unknown", "❔", "Невідома подія", True)


def get(key: str | None) -> EventType:
    return BY_KEY.get(key or "", UNKNOWN if key else UNKNOWN)
