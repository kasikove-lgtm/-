"""
Сметчик для флип-проекта
aiogram 3.7 + Claude API + Google Sheets
Проекты + несколько товаров из одного чека
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
    ReplyKeyboardRemove
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
 
BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")
 
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
 
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = DATA_DIR + "/flip_bot"
 
CATEGORIES = [
    "Черновые материалы", "Чистовые материалы", "Сантехника", "Электрика",
    "Мебель", "Техника", "Демонтажные работы", "Ремонтные услуги",
    "Вывоз мусора", "Доставка", "Сделка покупки", "Сделка продажи", "Непредвиденные",
]
 
SHEET_HEADERS = [
    "Дата", "Проект", "Категория", "Наименование", "Магазин/поставщик",
    "Количество", "Единица измерения", "Цена за единицу", "Сумма", "Доставка", "Способ оплаты"
]
 
 
# --- Время МСК ---
 
def now_msk():
    msk = timezone(timedelta(hours=3))
    return datetime.now(timezone.utc).astimezone(msk).replace(tzinfo=None)
 
def today_msk():
    return now_msk().date()
 
 
# --- shelve ---
 
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
 
 
# --- Проекты ---
 
def get_projects() -> list:
    return db_get("projects", [])
 
def add_project(name: str):
    projects = get_projects()
    if name not in projects:
        projects.append(name)
        db_set("projects", projects)
 
def get_current_project(uid: int) -> str:
    return db_get(f"project_{uid}", "")
 
def set_current_project(uid: int, name: str):
    db_set(f"project_{uid}", name)
 
 
# --- Google Sheets ---
 
def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
 
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
 
    cred_value = GOOGLE_CREDENTIALS
    if os.path.isfile(cred_value):
        creds = Credentials.from_service_account_file(cred_value, scopes=scopes)
    else:
        info = json.loads(cred_value)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
 
    return gspread.authorize(creds)
 
 
def ensure_headers(ws):
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != "Дата":
        ws.insert_row(SHEET_HEADERS, 1)
 
 
def append_rows_to_sheet(rows: list):
    gc = get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
 
    try:
        ws = sh.worksheet("Смета")
    except Exception:
        ws = sh.add_worksheet(title="Смета", rows=1000, cols=20)
 
    ensure_headers(ws)
    for row in rows:
        ws.append_row(row, value_input_option="USER_ENTERED")
 
 
# --- Claude API ---
 
SYSTEM_PROMPT = (
    "Ты помощник-сметчик для флип-проекта (ремонт и перепродажа недвижимости).\n"
    "Пользователь присылает текст или фото чека/накладной.\n"
    "Тебе нужно извлечь ВСЕ товары и вернуть ТОЛЬКО валидный JSON-массив без пояснений.\n\n"
    "ВАЖНО: если в чеке несколько товаров — каждый товар отдельным объектом в массиве!\n\n"
    "Категории (используй только из этого списка):\n"
    '["Черновые материалы","Чистовые материалы","Сантехника","Электрика","Мебель",'
    '"Техника","Демонтажные работы","Ремонтные услуги","Вывоз мусора","Доставка",'
    '"Сделка покупки","Сделка продажи","Непредвиденные"]\n\n'
    "Формат ответа (строго JSON-массив, даже если товар один):\n"
    '[{"category":"Чистовые материалы","name":"Обои флизелиновые","shop":"Леруа Мерлен",'
    '"quantity":5,"unit":"рул.","price_per_unit":900,"total":4500,"delivery":0,"notes":""}]\n\n'
    "Правила:\n"
    "- Каждый товар отдельный объект в массиве\n"
    "- category: из списка выше\n"
    "- name: краткое наименование одного товара\n"
    "- shop: магазин/поставщик (одинаковый для всех из одного чека)\n"
    "- quantity: число (если не указано — 1)\n"
    "- unit: шт., рул., м., м2, кг., л., компл., услуга и т.д.\n"
    "- price_per_unit: цена за единицу\n"
    "- total: сумма за этот товар в рублях\n"
    "- delivery: доставка (только у одного товара, у остальных 0)\n"
    "- notes: замечания (обычно пустая строка)\n"
    "Отвечай ТОЛЬКО JSON-массив, без markdown, без пояснений."
)
 
 
async def parse_with_claude(text: str = None, image_bytes: bytes = None, mime: str = "image/jpeg") -> list:
    content = []
 
    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
        content.append({"type": "text", "text": "Извлеки все товары из этого чека/накладной."})
    elif text:
        content.append({"type": "text", "text": text})
    else:
        raise ValueError("Нужен текст или изображение")
 
    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 2000,
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
        log.info(f"Claude API status: {resp.status_code}")
        if resp.status_code != 200:
            log.error(f"Claude API error: {resp.text}")
        resp.raise_for_status()
        data = resp.json()
 
    raw = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            raw += block["text"]
 
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```$", "", raw)
    raw = raw.strip()
 
    result = json.loads(raw)
    if isinstance(result, dict):
        result = [result]
    return result
 
 
# --- Форматирование ---
 
def fmt_item(p: dict, idx: int = None, total_items: int = 1) -> str:
    prefix = f"*{idx}/{total_items}* " if total_items > 1 else ""
    lines = [f"{prefix}📋 *{p['category']}*", f"📦 {p['name']}"]
    if p.get("shop"):
        lines.append(f"🏪 {p['shop']}")
    lines.append(f"🔢 {p['quantity']} {p['unit']} × {p['price_per_unit']:,.0f}₽ = *{p['total']:,.0f}₽*")
    if p.get("delivery") and p["delivery"] > 0:
        lines.append(f"🚚 Доставка: {p['delivery']:,.0f}₽")
    if p.get("notes"):
        lines.append(f"📝 {p['notes']}")
    return "\n".join(lines)
 
 
def fmt_all_items(items: list) -> str:
    n = len(items)
    parts = [fmt_item(item, i+1, n) for i, item in enumerate(items)]
    total_sum = sum(p.get("total", 0) or 0 for p in items)
    total_delivery = sum(p.get("delivery", 0) or 0 for p in items)
    summary = f"\n💰 *Итого: {total_sum:,.0f}₽*"
    if total_delivery > 0:
        summary += f" + доставка {total_delivery:,.0f}₽"
    return "\n\n".join(parts) + summary
 
 
# --- Клавиатуры ---
 
def build_confirm_kb(uid: int) -> InlineKeyboardMarkup:
    project = get_current_project(uid)
    project_btn = f"📁 {project}" if project else "📁 Проект не выбран"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Верно", callback_data="confirm_yes"),
            InlineKeyboardButton(text="✏️ Исправить", callback_data="confirm_edit"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_no"),
        ],
        [
            InlineKeyboardButton(text=project_btn, callback_data="show_projects"),
            InlineKeyboardButton(text="➕ Новый проект", callback_data="new_project"),
        ]
    ])
 
 
def build_pay_kb(uid: int) -> InlineKeyboardMarkup:
    project = get_current_project(uid)
    project_btn = f"📁 {project}" if project else "📁 Проект не выбран"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash"),
            InlineKeyboardButton(text="💳 Карта", callback_data="pay_card"),
        ],
        [
            InlineKeyboardButton(text=project_btn, callback_data="show_projects"),
            InlineKeyboardButton(text="➕ Новый проект", callback_data="new_project"),
        ]
    ])
 
 
def build_projects_kb() -> InlineKeyboardMarkup:
    projects = get_projects()
    rows = []
    # По 2 проекта в ряд
    for i in range(0, len(projects), 2):
        row = [InlineKeyboardButton(text=p, callback_data=f"select_project_{p}") for p in projects[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
 
 
def build_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
    ]])
 
 
# --- Хэндлеры ---
 
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    project = get_current_project(uid)
    project_info = f"📁 Текущий проект: *{project}*" if project else "📁 Проект не выбран"
    await msg.answer(
        f"👋 Привет! Я сметчик для флип-проекта.\n\n"
        f"{project_info}\n\n"
        "Просто напиши что купил или пришли фото чека — запишу в таблицу.\n\n"
        "Пример: _обои 5 рулонов 4500 руб Леруа_",
        parse_mode="Markdown"
    )
 
 
@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "📖 *Как пользоваться:*\n\n"
        "1. Выбери проект (кнопка 📁 при отправке чека)\n"
        "2. Напиши что купил или пришли фото чека\n"
        "3. Подтверди → выбери способ оплаты\n"
        "4. Каждый товар — отдельная строка в таблице ✅\n\n"
        "*Категории:*\n" + "\n".join(f"• {c}" for c in CATEGORIES),
        parse_mode="Markdown"
    )
 
 
async def process_input(msg: Message, text: str = None, image_bytes: bytes = None, mime: str = "image/jpeg"):
    uid = msg.from_user.id
    wait = await msg.answer("🔍 Анализирую...")
 
    try:
        items = await parse_with_claude(text=text, image_bytes=image_bytes, mime=mime)
    except Exception as e:
        log.error(f"Claude error: {e}")
        await wait.delete()
        await msg.answer("❌ Не смог распознать. Попробуй написать подробнее или прислать более чёткое фото.")
        return
 
    await wait.delete()
 
    project = get_current_project(uid)
    project_line = f"\n\n📁 *Проект: {project}*" if project else "\n\n📁 *Проект не выбран*"
 
    n = len(items)
    header = f"Нашёл *{n} товар{'а' if 2 <= n <= 4 else 'ов' if n >= 5 else ''}*:\n\n" if n > 1 else ""
    text_out = header + fmt_all_items(items) + project_line + "\n\n*Всё верно?*"
    sent = await msg.answer(text_out, parse_mode="Markdown", reply_markup=build_confirm_kb(uid))
 
    db_set(f"pending_{sent.message_id}", {
        "items": items,
        "date": str(today_msk()),
        "uid": uid,
    })
 
 
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    # Проверяем не ожидаем ли ввод нового проекта
    uid = msg.from_user.id
    if db_get(f"awaiting_project_{uid}"):
        db_del(f"awaiting_project_{uid}")
        name = msg.text.strip()
        if name:
            add_project(name)
            set_current_project(uid, name)
            await msg.answer(
                f"✅ Проект *{name}* создан и выбран.",
                parse_mode="Markdown"
            )
        return
    await process_input(msg, text=msg.text)
 
 
@dp.message(F.photo)
async def handle_photo(msg: Message):
    photo = msg.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    await process_input(msg, image_bytes=buf.read(), mime="image/jpeg")
 
 
@dp.message(F.document)
async def handle_document(msg: Message):
    doc = msg.document
    mime = doc.mime_type or "application/octet-stream"
    if not mime.startswith("image/"):
        await msg.answer("Я понимаю только фото и текст.")
        return
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    await process_input(msg, image_bytes=buf.read(), mime=mime)
 
 
@dp.callback_query(F.data == "confirm_no")
async def confirm_no(cb: CallbackQuery):
    db_del(f"pending_{cb.message.message_id}")
    await cb.message.edit_text("❌ Отменено.")
 
 
@dp.callback_query(F.data == "confirm_edit")
async def confirm_edit(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("✏️ Напиши исправленный вариант — распознаю заново.")
 
 
@dp.callback_query(F.data == "confirm_yes")
async def confirm_yes(cb: CallbackQuery):
    uid = cb.from_user.id
    # Обновляем проект в pending на актуальный
    msg_id = cb.message.message_id
    pending = db_get(f"pending_{msg_id}")
    if pending:
        pending["uid"] = uid
        db_set(f"pending_{msg_id}", pending)
 
    await cb.answer()
    await cb.message.edit_text(
        cb.message.text + "\n\n💳 *Способ оплаты?*",
        parse_mode="Markdown",
        reply_markup=build_pay_kb(uid)
    )
 
 
@dp.callback_query(F.data == "show_projects")
async def show_projects(cb: CallbackQuery):
    await cb.answer()
    projects = get_projects()
    if not projects:
        await cb.message.answer(
            "Проектов пока нет. Нажми ➕ Новый проект чтобы создать.",
            reply_markup=build_cancel_kb()
        )
        return
    await cb.message.answer(
        "📁 *Выбери проект:*",
        parse_mode="Markdown",
        reply_markup=build_projects_kb()
    )
 
 
@dp.callback_query(F.data == "new_project")
async def new_project(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    db_set(f"awaiting_project_{uid}", True)
    await cb.message.answer(
        "Введи название нового проекта:",
        reply_markup=build_cancel_kb()
    )
 
 
@dp.callback_query(F.data.startswith("select_project_"))
async def select_project(cb: CallbackQuery):
    uid = cb.from_user.id
    name = cb.data[len("select_project_"):]
    set_current_project(uid, name)
    await cb.answer(f"Выбран проект: {name}")
    await cb.message.edit_text(f"✅ Проект *{name}* выбран.", parse_mode="Markdown")
 
 
@dp.callback_query(F.data == "main_menu")
async def main_menu(cb: CallbackQuery):
    uid = cb.from_user.id
    db_del(f"awaiting_project_{uid}")
    await cb.answer()
    await cb.message.edit_text("🏠 Главное меню. Отправь чек или напиши что купил.")
 
 
async def save_to_sheet(cb: CallbackQuery, pay_method: str):
    uid = cb.from_user.id
    msg_id = cb.message.message_id
    pending = db_get(f"pending_{msg_id}")
    log.info(f"save_to_sheet: uid={uid} msg_id={msg_id} pending={'found' if pending else 'MISSING'}")
 
    if not pending:
        await cb.answer("Нет данных для записи", show_alert=True)
        return
 
    items = pending["items"]
    date_str = pending["date"]
    project = get_current_project(uid)
 
    rows = []
    for p in items:
        rows.append([
            date_str,
            project,
            p.get("category", ""),
            p.get("name", ""),
            p.get("shop", ""),
            p.get("quantity", ""),
            p.get("unit", ""),
            p.get("price_per_unit", ""),
            p.get("total", ""),
            p.get("delivery", 0) or "",
            pay_method,
        ])
 
    try:
        await asyncio.to_thread(append_rows_to_sheet, rows)
    except Exception as e:
        log.error(f"Google Sheets error: {e}")
        await cb.message.edit_text(
            "❌ Ошибка записи в таблицу.\n\n"
            f"Детали: `{str(e)[:200]}`",
            parse_mode="Markdown"
        )
        return
 
    db_del(f"pending_{msg_id}")
    n = len(rows)
    project_line = f" | 📁 {project}" if project else ""
    await cb.message.edit_text(
        cb.message.text + f"\n\n✅ Записано {n} стр.! ({pay_method}{project_line})",
        parse_mode="Markdown"
    )
 
 
@dp.callback_query(F.data == "pay_cash")
async def pay_cash(cb: CallbackQuery):
    await save_to_sheet(cb, "Наличные")
 
 
@dp.callback_query(F.data == "pay_card")
async def pay_card(cb: CallbackQuery):
    await save_to_sheet(cb, "Карта")
 
 
# --- Запуск ---
 
async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot, skip_updates=True)
 
 
if __name__ == "__main__":
    asyncio.run(main())
