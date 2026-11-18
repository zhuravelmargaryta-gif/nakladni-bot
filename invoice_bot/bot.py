"""Обробники Telegram: фото → картка, меню, фільтри, редагування, події, Odoo."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          Defaults, MessageHandler, filters)

from . import cards, event_types, parsing
from .config import Config
from .db import EDITABLE_FIELDS, Database, InvoiceFilter, now_str
from .export import build_workbook
from .odoo import Attachment, OdooError, OdooSync
from .recognition import RecognitionError, normalize

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 20 * 1024 * 1024  # ліміт Bot API на завантаження файлів
ERROR_TEXT = "😔 Щось пішло не так. Спробуйте ще раз, а якщо повториться — напишіть адміністратору."
NOT_INVOICE_TEXT = (
    "🤔 Схоже, на фото не накладна, або її не вдалося прочитати.\n\n"
    "Порада: сфотографуйте документ рівно, при хорошому освітленні, щоб увесь аркуш потрапив у кадр "
    "і текст був різким. Можна надіслати фото файлом (без стиснення) або PDF."
)


def user_of(update: Update) -> dict:
    u = update.effective_user
    return {"id": u.id, "name": u.full_name or u.username or str(u.id), "username": u.username or ""}


class InvoiceBot:
    def __init__(self, cfg: Config, db: Database, recognizer=None, odoo: OdooSync | None = None):
        self.cfg = cfg
        self.db = db
        self.recognizer = recognizer
        self.odoo = odoo if cfg.odoo_enabled else None
        self._bg_tasks: set[asyncio.Task] = set()
        self._bot = None

    # ================================================================ збірка

    def build(self) -> Application:
        defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
        app = ApplicationBuilder().token(self.cfg.bot_token).defaults(defaults).concurrent_updates(True).build()
        app.add_handler(CommandHandler("myid", self.cmd_myid))
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("odoo_test", self.cmd_odoo_test))
        app.add_handler(CommandHandler("odoo_sync", self.cmd_odoo_sync))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_error_handler(self.on_error)
        self._bot = app.bot
        return app

    # ================================================================ доступ

    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        if user and self.cfg.is_allowed(user.id):
            return True
        uid = user.id if user else "?"
        log.info("Відмовлено в доступі: %s", uid)
        text = f"⛔ Немає доступу.\nВаш ID: <code>{uid}</code>\nПередайте його адміністратору бота."
        if update.callback_query:
            await update.callback_query.answer("⛔ Немає доступу", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
        return False

    def can_modify(self, user_id: int, owner_id: int | None) -> bool:
        return self.cfg.is_admin(user_id) or (owner_id is not None and owner_id == user_id)

    # ================================================================ команди

    async def cmd_myid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        await update.message.reply_text(f"🆔 Ваш Telegram ID: <code>{u.id}</code>")

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        ctx.user_data.pop("state", None)
        name = parsing.esc(update.effective_user.first_name or "")
        await update.message.reply_text(
            f"👋 Вітаю, {name}!\n\nНадішліть мені <b>фото накладної</b> — я розпізнаю реквізити і збережу її.\n"
            f"Меню внизу — для перегляду, фільтрів, підсумків і Excel.", reply_markup=cards.main_menu())

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = cards.HELP_TEXT
        if self.odoo:
            text += ", /odoo_test" + (", /odoo_sync" if self.cfg.is_admin(update.effective_user.id) else "")
        await update.message.reply_text(text, reply_markup=cards.main_menu())

    async def cmd_odoo_test(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not self.odoo:
            await update.message.reply_text("🔗 Odoo не підключено: заповніть ODOO_URL, ODOO_DB, ODOO_USER, "
                                            "ODOO_API_KEY у файлі .env і перезапустіть бота.")
            return
        msg = await update.message.reply_text("🔗 Перевіряю з'єднання з Odoo…")
        try:
            info = await asyncio.to_thread(self.odoo.test)
            await self._edit(msg, f"✅ З'єднання з Odoo працює.\n{parsing.esc(info)}")
        except OdooError as exc:
            await self._edit(msg, f"❌ Не вдалося підключитися до Odoo:\n{parsing.esc(exc)}")
        except Exception:
            log.exception("odoo_test")
            await self._edit(msg, "❌ Не вдалося підключитися до Odoo. Подробиці — в журналі бота.")

    async def cmd_odoo_sync(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not self.odoo:
            await update.message.reply_text("🔗 Odoo не підключено.")
            return
        user = user_of(update)
        if not self.cfg.is_admin(user["id"]):
            await update.message.reply_text("⛔ Ця команда лише для адміністраторів.")
            return
        todo = self.db.invoices_for_sync()
        if not todo:
            await update.message.reply_text("✅ Усе вже синхронізовано з Odoo.")
            return
        msg = await update.message.reply_text(f"🔄 Відправляю в Odoo: {len(todo)} накладних…")
        ok, errors = 0, []
        for inv in todo:
            err = await self.odoo_push(inv["id"], user, ctx)
            if err:
                errors.append(f"#{inv['id']} №{parsing.esc(inv['number'])}: {parsing.esc(parsing.truncate(err, 120))}")
            else:
                ok += 1
        text = f"🔗 Синхронізація з Odoo завершена.\n✅ Успішно: {ok}\n❌ Помилок: {len(errors)}"
        if errors:
            text += "\n\n" + "\n".join(errors[:20])
        await self._edit(msg, parsing.truncate(text, cards.TG_LIMIT))

    # ================================================================ фото / файли

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        msg = update.message
        file_id, kind, mime, name, size = self._file_info(msg)
        state = ctx.user_data.get("state")

        # Посеред діалогу «Подія» файл — це вкладення до події, а не нова накладна
        if state and state.get("mode") == "event":
            if not file_id:
                await msg.reply_text("📎 Можна додати фото або документ (PDF, Word, Excel…).")
                return
            state["file_id"], state["file_kind"] = file_id, kind
            if msg.caption:
                state["comment"] = msg.caption.strip()
            if state["step"] == "comment":
                await self._save_event(update, ctx)
            else:
                await msg.reply_text("📎 Вкладення додано до події. Продовжуйте 👇")
                await self._event_prompt(update, ctx)
            return
        ctx.user_data.pop("state", None)

        if not file_id or not (mime.startswith("image/") or mime == "application/pdf"):
            await msg.reply_text("📄 Надішліть, будь ласка, фото накладної, зображення або PDF.")
            return
        if size and size > MAX_DOWNLOAD:
            await msg.reply_text("📦 Файл завеликий (більше 20 МБ). Стисніть його або надішліть фото.")
            return
        if not self.recognizer:
            await msg.reply_text("⚙️ Розпізнавання не налаштоване: адміністратору потрібно вказати GEMINI_API_KEY у .env.")
            return

        status = await msg.reply_text("🔍 Розпізнаю…")
        try:
            tg_file = await ctx.bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
            result = await self.recognizer.recognize(data, mime)
        except RecognitionError as exc:
            await self._edit(status, f"😔 Не вдалося розпізнати накладну.\n{parsing.esc(exc)}")
            return
        except Exception:
            log.exception("Помилка завантаження/розпізнавання")
            await self._edit(status, "😔 Не вдалося обробити файл. Спробуйте надіслати його ще раз.")
            return

        fields = normalize(result, self.cfg.our_company_code)
        if not result.is_invoice or not any([fields["number"], fields["total"], fields["counterparty_name"]]):
            await self._edit(status, NOT_INVOICE_TEXT)
            return

        user = user_of(update)
        items = fields.pop("items")
        dups = self.db.find_duplicates(fields["number"], fields["counterparty_code"], fields["counterparty_name"])
        fields.update({
            "sent_at": now_str(), "user_id": user["id"], "user_name": user["name"], "user_username": user["username"],
            "photo_file_id": file_id, "photo_kind": kind, "photo_mime": mime, "photo_name": name,
            "is_duplicate": 1 if dups else 0,
        })
        inv_id = self.db.add_invoice(fields, items, user)
        header = "✅ <b>Збережено!</b>"
        if dups:
            refs = ", ".join(f"#{d['id']}" for d in dups[:5])
            header += (f"\n⚠️ <b>Можливий дублікат:</b> накладна № {parsing.esc(fields['number'])} від цього "
                       f"контрагента вже є ({refs}). Якщо це помилка — видаліть цей запис 🗑")
        await self._show_card(inv_id, header=header, edit=status)

        if self.odoo and self.cfg.odoo_auto_sync and not dups:
            await self.odoo_push(inv_id, user, ctx)
            await self._show_card(inv_id, header=header, edit=status)

    @staticmethod
    def _file_info(msg: Message) -> tuple[str, str, str, str, int]:
        if msg.photo:
            p = msg.photo[-1]
            return p.file_id, "photo", "image/jpeg", f"photo_{msg.message_id}.jpg", p.file_size or 0
        if msg.document:
            d = msg.document
            mime = d.mime_type or mimetypes.guess_type(d.file_name or "")[0] or "application/octet-stream"
            return d.file_id, "document", mime, d.file_name or f"file_{msg.message_id}", d.file_size or 0
        return "", "", "", "", 0

    # ================================================================ текст

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = (update.message.text or "").strip()
        if text in cards.MENU_BUTTONS:
            ctx.user_data.pop("state", None)
            await self._menu(update, ctx, text)
            return
        state = ctx.user_data.get("state")
        if state:
            mode = state.get("mode")
            if mode == "edit":
                return await self._edit_value(update, ctx, text)
            if mode == "event":
                return await self._event_text(update, ctx, text)
            if mode == "period":
                return await self._period_text(update, ctx, text)
            if mode == "cp_search":
                ctx.user_data.pop("state", None)
                ctx.user_data["cp_query"] = text
                return await self._show_counterparties(update, ctx, 0)
        m = re.fullmatch(r"#\s*(\d+)", text)
        if m:
            inv_id = int(m.group(1))
            if not self.db.get_invoice(inv_id):
                await update.message.reply_text(f"🔎 Запису #{inv_id} немає.")
                return
            return await self._show_card(inv_id, chat_id=update.effective_chat.id)
        # будь-який інший текст — пошук
        flt = self.db.get_filter(update.effective_user.id)
        flt.query = text[:100]
        self.db.save_filter(update.effective_user.id, flt)
        await self._show_list(update, ctx, 0)

    async def _menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, item: str):
        uid = update.effective_user.id
        if item == cards.M_ALL:
            await self._show_list(update, ctx, 0)
        elif item == cards.M_CP:
            ctx.user_data.pop("cp_query", None)
            await self._show_counterparties(update, ctx, 0)
        elif item == cards.M_PERIOD:
            await update.message.reply_text("📅 Оберіть період (за датою виписки):", reply_markup=cards.period_keyboard())
        elif item == cards.M_STATUS:
            await self._show_status_menu(update, ctx)
        elif item == cards.M_SUMMARY:
            await self._show_summary(update, ctx)
        elif item == cards.M_EXCEL:
            await self._send_excel(update, ctx)
        elif item == cards.M_JOURNAL:
            await update.message.reply_text(cards.journal_view(self.db.journal(20)))
        elif item == cards.M_RESET:
            self.db.save_filter(uid, InvoiceFilter())
            await update.message.reply_text("♻️ Фільтр скинуто.")
            await self._show_list(update, ctx, 0)
        elif item == cards.M_HELP:
            await self.cmd_help(update, ctx)

    # ================================================================ кнопки

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        q = update.callback_query
        data = q.data or ""
        cmd, _, arg = data.partition(":")
        user = user_of(update)
        handler = {
            "inv": self._cb_open, "ph": self._cb_photo, "ed": self._cb_edit_menu, "ef": self._cb_edit_field,
            "ev": self._cb_event_menu, "et": self._cb_event_type, "evs": self._cb_event_skip_amount,
            "evd": self._cb_event_date, "evn": self._cb_event_no_comment, "mv": self._cb_movement,
            "ea": self._cb_event_attachment, "edl": self._cb_event_delete_ask, "edy": self._cb_event_delete,
            "hi": self._cb_history, "dl": self._cb_delete_ask, "dly": self._cb_delete, "rs": self._cb_restore,
            "os": self._cb_odoo, "lp": self._cb_list_page, "cpl": self._cb_cp_page, "cp": self._cb_cp_select,
            "cps": self._cb_cp_search, "cpa": self._cb_cp_all, "pr": self._cb_period, "st": self._cb_status,
            "sum": self._cb_summary, "xls": self._cb_excel, "rst": self._cb_reset, "cx": self._cb_cancel,
        }.get(cmd)
        if not handler:
            await q.answer()
            return
        await handler(update, ctx, arg, user)

    # ---- картка
    async def _get_inv_or_alert(self, update: Update, arg: str) -> dict | None:
        inv = self.db.get_invoice(int(arg)) if arg.isdigit() else None
        if not inv:
            await update.callback_query.answer("Запис не знайдено", show_alert=True)
        return inv

    async def _check_owner(self, update: Update, inv: dict, user: dict) -> bool:
        if self.can_modify(user["id"], inv.get("user_id")):
            return True
        await update.callback_query.answer("⛔ Змінювати можна лише власні накладні (або адміністратор).",
                                           show_alert=True)
        return False

    async def _cb_open(self, update, ctx, arg, user):
        if await self._get_inv_or_alert(update, arg):
            await update.callback_query.answer()
            await self._show_card(int(arg), chat_id=update.effective_chat.id)

    async def _cb_photo(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv:
            return
        await update.callback_query.answer()
        caption = f"🧾 Накладна № {parsing.esc(inv['number'] or '—')} (#{inv['id']})"
        if not inv.get("photo_file_id"):
            await ctx.bot.send_message(update.effective_chat.id, "🖼 Фото для цього запису немає.")
        elif inv.get("photo_kind") == "photo":
            await ctx.bot.send_photo(update.effective_chat.id, inv["photo_file_id"], caption=caption)
        else:
            await ctx.bot.send_document(update.effective_chat.id, inv["photo_file_id"], caption=caption)

    async def _cb_history(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if inv:
            await update.callback_query.answer()
            await ctx.bot.send_message(update.effective_chat.id, cards.history_view(inv, self.db.history(inv["id"])),
                                       reply_markup=InlineKeyboardMarkup([[Btn("🧾 Картка", callback_data=f"inv:{inv['id']}")]]))

    # ---- редагування
    async def _cb_edit_menu(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv or not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer()
        await ctx.bot.send_message(update.effective_chat.id,
                                   f"✏️ Що змінити в накладній № {parsing.esc(inv['number'] or '—')} (#{inv['id']})?",
                                   reply_markup=cards.edit_keyboard(inv["id"]))

    async def _cb_edit_field(self, update, ctx, arg, user):
        inv_id, _, field = arg.partition(":")
        inv = await self._get_inv_or_alert(update, inv_id)
        if not inv or field not in EDITABLE_FIELDS or not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer()
        title, kind = EDITABLE_FIELDS[field]
        current = cards.fmt_value(field, None if inv[field] is None else str(inv[field]))
        hint = {"date": "у форматі ДД.ММ.РРРР, напр. 05.10.2026", "money": "числом, напр. 1 200,50",
                "code": "лише цифри (8–10), або «-», щоб очистити", "text": "текстом"}[kind]
        ctx.user_data["state"] = {"mode": "edit", "invoice_id": inv["id"], "field": field}
        await self._edit(update.callback_query.message,
                         f"✏️ <b>{title}</b>\nЗараз: {current}\n\nНадішліть нове значення {hint}.",
                         reply_markup=cards.cancel_keyboard())

    async def _edit_value(self, update: Update, ctx, text: str):
        state = ctx.user_data["state"]
        field = state["field"]
        kind = EDITABLE_FIELDS[field][1]
        value, error = parse_field_value(kind, text)
        if error:
            await update.message.reply_text(error, reply_markup=cards.cancel_keyboard())
            return
        inv = self.db.get_invoice(state["invoice_id"])
        user = user_of(update)
        if not inv or not self.can_modify(user["id"], inv.get("user_id")):
            ctx.user_data.pop("state", None)
            await update.message.reply_text("⛔ Неможливо змінити цей запис.")
            return
        ctx.user_data.pop("state", None)
        old, new = self.db.update_field(inv["id"], field, value, user)
        title = EDITABLE_FIELDS[field][0]
        header = (f"✅ Змінено «{title}»: {cards.fmt_value(field, '' if old is None else str(old))} → "
                  f"{cards.fmt_value(field, '' if new is None else str(new))}")
        if self.db.get_invoice(inv["id"]).get("odoo_status") == "modified":
            header += "\n⚠️ Накладна вже була в Odoo — натисніть «🔄 Оновити в Odoo»."
        await self._show_card(inv["id"], header=header, chat_id=update.effective_chat.id)

    # ---- видалення / відновлення
    async def _cb_delete_ask(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv or not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer()
        text = (f"🗑 Видалити накладну № {parsing.esc(inv['number'] or '—')} (#{inv['id']})?\n"
                f"Вона зникне зі списків, підсумків і Excel, але її можна буде відновити.")
        if inv.get("odoo_id"):
            text += f"\n\n⚠️ У Odoo рахунок #{inv['odoo_id']} <b>не видаляється</b> — за потреби видаліть його там вручну."
        await ctx.bot.send_message(update.effective_chat.id, text, reply_markup=cards.confirm_keyboard(f"dly:{inv['id']}"))

    async def _cb_delete(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv or not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer("Видалено")
        self.db.soft_delete(inv["id"], user)
        await self._show_card(inv["id"], header="🗑 Накладну видалено.", edit=update.callback_query.message)

    async def _cb_restore(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv or not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer("Відновлено")
        self.db.restore(inv["id"], user)
        await self._show_card(inv["id"], header="↩️ Накладну відновлено.", chat_id=update.effective_chat.id)

    # ---- події руху накладної
    async def _cb_event_menu(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv:
            return
        await update.callback_query.answer()
        ctx.user_data.pop("state", None)
        await ctx.bot.send_message(update.effective_chat.id,
                                   f"➕ Нова подія для накладної № {parsing.esc(inv['number'] or '—')} (#{inv['id']}).\n"
                                   f"Оберіть тип:", reply_markup=cards.event_type_keyboard(inv["id"]))

    async def _cb_event_type(self, update, ctx, arg, user):
        inv_id, _, key = arg.partition(":")
        inv = await self._get_inv_or_alert(update, inv_id)
        if not inv or key not in event_types.BY_KEY:
            return
        await update.callback_query.answer()
        et = event_types.BY_KEY[key]
        ctx.user_data["state"] = {"mode": "event", "invoice_id": inv["id"], "type": key,
                                  "step": "amount" if et.has_amount else "date"}
        await self._event_prompt(update, ctx, edit=update.callback_query.message)

    async def _event_prompt(self, update: Update, ctx, edit: Message | None = None):
        st = ctx.user_data["state"]
        et = event_types.get(st["type"])
        title = f"{et.label}\n\n"
        if st["step"] == "amount":
            hint = " (можна від'ємну)" if st["type"] == "correction" else ""
            text = title + f"💰 Введіть суму{hint}, напр. 1 200,50 — або натисніть «Пропустити»."
            kb = cards.cancel_keyboard([Btn("⏭ Пропустити", callback_data="evs")])
        elif st["step"] == "date":
            text = title + "📅 Дата події? Оберіть або введіть у форматі ДД.ММ.РРРР."
            kb = cards.cancel_keyboard([Btn("Сьогодні", callback_data="evd:0"), Btn("Вчора", callback_data="evd:1")])
        else:
            text = title + ("💬 Напишіть коментар (напр. номер ТТН, причину) та/або надішліть фото чи документ "
                            "(підпис до фото = коментар).")
            kb = cards.cancel_keyboard([Btn("💾 Зберегти без коментаря", callback_data="evn")])
        if edit:
            await self._edit(edit, text, reply_markup=kb)
        else:
            await ctx.bot.send_message(update.effective_chat.id, text, reply_markup=kb)

    async def _event_text(self, update: Update, ctx, text: str):
        st = ctx.user_data["state"]
        if st["step"] == "amount":
            amount = parsing.parse_amount(text)
            if amount is None:
                await update.message.reply_text("🔢 Не схоже на суму. Приклад: 1 200,50",
                                                reply_markup=cards.cancel_keyboard([Btn("⏭ Пропустити", callback_data="evs")]))
                return
            st["amount"], st["step"] = amount, "date"
            await self._event_prompt(update, ctx)
        elif st["step"] == "date":
            d = parsing.parse_date(text)
            if not d:
                await update.message.reply_text("📅 Не схоже на дату. Приклад: 13.11.2026")
                return
            st["date"], st["step"] = d.isoformat(), "comment"
            await self._event_prompt(update, ctx)
        else:
            st["comment"] = text
            await self._save_event(update, ctx)

    async def _cb_event_skip_amount(self, update, ctx, arg, user):
        st = ctx.user_data.get("state")
        if not st or st.get("mode") != "event":
            return await update.callback_query.answer("Діалог уже завершено")
        await update.callback_query.answer()
        st["step"] = "date"
        await self._event_prompt(update, ctx, edit=update.callback_query.message)

    async def _cb_event_date(self, update, ctx, arg, user):
        st = ctx.user_data.get("state")
        if not st or st.get("mode") != "event":
            return await update.callback_query.answer("Діалог уже завершено")
        await update.callback_query.answer()
        st["date"] = (date.today() - timedelta(days=1 if arg == "1" else 0)).isoformat()
        st["step"] = "comment"
        await self._event_prompt(update, ctx, edit=update.callback_query.message)

    async def _cb_event_no_comment(self, update, ctx, arg, user):
        st = ctx.user_data.get("state")
        if not st or st.get("mode") != "event":
            return await update.callback_query.answer("Діалог уже завершено")
        await update.callback_query.answer()
        await self._save_event(update, ctx)

    async def _save_event(self, update: Update, ctx):
        st = ctx.user_data.pop("state")
        user = user_of(update)
        ev_date = st.get("date") or date.today().isoformat()
        self.db.add_event(st["invoice_id"], st["type"], ev_date, user, amount=st.get("amount"),
                          comment=st.get("comment", ""), file_id=st.get("file_id", ""),
                          file_kind=st.get("file_kind", ""))
        inv = self.db.get_invoice(st["invoice_id"])
        if self.odoo and inv.get("odoo_id"):
            et = event_types.get(st["type"])
            note = f"{et.label} · {parsing.fmt_date(ev_date)}"
            if st.get("amount") is not None:
                note += f" · {parsing.fmt_money(st['amount'], inv.get('currency'))}"
            if st.get("comment"):
                note += f"\n{st['comment']}"
            note += f"\n(Telegram-бот, {user['name']})"
            self._background(self._odoo_note(inv["odoo_id"], note))
        await self._show_card(st["invoice_id"], header="✅ Подію додано.", chat_id=update.effective_chat.id)

    async def _cb_movement(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv:
            return
        await update.callback_query.answer()
        await self._send_movement(ctx, update.effective_chat.id, inv, user)

    async def _send_movement(self, ctx, chat_id: int, inv: dict, user: dict, edit: Message | None = None):
        text, kb = cards.movement_view(inv, self.db.events(inv["id"]),
                                       lambda ev: self.can_modify(user["id"], ev.get("user_id")))
        if edit:
            await self._edit(edit, text, reply_markup=kb)
        else:
            await ctx.bot.send_message(chat_id, text, reply_markup=kb)

    async def _cb_event_attachment(self, update, ctx, arg, user):
        ev = self.db.get_event(int(arg)) if arg.isdigit() else None
        if not ev or not ev.get("file_id"):
            return await update.callback_query.answer("Вкладення не знайдено", show_alert=True)
        await update.callback_query.answer()
        caption = f"📎 {event_types.get(ev['event_type']).label} · {parsing.fmt_date(ev['event_date'])}"
        if ev.get("comment"):
            caption += f"\n{parsing.esc(parsing.truncate(ev['comment'], 900))}"
        send = ctx.bot.send_photo if ev.get("file_kind") == "photo" else ctx.bot.send_document
        await send(update.effective_chat.id, ev["file_id"], caption=caption)

    async def _cb_event_delete_ask(self, update, ctx, arg, user):
        ev = self.db.get_event(int(arg)) if arg.isdigit() else None
        if not ev or ev.get("deleted_at"):
            return await update.callback_query.answer("Подію не знайдено", show_alert=True)
        if not self.can_modify(user["id"], ev.get("user_id")):
            return await update.callback_query.answer("⛔ Видалити подію може її автор або адміністратор.",
                                                      show_alert=True)
        await update.callback_query.answer()
        await ctx.bot.send_message(update.effective_chat.id,
                                   f"❌ Видалити подію «{cards.event_line(ev)}»?",
                                   reply_markup=cards.confirm_keyboard(f"edy:{ev['id']}"))

    async def _cb_event_delete(self, update, ctx, arg, user):
        ev = self.db.get_event(int(arg)) if arg.isdigit() else None
        if not ev or ev.get("deleted_at"):
            return await update.callback_query.answer("Подію вже видалено", show_alert=True)
        if not self.can_modify(user["id"], ev.get("user_id")):
            return await update.callback_query.answer("⛔ Немає прав", show_alert=True)
        await update.callback_query.answer("Подію видалено")
        self.db.delete_event(ev["id"], user)
        inv = self.db.get_invoice(ev["invoice_id"])
        if self.odoo and inv.get("odoo_id"):
            self._background(self._odoo_note(
                inv["odoo_id"], f"Видалено подію: {event_types.get(ev['event_type']).label} · "
                                f"{parsing.fmt_date(ev['event_date'])} ({user['name']})"))
        await self._send_movement(ctx, update.effective_chat.id, inv, user, edit=update.callback_query.message)

    # ---- Odoo
    async def _cb_odoo(self, update, ctx, arg, user):
        inv = await self._get_inv_or_alert(update, arg)
        if not inv:
            return
        if not self.odoo:
            return await update.callback_query.answer("Odoo не підключено", show_alert=True)
        if not await self._check_owner(update, inv, user):
            return
        await update.callback_query.answer("🔗 Відправляю в Odoo…")
        err = await self.odoo_push(inv["id"], user, ctx)
        header = f"❌ Odoo: {parsing.esc(err)}" if err else "🔗 Odoo: успішно ✅"
        await self._show_card(inv["id"], header=header, chat_id=update.effective_chat.id)

    async def odoo_push(self, inv_id: int, user: dict, ctx) -> str | None:
        """Відправити/оновити накладну в Odoo. Повертає текст помилки або None."""
        inv = self.db.get_invoice(inv_id)
        items = self.db.items(inv_id)
        attachment = None
        if not inv.get("odoo_id") and inv.get("photo_file_id"):
            try:
                tg_file = await ctx.bot.get_file(inv["photo_file_id"])
                data = bytes(await tg_file.download_as_bytearray())
                ext = mimetypes.guess_extension(inv.get("photo_mime") or "") or ".jpg"
                fname = f"Накладна_{inv.get('number') or inv_id}{ext}".replace("/", "_")
                attachment = Attachment(data, fname, inv.get("photo_mime") or "image/jpeg")
            except Exception:
                log.warning("Не вдалося завантажити фото #%s для Odoo", inv_id, exc_info=True)
        try:
            odoo_id, action = await asyncio.to_thread(self.odoo.push_invoice, inv, items, attachment)
        except OdooError as exc:
            self.db.set_odoo(inv_id, "error", error=str(exc))
            self.db.log(inv_id, "odoo_error", user, new=str(exc))
            log.warning("Odoo, накладна #%s: %s", inv_id, exc)
            return str(exc)
        except Exception as exc:
            log.exception("Odoo, накладна #%s", inv_id)
            msg = "внутрішня помилка під час відправки (подробиці в журналі бота)"
            self.db.set_odoo(inv_id, "error", error=msg)
            self.db.log(inv_id, "odoo_error", user, new=repr(exc)[:300])
            return msg
        self.db.set_odoo(inv_id, "sent", odoo_id=odoo_id)
        self.db.log(inv_id, "odoo_sent" if action == "created" else "odoo_updated", user, new=f"рахунок #{odoo_id}")
        return None

    async def _odoo_note(self, odoo_id: int, text: str):
        try:
            await asyncio.to_thread(self.odoo.post_note, odoo_id, text)
        except Exception:
            log.warning("Не вдалося додати нотатку в Odoo #%s", odoo_id, exc_info=True)

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---- список і фільтри
    async def _cb_list_page(self, update, ctx, arg, user):
        await update.callback_query.answer()
        await self._show_list(update, ctx, int(arg) if arg.lstrip("-").isdigit() else 0,
                              edit=update.callback_query.message)

    async def _cb_cp_page(self, update, ctx, arg, user):
        await update.callback_query.answer()
        await self._show_counterparties(update, ctx, int(arg) if arg.isdigit() else 0, edit=update.callback_query.message)

    async def _cb_cp_select(self, update, ctx, arg, user):
        key, label = self.db.counterparty_of(int(arg)) if arg.isdigit() else ("", "")
        if not key:
            return await update.callback_query.answer("Контрагента не знайдено", show_alert=True)
        await update.callback_query.answer()
        flt = self.db.get_filter(user["id"])
        flt.cp_key, flt.cp_label, flt.query = key, label, ""
        self.db.save_filter(user["id"], flt)
        await self._show_list(update, ctx, 0, edit=update.callback_query.message)

    async def _cb_cp_search(self, update, ctx, arg, user):
        await update.callback_query.answer()
        ctx.user_data["state"] = {"mode": "cp_search"}
        await ctx.bot.send_message(update.effective_chat.id, "🔎 Напишіть частину назви контрагента або ЄДРПОУ:",
                                   reply_markup=cards.cancel_keyboard())

    async def _cb_cp_all(self, update, ctx, arg, user):
        await update.callback_query.answer()
        flt = self.db.get_filter(user["id"]).without("cp_key")
        self.db.save_filter(user["id"], flt)
        await self._show_list(update, ctx, 0, edit=update.callback_query.message)

    async def _cb_period(self, update, ctx, arg, user):
        await update.callback_query.answer()
        if arg == "custom":
            ctx.user_data["state"] = {"mode": "period"}
            await self._edit(update.callback_query.message,
                             "✍️ Введіть період у форматі <code>01.10.2026-31.10.2026</code> або одну дату.",
                             reply_markup=cards.cancel_keyboard())
            return
        d1, d2, label = parsing.period_bounds(arg)
        flt = self.db.get_filter(user["id"])
        flt.date_from = d1.isoformat() if d1 else ""
        flt.date_to = d2.isoformat() if d2 else ""
        flt.period_label = label if d1 else ""
        self.db.save_filter(user["id"], flt)
        await self._show_list(update, ctx, 0, edit=update.callback_query.message)

    async def _period_text(self, update: Update, ctx, text: str):
        period = parsing.parse_period(text)
        if not period:
            await update.message.reply_text("📅 Не вдалося розібрати. Приклад: <code>01.10.2026-31.10.2026</code>",
                                            reply_markup=cards.cancel_keyboard())
            return
        ctx.user_data.pop("state", None)
        d1, d2 = period
        flt = self.db.get_filter(update.effective_user.id)
        flt.date_from, flt.date_to = d1.isoformat(), d2.isoformat()
        flt.period_label = parsing.fmt_date(d1) if d1 == d2 else f"{parsing.fmt_date(d1)} – {parsing.fmt_date(d2)}"
        self.db.save_filter(update.effective_user.id, flt)
        await self._show_list(update, ctx, 0)

    async def _cb_status(self, update, ctx, arg, user):
        await update.callback_query.answer()
        flt = self.db.get_filter(user["id"])
        flt.status = "" if arg == "*" else arg
        self.db.save_filter(user["id"], flt)
        await self._show_list(update, ctx, 0, edit=update.callback_query.message)

    async def _cb_summary(self, update, ctx, arg, user):
        await update.callback_query.answer()
        await self._show_summary(update, ctx)

    async def _cb_excel(self, update, ctx, arg, user):
        await update.callback_query.answer("📥 Готую файл…")
        await self._send_excel(update, ctx)

    async def _cb_reset(self, update, ctx, arg, user):
        await update.callback_query.answer("♻️ Фільтр скинуто")
        self.db.save_filter(user["id"], InvoiceFilter())
        await self._show_list(update, ctx, 0, edit=update.callback_query.message)

    async def _cb_cancel(self, update, ctx, arg, user):
        ctx.user_data.pop("state", None)
        await update.callback_query.answer("Скасовано")
        await self._edit(update.callback_query.message, "✖️ Скасовано.")

    # ================================================================ відображення

    async def _show_card(self, inv_id: int, header: str = "", edit: Message | None = None, chat_id: int | None = None):
        inv = self.db.get_invoice(inv_id)
        text = cards.invoice_card(inv, self.db.items(inv_id), self.db.events(inv_id), header=header,
                                  odoo_enabled=bool(self.odoo), odoo_url=self.cfg.odoo_url)
        kb = cards.invoice_keyboard(inv, bool(self.odoo))
        if edit:
            await self._edit(edit, text, reply_markup=kb)
        else:
            await self._bot.send_message(chat_id, text, reply_markup=kb)

    async def _show_list(self, update: Update, ctx, page: int, edit: Message | None = None):
        flt = self.db.get_filter(update.effective_user.id)
        total = self.db.count_invoices(flt)
        pages = max(1, -(-total // cards.PAGE_SIZE))
        page = min(max(0, page), pages - 1)
        rows = self.db.list_invoices(flt, page * cards.PAGE_SIZE, cards.PAGE_SIZE)
        text, kb = cards.list_view(rows, page, total, flt)
        if edit:
            await self._edit(edit, text, reply_markup=kb)
        else:
            await ctx.bot.send_message(update.effective_chat.id, text, reply_markup=kb)

    async def _show_counterparties(self, update: Update, ctx, page: int, edit: Message | None = None):
        query = ctx.user_data.get("cp_query", "")
        total = self.db.count_counterparties(query)
        pages = max(1, -(-total // cards.PAGE_SIZE))
        page = min(max(0, page), pages - 1)
        rows = self.db.counterparties(query, page * cards.PAGE_SIZE, cards.PAGE_SIZE)
        text, kb = cards.counterparties_view(rows, page, total, query)
        if edit:
            await self._edit(edit, text, reply_markup=kb)
        else:
            await ctx.bot.send_message(update.effective_chat.id, text, reply_markup=kb)

    async def _show_status_menu(self, update: Update, ctx):
        flt = self.db.get_filter(update.effective_user.id)
        counts = self.db.status_counts(flt)
        text = (cards.filter_description(flt.without("status")) +
                "\n\n📌 Оберіть статус (остання подія руху накладної):")
        await ctx.bot.send_message(update.effective_chat.id, text, reply_markup=cards.status_keyboard(counts))

    async def _show_summary(self, update: Update, ctx):
        flt = self.db.get_filter(update.effective_user.id)
        await ctx.bot.send_message(update.effective_chat.id, cards.summary_view(self.db.summary(flt), flt))

    async def _send_excel(self, update: Update, ctx):
        flt = self.db.get_filter(update.effective_user.id)
        data, count = await asyncio.to_thread(build_workbook, self.db, flt)
        fname = f"Накладні_{datetime.now():%Y-%m-%d_%H%M}.xlsx"
        await ctx.bot.send_document(update.effective_chat.id, document=data, filename=fname,
                                    caption=f"📥 Експорт: {count} накладних\n{cards.filter_description(flt)}")

    async def _edit(self, message: Message, text: str, reply_markup=None):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            if "no text in the message" in str(exc).lower() or "can't be edited" in str(exc).lower():
                await message.reply_text(text, reply_markup=reply_markup)
                return
            raise

    # ================================================================ помилки

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE):
        log.error("Помилка під час обробки оновлення", exc_info=ctx.error)
        if isinstance(update, Update):
            try:
                if update.callback_query:
                    await update.callback_query.answer("😔 Помилка, спробуйте ще раз", show_alert=False)
                elif update.effective_message:
                    await update.effective_message.reply_text(ERROR_TEXT)
            except Exception:
                pass


def parse_field_value(kind: str, text: str):
    """→ (значення, текст_помилки)."""
    text = (text or "").strip()
    if kind == "money":
        v = parsing.parse_amount(text)
        return (v, None) if v is not None else (None, "🔢 Не схоже на суму. Приклад: 1 200,50")
    if kind == "date":
        d = parsing.parse_date(text)
        return (d.isoformat(), None) if d else (None, "📅 Не схоже на дату. Приклад: 05.10.2026")
    if kind == "code":
        if text in ("-", "—"):
            return "", None
        digits = parsing.digits_only(text)
        if 8 <= len(digits) <= 12 and len(digits) == len(text.replace(" ", "")):
            return digits, None
        return None, "🆔 Код має складатися з 8–12 цифр (ЄДРПОУ — 8, ІПН — 10)."
    if not text:
        return None, "✍️ Значення не може бути порожнім."
    return text[:300], None
