"""
Сметчик для флип-проекта
aiogram 3.7 + Claude API + Google Sheets
"""

import os
import re
import shelve
import base64
import asyncio
import logging
import json
import httpx
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# Google Sheets credentials JSON — либо путь к файлу, либо сам JSON строкой
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_PATH = "flip_bot"

CATEGORIES = [
    "Черновые материалы",
    "Чистовые материалы",
    "Сантехника",
    "Электрика",
    "Мебель",
    "Техника",
    "Демонтажные работы",
    "Ремонтные услуги",
    "Вывоз мусора",
    "Доставка",
    "Сделка покупки",
    "Сделка продажи",
    "Непредвиденные",
]

SHEET_HEADERS = [
    "Дата", "Категория", "Наименование", "Магазин/поставщик",
    "Количество", "Единица измерения", "Цена за единицу",
    "Сумма", "Доставка", "Способ оплаты"
]


# ─── Время МСК ──────────────────────────────────────────────────────────────

def now_msk():
    msk = timezone(timedelta(hours=3))
    return datetime.now(timezone.utc).astimezone(msk).replace(tzinfo=None)

def today_msk():
    return now_msk().date()


# ─── shelve ─────────────────────────────────────────────────────────────────

def db_get(key, default=None):
    with shelve.open(DB_PATH) as db:
        return db.get(key, default)

def db_set(key, value):
    with shelve.open(DB_PATH) as db:
        db[key] = value

def db_del(key):
    with shelve.open(DB_PATH) as db:
        if key in db:
            del db[key]


# ─── Google Sheets ───────────────────────────────────────────────────────────

def get_gspread_client():
    """Создаёт авторизованный gspread клиент."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    cred_value = GOOGLE_CREDENTIALS
    # Если это путь к файлу
    if os.path.isfile(cred_value):
        creds = Credentials.from_service_account_file(cred_value, scopes=scopes)
    else:
        # Иначе пробуем как JSON-строку
        info = json.loads(cred_value)
        creds = Credentials.from_service_account_info(info, scopes=scopes)

    return gspread.authorize(creds)


def ensure_headers(ws):
    """Если первая строка пустая — записываем заголовки."""
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != "Дата":
        ws.insert_row(SHEET_HEADERS, 1)


def append_row_to_sheet(row_data: list):
    """Добавляет строку в Google Sheets."""
    gc = get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)

    # Берём первый лист или создаём "Смета"
    try:
        ws = sh.worksheet("Смета")
    except Exception:
        ws = sh.add_worksheet(title="Смета", rows=1000, cols=20)

    ensure_headers(ws)
    ws.append_row(row_data, value_input_option="USER_ENTERED")


# ─── Claude API ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Ты помощник-сметчик для флип-проекта (ремонт и перепродажа недвижимости).
Пользователь присылает текст или фото чека/накладной.
Тебе нужно извлечь данные и вернуть ТОЛЬКО валидный JSON без пояснений.

Категории (используй только из этого списка):
{json.dumps(CATEGORIES, ensure_ascii=False)}

Формат ответа (строго JSON):
{{
  "category": "Чистовые материалы",
  "name": "Обои флизелиновые",
  "shop": "Леруа Мерлен",
  "quantity": 5,
  "unit": "рул.",
  "price_per_unit": 900,
  "total": 4500,
  "delivery": 0,
  "notes": ""
}}

Правила:
- category: выбери наиболее подходящую из списка
- name: краткое чёткое наименование
- shop: магазин или поставщик (если не указан — пустая строка)
- quantity: число (если не указано — 1)
- unit: единица измерения (шт., рул., м., м², кг., л., компл., услуга и т.д.)
- price_per_unit: цена за единицу (если не указана, но есть total и quantity — вычисли)
- total: итоговая сумма в рублях (число, без знака ₽)
- delivery: стоимость доставки (0 если нет)
- notes: любые важные замечания (обычно пустая строка)

Если чего-то нет в тексте — ставь разумные defaults или 0/пустую строку.
Отвечай ТОЛЬКО JSON, без markdown, без пояснений.
"""


async def parse_with_claude(text: str = None, image_bytes: bytes = None, mime: str = "image/jpeg") -> dict:
    """Отправляет текст или фото в Claude, получает JSON с данными."""
    content = []

    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64}
        })
        content.append({"type": "text", "text": "Извлеки данные из этого чека/накладной."})
    elif text:
        content.append({"type": "text", "text": text})
    else:
        raise ValueError("Нужен текст или изображение")

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}]
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload
        )
        resp.raise_for_status()
        data = resp.json()

    raw = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            raw += block["text"]

    # Чистим от возможных markdown-обёрток
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```$", "", raw)
    raw = raw.strip()

    return json.loads(raw)


# ─── Форматирование ───────────────────────────────────────────────────────────

def fmt_parsed(p: dict) -> str:
    """Красиво форматирует распознанные данные для подтверждения."""
    lines = [
        f"📋 *{p['category']}*",
        f"📦 {p['name']}",
    ]
    if p.get("shop"):
        lines.append(f"🏪 {p['shop']}")
    lines.append(f"🔢 {p['quantity']} {p['unit']} × {p['price_per_unit']:,.0f}₽ = *{p['total']:,.0f}₽*")
    if p.get("delivery") and p["delivery"] > 0:
        lines.append(f"🚚 Доставка: {p['delivery']:,.0f}₽")
    else:
        lines.append("🚚 Доставки нет")
    if p.get("notes"):
        lines.append(f"📝 {p['notes']}")
    return "\n".join(lines)


def build_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Верно", callback_data="confirm_yes"),
            InlineKeyboardButton(text="✏️ Исправить", callback_data="confirm_edit"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_no"),
        ]
    ])


def build_pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash"),
            InlineKeyboardButton(text="💳 Карта", callback_data="pay_card"),
        ]
    ])


# ─── Хэндлеры ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 Привет! Я сметчик для флип-проекта.\n\n"
        "Просто напиши что купил или пришли фото чека — я всё запишу в таблицу.\n\n"
        "Пример:\n"
        "_обои 5 рулонов 4500 руб Леруа_\n\n"
        "Или отправь фото накладной.",
        parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "📖 *Как пользоваться:*\n\n"
        "1. Напиши что купил (можно вольно, как в мессенджере)\n"
        "2. Или пришли фото чека/накладной\n"
        "3. Я распознаю данные и покажу на подтверждение\n"
        "4. Выбери способ оплаты\n"
        "5. Готово — строка в таблице ✅\n\n"
        "*Категории которые я знаю:*\n"
        + "\n".join(f"• {c}" for c in CATEGORIES),
        parse_mode="Markdown"
    )


async def process_input(msg: Message, text: str = None, image_bytes: bytes = None, mime: str = "image/jpeg"):
    """Общая логика обработки текста или фото."""
    uid = msg.from_user.id
    wait = await msg.answer("🔍 Анализирую...")

    try:
        parsed = await parse_with_claude(text=text, image_bytes=image_bytes, mime=mime)
    except Exception as e:
        log.error(f"Claude error: {e}")
        await wait.delete()
        await msg.answer("❌ Не смог распознать. Попробуй написать подробнее или прислать более чёткое фото.")
        return

    await wait.delete()

    # Сохраняем в shelve
    db_set(f"pending_{uid}", {
        "parsed": parsed,
        "date": str(today_msk()),
    })

    text_out = fmt_parsed(parsed) + "\n\n*Всё верно?*"
    await msg.answer(text_out, parse_mode="Markdown", reply_markup=build_confirm_kb())


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    await process_input(msg, text=msg.text)


@dp.message(F.photo)
async def handle_photo(msg: Message):
    photo = msg.photo[-1]  # наибольшее разрешение
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    image_bytes = buf.read()
    await process_input(msg, image_bytes=image_bytes, mime="image/jpeg")


@dp.message(F.document)
async def handle_document(msg: Message):
    doc = msg.document
    mime = doc.mime_type or "application/octet-stream"
    if not mime.startswith("image/"):
        await msg.answer("Я понимаю только фото и текст. Пришли фото чека или напиши текстом.")
        return
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    image_bytes = buf.read()
    await process_input(msg, image_bytes=image_bytes, mime=mime)


@dp.callback_query(F.data == "confirm_no")
async def confirm_no(cb: CallbackQuery):
    uid = cb.from_user.id
    db_del(f"pending_{uid}")
    await cb.message.edit_text("❌ Отменено. Можешь прислать новый чек.")


@dp.callback_query(F.data == "confirm_edit")
async def confirm_edit(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "✏️ Напиши исправленный вариант — я распознаю заново.",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.callback_query(F.data == "confirm_yes")
async def confirm_yes(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        cb.message.text + "\n\n💳 *Способ оплаты?*",
        parse_mode="Markdown",
        reply_markup=build_pay_kb()
    )


async def save_to_sheet(cb: CallbackQuery, pay_method: str):
    uid = cb.from_user.id
    pending = db_get(f"pending_{uid}")

    if not pending:
        await cb.answer("Нет данных для записи", show_alert=True)
        return

    p = pending["parsed"]
    date_str = pending["date"]

    row = [
        date_str,
        p.get("category", ""),
        p.get("name", ""),
        p.get("shop", ""),
        p.get("quantity", ""),
        p.get("unit", ""),
        p.get("price_per_unit", ""),
        p.get("total", ""),
        p.get("delivery", 0) or "",
        pay_method,
    ]

    try:
        await asyncio.to_thread(append_row_to_sheet, row)
    except Exception as e:
        log.error(f"Google Sheets error: {e}")
        await cb.message.edit_text(
            "❌ Ошибка записи в таблицу. Проверь настройки Google Sheets.\n\n"
            f"Детали: `{str(e)[:200]}`",
            parse_mode="Markdown"
        )
        return

    db_del(f"pending_{uid}")

    await cb.message.edit_text(
        cb.message.text + f"\n\n✅ Записано! ({pay_method})",
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "pay_cash")
async def pay_cash(cb: CallbackQuery):
    await save_to_sheet(cb, "Наличные")


@dp.callback_query(F.data == "pay_card")
async def pay_card(cb: CallbackQuery):
    await save_to_sheet(cb, "Карта")


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
