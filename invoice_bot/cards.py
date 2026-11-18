"""Тексти повідомлень (HTML) та inline-клавіатури. Не залежить від мережі — легко тестувати."""

from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from . import event_types
from .db import ACTIONS, EDITABLE_FIELDS, NO_STATUS, InvoiceFilter
from .parsing import (esc, fmt_date, fmt_datetime, fmt_money, fmt_qty, fmt_short_date, iso_to_date,
                      truncate)

TG_LIMIT = 4096
PAGE_SIZE = 8

# ---------------------------------------------------------------- головне меню

M_ALL = "📋 Усі накладні"
M_CP = "🏢 За контрагентом"
M_PERIOD = "📅 За періодом"
M_STATUS = "📌 За статусом"
M_SUMMARY = "📊 Підсумки"
M_EXCEL = "📥 Експорт в Excel"
M_JOURNAL = "🕓 Журнал дій"
M_RESET = "♻️ Скинути фільтр"
M_HELP = "ℹ️ Допомога"
MENU_BUTTONS = [M_ALL, M_CP, M_PERIOD, M_STATUS, M_SUMMARY, M_EXCEL, M_JOURNAL, M_RESET, M_HELP]


def main_menu() -> ReplyKeyboardMarkup:
    rows = [[M_ALL], [M_CP, M_PERIOD], [M_STATUS, M_SUMMARY], [M_EXCEL, M_JOURNAL], [M_RESET, M_HELP]]
    return ReplyKeyboardMarkup([[KeyboardButton(t) for t in r] for r in rows], resize_keyboard=True,
                               is_persistent=True, input_field_placeholder="Надішліть фото накладної або пошук…")


HELP_TEXT = (
    "ℹ️ <b>Як користуватися ботом</b>\n\n"
    "📷 <b>Надішліть фото накладної</b> (можна файлом без стиснення або PDF) — бот розпізнає реквізити "
    "і збереже їх.\n\n"
    "📋 <b>Меню внизу</b> — перегляд, фільтри за контрагентом, періодом і статусом, підсумки, Excel.\n"
    "Фільтри поєднуються між собою і зберігаються саме для вас. ♻️ — скинути.\n\n"
    "🔎 <b>Пошук</b> — просто напишіть у чат номер накладної, назву контрагента або ЄДРПОУ.\n"
    "<code>#12</code> — відкрити запис №12.\n\n"
    "📌 <b>Рух накладної</b> — у картці натисніть «➕ Подія»: відправлено, повернулась підписана, оплачено, "
    "повернення тощо. Можна додати коментар (напр. номер ТТН) і фото/документ.\n\n"
    "🕓 Усі зміни записуються в історію: хто, коли, що було і що стало.\n\n"
    "Команди: /start, /help, /myid"
)

# ---------------------------------------------------------------- дрібниці


def user_display(name: str | None, username: str | None) -> str:
    name = (name or "").strip()
    if username:
        return f"{name} (@{username})" if name else f"@{username}"
    return name or "—"


def status_label(key: str | None) -> str:
    if not key or key == NO_STATUS:
        return "Без подій"
    return event_types.get(key).label


def status_emoji(key: str | None) -> str:
    return event_types.get(key).emoji if key else "▫️"


def odoo_web_url(base_url: str, odoo_id: int) -> str:
    return f"{base_url}/web#id={odoo_id}&model=account.move&view_type=form"


def odoo_line(inv: dict, odoo_url: str = "") -> str:
    st, oid = inv.get("odoo_status") or "", inv.get("odoo_id")
    ref = f"рахунок #{oid}"
    if oid and odoo_url:
        ref = f'<a href="{esc(odoo_web_url(odoo_url, oid))}">{ref}</a>'
    if st == "sent":
        return f"✅ {ref}"
    if st == "modified":
        return f"⚠️ змінено після відправки ({ref})"
    if st == "error":
        return f"❌ помилка: {esc(truncate(inv.get('odoo_error') or '', 200))}"
    return "➖ не відправлено"


def event_line(ev: dict, currency: str = "UAH", full: bool = False) -> str:
    et = event_types.get(ev["event_type"])
    parts = [f"{fmt_date(ev['event_date']) if full else fmt_short_date(ev['event_date'])} {et.label}"]
    if ev.get("amount") is not None:
        parts.append(fmt_money(ev["amount"], currency))
    if ev.get("comment"):
        parts.append(f"«{esc(truncate(ev['comment'], 300 if full else 30))}»")
    line = " — ".join(parts)
    if ev.get("file_id"):
        line += " 📎"
    if full:
        line += f"\n      👤 {esc(ev.get('user_name') or '—')}"
    return line


# ---------------------------------------------------------------- картка накладної


def invoice_card(inv: dict, items: list[dict], events: list[dict], *, header: str = "",
                 odoo_enabled: bool = False, odoo_url: str = "") -> str:
    cur = inv.get("currency") or "UAH"
    lines = []
    if header:
        lines.append(header)
    if inv.get("deleted_at"):
        lines.append(f"🗑 <b>Видалено</b> {fmt_datetime(inv['deleted_at'])} — {esc(inv.get('deleted_by') or '')}")
    lines.append(f"🧾 <b>Накладна № {esc(inv.get('number') or '—')}</b>  (запис #{inv['id']})")
    lines.append(f"📅 Дата виписки: {fmt_date(inv.get('invoice_date'))}")
    lines.append(f"📨 Надіслано в бот: {fmt_datetime(inv.get('sent_at'))} — "
                 f"{esc(user_display(inv.get('user_name'), inv.get('user_username')))}")
    lines.append(f"🏢 Контрагент: {esc(inv.get('counterparty_name') or '—')}")
    lines.append(f"🆔 ЄДРПОУ/ІПН: {esc(inv.get('counterparty_code') or '—')}")
    sup, buy = inv.get("supplier_name") or "", inv.get("buyer_name") or ""
    if sup or buy:
        lines.append(f"↗️ Постачальник: {esc(sup or '—')}" + (f" ({esc(inv['supplier_code'])})" if inv.get("supplier_code") else ""))
        lines.append(f"↘️ Покупець: {esc(buy or '—')}" + (f" ({esc(inv['buyer_code'])})" if inv.get("buyer_code") else ""))
    lines.append(f"💰 Сума з ПДВ: {fmt_money(inv.get('total'), cur)}")
    lines.append(f"🧮 ПДВ: {fmt_money(inv.get('vat'), cur)}")
    if inv.get("status"):
        lines.append(f"📌 Статус: {status_label(inv['status'])} · {fmt_date(inv.get('status_date'))}")
    else:
        lines.append("📌 Статус: без подій")
    if odoo_enabled:
        lines.append(f"🔗 Odoo: {odoo_line(inv, odoo_url)}")
    if events:
        lines.append("")
        lines.append("📌 <b>Рух накладної</b> (останні події):")
        lines += [event_line(e, cur) for e in events[-3:]]
    head = "\n".join(lines)

    if not items:
        return head
    item_lines = [_item_line(n, it, cur) for n, it in enumerate(items, 1)]
    text = head + f"\n\n📦 <b>Товари ({len(items)}):</b>\n"
    budget = TG_LIMIT - len(text) - 80
    shown = []
    for ln in item_lines:
        if len(ln) + 1 > budget:
            break
        shown.append(ln)
        budget -= len(ln) + 1
    text += "\n".join(shown)
    if len(shown) < len(item_lines):
        text += f"\n… і ще {len(item_lines) - len(shown)} поз. (повний перелік — в Excel)"
    return text


def _item_line(n: int, it: dict, cur: str) -> str:
    s = f"{n}. {esc(truncate(it.get('name') or '—', 120))}"
    qty = fmt_qty(it.get("quantity"))
    if qty:
        s += f" — {qty} {esc(it.get('unit') or '')}".rstrip()
        if it.get("price") is not None:
            s += f" × {fmt_money(it['price'], cur)}"
    if it.get("total") is not None:
        s += f" = {fmt_money(it['total'], cur)}"
    return s


def invoice_keyboard(inv: dict, odoo_enabled: bool = False) -> InlineKeyboardMarkup:
    i = inv["id"]
    if inv.get("deleted_at"):
        return InlineKeyboardMarkup([[Btn("↩️ Відновити", callback_data=f"rs:{i}"),
                                      Btn("🕓 Історія змін", callback_data=f"hi:{i}")]])
    rows = [
        [Btn("🖼 Фото", callback_data=f"ph:{i}"), Btn("✏️ Редагувати", callback_data=f"ed:{i}")],
        [Btn("➕ Подія", callback_data=f"ev:{i}"), Btn("📌 Рух накладної", callback_data=f"mv:{i}")],
        [Btn("🕓 Історія змін", callback_data=f"hi:{i}"), Btn("🗑 Видалити", callback_data=f"dl:{i}")],
    ]
    if odoo_enabled:
        if inv.get("odoo_id"):
            rows.append([Btn("🔄 Оновити в Odoo", callback_data=f"os:{i}")])
        else:
            rows.append([Btn("📤 Відправити в Odoo", callback_data=f"os:{i}")])
    return InlineKeyboardMarkup(rows)


def edit_keyboard(invoice_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, (title, _) in EDITABLE_FIELDS.items():
        row.append(Btn(title, callback_data=f"ef:{invoice_id}:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("✖️ Скасувати", callback_data="cx")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard(extra: list[Btn] | None = None) -> InlineKeyboardMarkup:
    rows = [extra] if extra else []
    rows.append([Btn("✖️ Скасувати", callback_data="cx")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(yes_data: str, yes_text: str = "🗑 Так, видалити") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(yes_text, callback_data=yes_data), Btn("✖️ Ні", callback_data="cx")]])


# ---------------------------------------------------------------- події


def event_type_keyboard(invoice_id: int) -> InlineKeyboardMarkup:
    rows, row = [], []
    for et in event_types.EVENT_TYPES:
        row.append(Btn(et.label, callback_data=f"et:{invoice_id}:{et.key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("✖️ Скасувати", callback_data="cx")])
    return InlineKeyboardMarkup(rows)


def movement_view(inv: dict, events: list[dict], can_delete) -> tuple[str, InlineKeyboardMarkup]:
    cur = inv.get("currency") or "UAH"
    title = f"📌 <b>Рух накладної № {esc(inv.get('number') or '—')}</b> (#{inv['id']})"
    if not events:
        text = title + "\n\nПодій ще немає. Натисніть «➕ Подія», щоб додати."
        return text, InlineKeyboardMarkup([[Btn("➕ Подія", callback_data=f"ev:{inv['id']}"),
                                            Btn("🧾 Картка", callback_data=f"inv:{inv['id']}")]])
    lines = [title, ""]
    buttons = []
    for n, ev in enumerate(events, 1):
        lines.append(f"{n}. {event_line(ev, cur, full=True)}")
        row = []
        if ev.get("file_id"):
            row.append(Btn(f"📎 {n}", callback_data=f"ea:{ev['id']}"))
        if can_delete(ev):
            row.append(Btn(f"❌ {n}", callback_data=f"edl:{ev['id']}"))
        if row:
            buttons.append(row)
    text = "\n".join(lines)
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 20] + "\n…"
    # кнопки по 4 в ряд, щоб не роздувати клавіатуру
    flat = [b for r in buttons for b in r]
    rows = [flat[k:k + 4] for k in range(0, len(flat), 4)]
    rows.append([Btn("➕ Подія", callback_data=f"ev:{inv['id']}"), Btn("🧾 Картка", callback_data=f"inv:{inv['id']}")])
    return text, InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- історія та журнал


def fmt_value(field: str, value: str | None) -> str:
    if value is None or value == "":
        return "(порожньо)"
    kind = EDITABLE_FIELDS.get(field, ("", "text"))[1]
    if kind == "money":
        try:
            return fmt_money(float(value))
        except ValueError:
            return esc(value)
    if kind == "date":
        return fmt_date(value) if iso_to_date(value) else esc(value)
    return esc(value)


def history_entry(h: dict) -> str:
    action = h["action"]
    if action == "edited":
        title = EDITABLE_FIELDS.get(h["field"], (h["field"], ""))[0]
        return f"✏️ {esc(title)}: {fmt_value(h['field'], h['old_value'])} → {fmt_value(h['field'], h['new_value'])}"
    if action == "event_added":
        return f"📌 Додано подію: {esc(h['new_value'])}"
    if action == "event_deleted":
        return f"❌ Видалено подію: {esc(h['old_value'])}"
    if action in ("odoo_sent", "odoo_updated"):
        return f"{ACTIONS[action]}" + (f": {esc(h['new_value'])}" if h.get("new_value") else "")
    if action == "odoo_error":
        return f"{ACTIONS[action]}: {esc(truncate(h.get('new_value') or '', 200))}"
    return ACTIONS.get(action, action)


def history_view(inv: dict, rows: list[dict]) -> str:
    lines = [f"🕓 <b>Історія змін накладної № {esc(inv.get('number') or '—')}</b> (#{inv['id']})", ""]
    for h in rows:
        lines.append(f"{fmt_datetime(h['created_at'])} — {esc(h.get('user_name') or '—')}")
        lines.append(f"    {history_entry(h)}")
    return _fit_tail(lines, keep_head=2)


def journal_view(rows: list[dict]) -> str:
    if not rows:
        return "🕓 Журнал дій порожній."
    lines = ["🕓 <b>Журнал дій</b> (останні 20)", ""]
    for h in rows:
        ref = f"#{h['invoice_id']}"
        num = f" №{esc(h['invoice_number'])}" if h.get("invoice_number") else ""
        lines.append(f"{fmt_datetime(h['created_at'])} — {esc(h.get('user_name') or '—')} · {ref}{num}")
        lines.append(f"    {history_entry(h)}")
    lines.append("")
    lines.append("Щоб відкрити запис, напишіть у чат, наприклад, <code>#12</code>")
    return _fit_tail(lines, keep_head=2)


def _fit_tail(lines: list[str], keep_head: int) -> str:
    """Якщо текст задовгий — лишаємо заголовок і найновіші рядки."""
    text = "\n".join(lines)
    if len(text) <= TG_LIMIT:
        return text
    head, body = lines[:keep_head], lines[keep_head:]
    while body and len("\n".join(head + ["…"] + body)) > TG_LIMIT:
        body = body[2:]
    return "\n".join(head + ["…"] + body)


# ---------------------------------------------------------------- список і фільтри


def filter_description(flt: InvoiceFilter) -> str:
    if flt.is_empty:
        return "🔎 Фільтр: усі накладні"
    parts = []
    if flt.cp_key:
        parts.append(f"🏢 {esc(truncate(flt.cp_label, 60))}")
    if flt.date_from or flt.date_to:
        if flt.period_label:
            parts.append(f"📅 {esc(flt.period_label)}")
        else:
            parts.append(f"📅 {fmt_date(flt.date_from)} – {fmt_date(flt.date_to)}")
    if flt.status:
        parts.append(f"📌 {status_label(flt.status)}")
    if flt.query:
        parts.append(f"🔎 «{esc(truncate(flt.query, 40))}»")
    return "🔎 Фільтр: " + " · ".join(parts)


def list_button_text(inv: dict) -> str:
    return (f"{status_emoji(inv.get('status'))} {fmt_date(inv.get('invoice_date'))} · "
            f"№{truncate(inv.get('number') or '—', 15)} · {truncate(inv.get('counterparty_name') or '—', 24)} · "
            f"{fmt_money(inv.get('total'), inv.get('currency'))}")


def list_view(rows: list[dict], page: int, total: int, flt: InvoiceFilter) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, -(-total // PAGE_SIZE))
    text = filter_description(flt) + f"\n\n📋 Знайдено накладних: <b>{total}</b>"
    if not total:
        text += "\n\nНічого не знайдено. Спробуйте змінити фільтр ♻️ або надішліть фото накладної."
    kb = [[Btn(list_button_text(r), callback_data=f"inv:{r['id']}")] for r in rows]
    if pages > 1:
        kb.append([
            Btn("◀️", callback_data=f"lp:{page - 1}" if page > 0 else "noop"),
            Btn(f"{page + 1}/{pages}", callback_data="noop"),
            Btn("▶️", callback_data=f"lp:{page + 1}" if page + 1 < pages else "noop"),
        ])
    kb.append([Btn("📊 Підсумки", callback_data="sum"), Btn("📥 Excel", callback_data="xls"),
               Btn("♻️ Скинути", callback_data="rst")])
    return text, InlineKeyboardMarkup(kb)


def counterparties_view(rows: list[dict], page: int, total: int, query: str = "") -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, -(-total // PAGE_SIZE))
    text = "🏢 <b>Оберіть контрагента</b>"
    if query:
        text += f"\nПошук: «{esc(query)}»"
    text += f"\nУсього: {total}"
    if not total:
        text += "\n\nНічого не знайдено."
    kb = []
    for r in rows:
        label = truncate(r["name"], 34) + (f" · {r['code']}" if r.get("code") else "") + f" ({r['n']})"
        kb.append([Btn(label, callback_data=f"cp:{r['rep_id']}")])
    if pages > 1:
        kb.append([
            Btn("◀️", callback_data=f"cpl:{page - 1}" if page > 0 else "noop"),
            Btn(f"{page + 1}/{pages}", callback_data="noop"),
            Btn("▶️", callback_data=f"cpl:{page + 1}" if page + 1 < pages else "noop"),
        ])
    kb.append([Btn("🔎 Пошук за назвою/ЄДРПОУ", callback_data="cps")])
    kb.append([Btn("🏢 Усі контрагенти (зняти)", callback_data="cpa")])
    return text, InlineKeyboardMarkup(kb)


PERIODS = [("today", "Сьогодні"), ("week", "Цей тиждень"), ("month", "Цей місяць"),
           ("prev_month", "Минулий місяць"), ("year", "Цей рік"), ("any", "Будь-яка дата")]


def period_keyboard() -> InlineKeyboardMarkup:
    rows = [[Btn(PERIODS[k][1], callback_data=f"pr:{PERIODS[k][0]}"),
             Btn(PERIODS[k + 1][1], callback_data=f"pr:{PERIODS[k + 1][0]}")] for k in range(0, len(PERIODS), 2)]
    rows.append([Btn("✍️ Свій період", callback_data="pr:custom")])
    return InlineKeyboardMarkup(rows)


def status_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    for et in event_types.EVENT_TYPES:
        n = counts.get(et.key, 0)
        if n:
            rows.append([Btn(f"{et.label} ({n})", callback_data=f"st:{et.key}")])
    rows.append([Btn(f"▫️ Без подій ({counts.get(NO_STATUS, 0)})", callback_data=f"st:{NO_STATUS}")])
    rows.append([Btn("📌 Будь-який статус (зняти)", callback_data="st:*")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- підсумки

SUMMARY_EVENT_KEYS = ["paid", "partial_paid", "refund", "goods_return", "goods_lost", "damaged",
                      "correction", "claim", "other"]


def summary_view(s: dict, flt: InvoiceFilter) -> str:
    lines = [filter_description(flt), "", "📊 <b>Підсумки</b>",
             f"🧾 Накладних: <b>{s['count']}</b>",
             f"💰 Сума з ПДВ: <b>{fmt_money(s['total'])}</b>",
             f"🧮 ПДВ: {fmt_money(s['vat'])}",
             f"📄 Без ПДВ: {fmt_money(s['net'])}"]
    if s["top"] and not flt.cp_key:
        lines += ["", "🏆 <b>Топ-10 контрагентів за сумою:</b>"]
        for n, r in enumerate(s["top"], 1):
            lines.append(f"{n}. {esc(truncate(r['name'] or r['code'] or '—', 40))} — {fmt_money(r['total'])} ({r['n']})")
    ev_lines = []
    for key in SUMMARY_EVENT_KEYS:
        if key in s["events"]:
            e = s["events"][key]
            ev_lines.append(f"{event_types.get(key).label}: {fmt_money(e['amount'])} ({e['count']})")
    if ev_lines:
        lines += ["", "📌 <b>За подіями руху:</b>"] + ev_lines
    return "\n".join(lines)
