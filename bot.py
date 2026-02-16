import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import aiosqlite
import numpy as np
import pandas as pd
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from matplotlib import pyplot as plt
from openpyxl.styles import Font
from pyrogram import Client, filters, idle, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from setup_db import DatabaseConnection, init_db
from help_text import HELP_TEXT

# ─── Конфигурация ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()

api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('BOT_TOKEN')

if not all([api_id, api_hash, bot_token]):
    raise RuntimeError(
        "Не заданы API_ID, API_HASH или BOT_TOKEN. Проверьте .env файл.")

app = Client("expense_bot", api_id=api_id,
             api_hash=api_hash, bot_token=bot_token)

DEFAULT_TZ = pytz.timezone('Asia/Almaty')
MAX_MESSAGE_LENGTH = 4000

CURRENCY_SYMBOLS = {
    'KZT': '₸', 'RUB': '₽', 'USD': '$', 'EUR': '€',
    'UAH': '₴', 'GBP': '£', 'UZS': 'сўм', 'KGS': 'сом',
}

POPULAR_TIMEZONES = {
    'Алматы (UTC+6)': 'Asia/Almaty',
    'Москва (UTC+3)': 'Europe/Moscow',
    'Киев (UTC+2)': 'Europe/Kyiv',
    'Ташкент (UTC+5)': 'Asia/Tashkent',
    'Бишкек (UTC+6)': 'Asia/Bishkek',
    'Лондон (UTC+0)': 'Europe/London',
    'Берлин (UTC+1)': 'Europe/Berlin',
    'Нью-Йорк (UTC-5)': 'America/New_York',
    'Дубай (UTC+4)': 'Asia/Dubai',
    'Стамбул (UTC+3)': 'Europe/Istanbul',
}

# ─── Клавиатуры ──────────────────────────────────────────────────────────────

main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Категории"), KeyboardButton("Шаблоны")],
        [KeyboardButton("Отчет"), KeyboardButton("Сервис")],
    ],
    resize_keyboard=True,
)

category_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Мои категории"),
         KeyboardButton("Добавить категорию")],
        [KeyboardButton("Удалить категорию"),
         KeyboardButton("Объединить категории")],
        [KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

template_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Мои шаблоны"), KeyboardButton("Добавить шаблон")],
        [KeyboardButton("Удалить шаблон"), KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

period_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("День"), KeyboardButton("Неделя")],
        [KeyboardButton("Месяц"), KeyboardButton(
            "Квартал"), KeyboardButton("Год")],
        [KeyboardButton("Ввести даты вручную")],
        [KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

report_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("По категориям"), KeyboardButton("За период")],
        [KeyboardButton("Все траты")],
        [KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

service_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Валюта"), KeyboardButton("Часовой пояс")],
        [KeyboardButton("Редактировать траты"),
         KeyboardButton("Удалить траты")],
        [KeyboardButton("Справка"), KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

timezone_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Алматы (UTC+6)"), KeyboardButton("Москва (UTC+3)")],
        [KeyboardButton("Киев (UTC+2)"), KeyboardButton("Ташкент (UTC+5)")],
        [KeyboardButton("Бишкек (UTC+6)"), KeyboardButton("Дубай (UTC+4)")],
        [KeyboardButton("Стамбул (UTC+3)"), KeyboardButton("Берлин (UTC+1)")],
        [KeyboardButton("Лондон (UTC+0)"), KeyboardButton("Нью-Йорк (UTC-5)")],
        [KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)

edit_period_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("День"), KeyboardButton(
            "Неделя"), KeyboardButton("Месяц")],
        [KeyboardButton("Назад")],
    ],
    resize_keyboard=True, one_time_keyboard=True,
)


async def category_keyboard(user_id):
    categories = await get_category_names(user_id)
    buttons = [[KeyboardButton(c)] for c in categories]
    buttons.append([KeyboardButton("Назад")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)


def undo_keyboard(expense_id):
    if expense_id is None:
        return main_keyboard
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "↩ Отменить", callback_data=f"undo:{expense_id}")]
    ])


def confirm_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data="cat_yes"),
         InlineKeyboardButton("Другая", callback_data="cat_other")]
    ])


def create_category_keyboard():
    """Клавиатура для предложения создать новую категорию."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, создать", callback_data="create_cat_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="create_cat_no")]
    ])


def fuzzy_category_keyboard(proposed_name: str = None):
    """Клавиатура для нечёткого совпадения категории."""
    create_button_text = f"➕ Создать «{proposed_name}»" if proposed_name else "➕ Нет, создать новую"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, использовать",
                              callback_data="fuzzy_cat_yes")],
        [InlineKeyboardButton(create_button_text,
                              callback_data="fuzzy_cat_new")],
        [InlineKeyboardButton("◀️ Назад", callback_data="fuzzy_cat_back")],
    ])


def template_confirm_keyboard(template_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записать", callback_data=f"tpl_yes:{template_id}"),
         InlineKeyboardButton("❌ Пропустить", callback_data=f"tpl_no:{template_id}")]
    ])


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def html_escape(text: str) -> str:
    """Экранирует HTML символы для безопасного вывода."""
    if not text:
        return text
    return (text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#x27;'))


async def send_long_message(message, text, **kwargs):
    if len(text) <= MAX_MESSAGE_LENGTH:
        return await message.reply(text, **kwargs)
    # Разбиваем по строкам, чтобы не разрезать HTML-теги
    lines = text.split('\n')
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            if chunk:
                await message.reply(chunk, **kwargs)
            chunk = line
        else:
            chunk = chunk + '\n' + line if chunk else line
    if chunk:
        await message.reply(chunk, **kwargs)


def safe_remove(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logging.warning(f"Не удалось удалить {path}: {e}")


# ─── Настройки пользователя ──────────────────────────────────────────────────

async def get_user_currency(user_id) -> tuple[str, str]:
    """Возвращает (код, символ) валюты пользователя."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('SELECT currency FROM user_settings WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            code = row[0] if row else 'KZT'
    except aiosqlite.Error:
        code = 'KZT'
    return code, CURRENCY_SYMBOLS.get(code, code)


async def set_user_currency(user_id, currency_code: str):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO user_settings (user_id, currency) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET currency = excluded.currency
            ''', (user_id, currency_code.upper()))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка set_user_currency: {e}")


async def get_last_category_id(user_id) -> int | None:
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT last_category_id FROM user_settings WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
    except aiosqlite.Error:
        return None


async def set_last_category_id(user_id, category_id: int):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO user_settings (user_id, last_category_id) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_category_id = excluded.last_category_id
            ''', (user_id, category_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка set_last_category_id: {e}")


async def get_user_timezone(user_id) -> pytz.BaseTzInfo:
    """Возвращает объект часового пояса пользователя."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            if row and row[0]:
                return pytz.timezone(row[0])
    except (aiosqlite.Error, pytz.UnknownTimeZoneError):
        pass
    return DEFAULT_TZ


async def get_user_timezone_name(user_id) -> str:
    """Возвращает строку часового пояса пользователя."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT timezone FROM user_settings WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]
    except aiosqlite.Error:
        pass
    return 'Asia/Almaty'


async def set_user_timezone(user_id, tz_name: str):
    try:
        pytz.timezone(tz_name)  # проверяем валидность
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO user_settings (user_id, timezone) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone
            ''', (user_id, tz_name))
        return True
    except pytz.UnknownTimeZoneError:
        return False
    except aiosqlite.Error as e:
        logging.error(f"Ошибка set_user_timezone: {e}")
        return False


async def ensure_timezone_column():
    """Добавляет колонку timezone в user_settings, если её нет."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute("PRAGMA table_info(user_settings)")
            columns = [row[1] for row in await cursor.fetchall()]
            if 'timezone' not in columns:
                await cursor.execute(
                    "ALTER TABLE user_settings ADD COLUMN timezone TEXT DEFAULT 'Asia/Almaty'")
                logging.info("Колонка 'timezone' добавлена в user_settings.")
    except aiosqlite.Error as e:
        logging.error(f"Ошибка ensure_timezone_column: {e}")


async def get_category_name_by_id(category_id) -> str | None:
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('SELECT name FROM categories WHERE id = ?', (category_id,))
            row = await cursor.fetchone()
            return row[0] if row else None
    except aiosqlite.Error:
        return None


async def get_category_name_by_id_for_user(category_id, user_id) -> str | None:
    """Возвращает имя категории только если она принадлежит пользователю."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT name FROM categories WHERE id = ? AND user_id = ?',
                (category_id, user_id))
            row = await cursor.fetchone()
            return row[0] if row else None
    except aiosqlite.Error:
        return None


# ─── Состояние пользователя ──────────────────────────────────────────────────

async def set_user_state(user_id, state, data=None):
    try:
        async with DatabaseConnection() as cursor:
            data_json = json.dumps(data if data is not None else {})
            await cursor.execute('''
                INSERT OR REPLACE INTO user_states (user_id, state, data)
                VALUES (?, ?, ?)
            ''', (user_id, state, data_json))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка set_user_state: {e}")


async def get_user_state(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT state, data FROM user_states WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            if row:
                return row[0], json.loads(row[1]) if row[1] else {}
            return None, {}
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_user_state: {e}")
        return None, {}


async def reset_user_state(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка reset_user_state: {e}")


# ─── Категории ───────────────────────────────────────────────────────────────

async def get_categories(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT MIN(id), name FROM categories WHERE user_id = ? GROUP BY LOWER(name)',
                (user_id,))
            return [f"{r[0]}: {r[1]}" for r in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_categories: {e}")
        return []


async def get_category_names(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT name FROM categories WHERE user_id = ? GROUP BY LOWER(name)',
                (user_id,))
            return [r[0] for r in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_names: {e}")
        return []


async def get_category_id(category_name, user_id):
    """Возвращает ID категории (поиск без учёта регистра)."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id FROM categories WHERE LOWER(name) = LOWER(?) AND user_id = ?',
                (category_name, user_id))
            row = await cursor.fetchone()
            return row[0] if row else None
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_id: {e}")
        return None


async def get_category_id_and_name(category_name, user_id):
    """Возвращает (id, реальное_имя) категории (поиск без учёта регистра)."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id, name FROM categories WHERE LOWER(name) = LOWER(?) AND user_id = ?',
                (category_name, user_id))
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else (None, None)
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_id_and_name: {e}")
        return None, None


def _fuzzy_ratio(a: str, b: str) -> float:
    """Вычисляет степень сходства двух строк (0..1)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def find_fuzzy_category(input_text: str, user_id: int) -> str | None:
    """
    Ищет категорию, похожую на input_text, через нечёткое сравнение.
    Возвращает имя категории если сходство >= 0.55, иначе None.
    """
    categories = await get_category_names(user_id)
    best_match = None
    best_ratio = 0.0
    threshold = 0.55

    for cat in categories:
        ratio = _fuzzy_ratio(input_text, cat)
        if ratio > best_ratio and ratio >= threshold:
            best_ratio = ratio
            best_match = cat

    return best_match


async def find_fuzzy_from_words(words: list[str], user_id: int) -> tuple[str | None, list[str]]:
    """
    Ищет категорию по нечёткому совпадению, перебирая префиксы слов.
    Возвращает (matched_category, remaining_words) или (None, words).
    """
    if not words:
        return None, words

    categories = await get_category_names(user_id)
    best_match = None
    best_ratio = 0.0
    best_length = 0
    threshold = 0.55

    for length in range(len(words), 0, -1):
        candidate = ' '.join(words[:length])
        for cat in categories:
            ratio = _fuzzy_ratio(candidate, cat)
            if ratio > best_ratio and ratio >= threshold:
                best_ratio = ratio
                best_match = cat
                best_length = length

    remaining = words[best_length:] if best_match else words
    return best_match, remaining


async def add_category(category_name, user_id):
    try:
        async with DatabaseConnection() as cursor:
            # Проверяем, нет ли уже категории с таким именем (без учёта регистра)
            await cursor.execute(
                'SELECT id FROM categories WHERE LOWER(name) = LOWER(?) AND user_id = ?',
                (category_name, user_id))
            existing = await cursor.fetchone()
            if existing:
                return  # Категория уже есть
            await cursor.execute(
                'INSERT INTO categories (name, user_id) VALUES (?, ?)',
                (category_name, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка add_category: {e}")


async def delete_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'DELETE FROM categories WHERE id = ? AND user_id = ?', (category_id, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_category: {e}")


async def has_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT COUNT(*) FROM expenses WHERE category_id = ? AND user_id = ?',
                (category_id, user_id))
            result = await cursor.fetchone()
            return result[0] > 0
    except aiosqlite.Error:
        return False


async def delete_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'DELETE FROM expenses WHERE category_id = ? AND user_id = ?',
                (category_id, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_expenses_for_category: {e}")


async def merge_categories_db(source_ids: list[int], new_name: str, user_id: int) -> int | None:
    """
    Объединяет несколько категорий в одну с именем new_name.
    Переносит все расходы и шаблоны в новую/первую категорию.
    Возвращает id результирующей категории или None при ошибке.
    """
    try:
        async with DatabaseConnection() as cursor:
            # Проверяем существование целевой категории (по новому имени)
            await cursor.execute(
                'SELECT id FROM categories WHERE LOWER(name) = LOWER(?) AND user_id = ?',
                (new_name, user_id))
            existing = await cursor.fetchone()

            if existing:
                target_id = existing[0]
            else:
                # Создаём новую категорию
                await cursor.execute(
                    'INSERT INTO categories (name, user_id) VALUES (?, ?)',
                    (new_name, user_id))
                target_id = cursor.lastrowid

            # Переносим расходы из всех исходных категорий в целевую
            for src_id in source_ids:
                if src_id == target_id:
                    continue
                await cursor.execute(
                    'UPDATE expenses SET category_id = ? WHERE category_id = ? AND user_id = ?',
                    (target_id, src_id, user_id))
                await cursor.execute(
                    'UPDATE recurring_templates SET category_id = ? WHERE category_id = ? AND user_id = ?',
                    (target_id, src_id, user_id))
                await cursor.execute(
                    'DELETE FROM categories WHERE id = ? AND user_id = ?',
                    (src_id, user_id))

        return target_id
    except aiosqlite.Error as e:
        logging.error(f"Ошибка merge_categories_db: {e}")
        return None


# ─── Расходы ─────────────────────────────────────────────────────────────────

async def log_expense(user_id, category_id, name, price, quantity, total) -> int | None:
    """Записывает расход и возвращает его ID (для отмены)."""
    try:
        user_tz = await get_user_timezone(user_id)
        # Нормализуем имя: первая буква заглавная
        if name and isinstance(name, str):
            name = name[0].upper() + name[1:] if len(name) > 1 else name.upper()
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO expenses (user_id, category_id, name, price, quantity, total, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, category_id, name, price, quantity, total,
                  datetime.now(user_tz).strftime("%Y-%m-%d %H:%M:%S")))
            return cursor.lastrowid
    except aiosqlite.Error as e:
        logging.error(f"Ошибка log_expense: {e}")
        return None


async def delete_expense_by_id(expense_id, user_id) -> bool:
    """Удаляет конкретную трату (для отмены). Проверяет владельца."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id FROM expenses WHERE id = ? AND user_id = ?',
                (expense_id, user_id))
            if not await cursor.fetchone():
                return False
            await cursor.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
            return True
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_expense_by_id: {e}")
        return False


async def get_expenses(start_date, end_date, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name, SUM(e.total) as total
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.date BETWEEN ? AND ? AND e.user_id = ?
                GROUP BY LOWER(c.name)
            ''', (start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses: {e}")
        return []


async def get_expenses_by_category(start_date, end_date, category_name, user_id):
    try:
        category_id = await get_category_id(category_name, user_id)
        if category_id is None:
            return None
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT e.name, SUM(e.total) as total
                FROM expenses e
                WHERE e.category_id = ? AND e.date BETWEEN ? AND ? AND e.user_id = ?
                GROUP BY LOWER(e.name)
            ''', (category_id, start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses_by_category: {e}")
        return []


async def get_expenses_by_category_detailed(start_date, end_date, category_name, user_id):
    """Возвращает детальные траты по категории: [(name, total_per_name, date, amount_per_date), ...]"""
    try:
        category_id = await get_category_id(category_name, user_id)
        if category_id is None:
            return None
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT e.name, e.total, e.date
                FROM expenses e
                WHERE e.category_id = ? AND e.date BETWEEN ? AND ? AND e.user_id = ?
                ORDER BY e.name, e.date
            ''', (category_id, start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses_by_category_detailed: {e}")
        return []


async def get_expenses_by_period_detailed(start_date, end_date, user_id):
    """Возвращает детальные траты за период: [(date, category_name, expense_name, total), ...]"""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT e.date, c.name as category, e.name, e.total
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.date BETWEEN ? AND ? AND e.user_id = ?
                ORDER BY e.date, c.name, e.name
            ''', (start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses_by_period_detailed: {e}")
        return []


async def get_all_expenses(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT e.id, c.name, e.name, e.price, e.quantity, e.total, e.date
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? ORDER BY e.date DESC
            ''', (user_id,))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_all_expenses: {e}")
        return []


async def get_expenses_for_period(start_date, end_date, user_id):
    """Возвращает список трат за период с ID для редактирования."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT e.id, c.name, e.name, e.total, e.date
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? AND e.date BETWEEN ? AND ?
                ORDER BY e.date DESC
            ''', (user_id, start_date, end_date))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses_for_period: {e}")
        return []


async def update_expense(expense_id, user_id, category_id, name, price, quantity, total) -> bool:
    """Обновляет запись о трате."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id FROM expenses WHERE id = ? AND user_id = ?',
                (expense_id, user_id))
            if not await cursor.fetchone():
                return False
            await cursor.execute('''
                UPDATE expenses
                SET category_id = ?, name = ?, price = ?, quantity = ?, total = ?
                WHERE id = ? AND user_id = ?
            ''', (category_id, name, price, quantity, total, expense_id, user_id))
            return True
    except aiosqlite.Error as e:
        logging.error(f"Ошибка update_expense: {e}")
        return False


# ─── Шаблоны повторяющихся трат ──────────────────────────────────────────────

async def add_template(user_id, category_id, name, amount, day_of_month) -> int | None:
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO recurring_templates (user_id, category_id, name, amount, day_of_month)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, category_id, name, amount, day_of_month))
            return cursor.lastrowid
    except aiosqlite.Error as e:
        logging.error(f"Ошибка add_template: {e}")
        return None


async def get_templates(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT t.id, c.name, t.name, t.amount, t.day_of_month
                FROM recurring_templates t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id = ?
            ''', (user_id,))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_templates: {e}")
        return []


async def delete_template(template_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'DELETE FROM recurring_templates WHERE id = ? AND user_id = ?',
                (template_id, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_template: {e}")


async def get_templates_for_today():
    """Получает все шаблоны, у которых день совпадает с сегодняшним (по часовому поясу пользователя)."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT t.id, t.user_id, c.name, t.name, t.amount, t.category_id, t.day_of_month
                FROM recurring_templates t
                JOIN categories c ON t.category_id = c.id
            ''')
            all_templates = await cursor.fetchall()

        result = []
        for tpl_id, tpl_user_id, cat_name, tpl_name, amount, category_id, day_of_month in all_templates:
            user_tz = await get_user_timezone(tpl_user_id)
            now = datetime.now(user_tz)
            if day_of_month == now.day and now.hour == 9:
                result.append((tpl_id, tpl_user_id, cat_name,
                              tpl_name, amount, category_id))
        return result
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_templates_for_today: {e}")
        return []


# ─── Парсер свободного формата ────────────────────────────────────────────────

def extract_numbers_and_words(text: str) -> tuple[list[float], list[str]]:
    """Разделяет текст на числа и слова, убирая суффиксы валют."""
    # Убираем суффиксы валют после чисел: 100р, 500руб, 200₸, 300тг
    cleaned = re.sub(
        r'(\d+(?:[.,]\d+)?)\s*(?:р(?:уб(?:лей|ля)?)?|₸|тг|тенге|сум|сом)\.?',
        r'\1', text, flags=re.IGNORECASE
    )
    # Десятичная запятая → точка
    cleaned = re.sub(r'(\d+),(\d+)', r'\1.\2', cleaned)

    tokens = cleaned.split()
    numbers, words = [], []
    for token in tokens:
        try:
            numbers.append(float(token))
        except ValueError:
            words.append(token)
    return numbers, words


async def parse_free_form(text: str, user_id: int) -> dict | None:
    """
    Парсит свободный ввод: 'Продукты яблоки 100', 'такси 500', '100' и т.д.
    Возвращает dict с ключами: category, name, price, quantity, total
    или None если не удалось распарсить.
    """
    numbers, words = extract_numbers_and_words(text)
    if not numbers:
        return None

    # Пытаемся найти категорию с начала слов (жадный поиск — самое длинное совпадение)
    categories = await get_category_names(user_id)
    matched_category = None
    name_words = words

    for length in range(len(words), 0, -1):
        candidate = ' '.join(words[:length])
        for cat in categories:
            if candidate.lower() == cat.lower():
                matched_category = cat
                name_words = words[length:]
                break
        if matched_category:
            break

    name = ' '.join(name_words).strip() if name_words else None

    if len(numbers) >= 2:
        price = numbers[0]
        quantity = numbers[1]
        total = price * quantity
    else:
        price = None
        quantity = None
        total = numbers[0]

    return {
        'category': matched_category,
        'name': name,
        'price': price,
        'quantity': quantity,
        'total': total,
    }


# ─── Отчёты ──────────────────────────────────────────────────────────────────

async def create_pie_chart(data, user_id, currency_symbol='₸'):
    categories = [item[0] for item in data]
    totals = [item[1] for item in data]

    def func(pct, allvals):
        absolute = int(pct / 100. * sum(allvals))
        return f"{pct:.1f}%\n({absolute} {currency_symbol})"

    plt.figure(figsize=(10, 6))
    plt.pie(totals, labels=categories, autopct=lambda pct: func(
        pct, totals), startangle=140)
    plt.title('Расходы по категориям')
    chart_file = f'expenses_pie_chart_{user_id}.png'
    plt.savefig(chart_file)
    plt.close()
    return chart_file


def _group_key(date_str, group_by):
    """Возвращает ключ группировки для даты."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    if group_by == 'day':
        return dt.strftime("%d.%m")
    elif group_by == 'week':
        # Начало недели (понедельник)
        monday = dt - timedelta(days=dt.weekday())
        return monday.strftime("%d.%m")
    elif group_by == 'month':
        return dt.strftime("%m.%Y")
    return dt.strftime("%d.%m")


def _determine_grouping(start_date, end_date):
    """Определяет группировку и доступные уровни детализации."""
    start = datetime.strptime(start_date[:10], "%Y-%m-%d")
    end = datetime.strptime(end_date[:10], "%Y-%m-%d")
    days = (end - start).days + 1

    if days <= 1:
        return None, []  # только pie chart
    elif days <= 31:
        return 'day', []  # неделя / месяц
    elif days <= 365:
        return 'week', ['day']  # квартал
    else:
        return 'month', ['week', 'day']  # год


# Цветовая палитра для категорий (до 12 цветов, затем повтор)
BAR_COLORS = [
    '#4ECDC4', '#FF6B6B', '#45B7D1', '#FFA07A', '#98D8C8',
    '#F7DC6F', '#BB8FCE', '#85C1E9', '#F0B27A', '#82E0AA',
    '#F1948A', '#AED6F1',
]


async def create_stacked_bar_chart(rows, user_id, currency_symbol='₸', group_by='day'):
    """Создаёт stacked bar chart с линией тренда.

    rows: список (date, cat_name, exp_name, total) из get_expenses_by_period_detailed
    """
    if not rows:
        return None

    # Агрегируем: {group_key: {category: total}}
    groups = OrderedDict()
    all_categories = OrderedDict()

    for date, cat_name, exp_name, total in rows:
        key = _group_key(date, group_by)
        if key not in groups:
            groups[key] = {}
        groups[key][cat_name] = groups[key].get(cat_name, 0) + total
        all_categories[cat_name] = True

    labels = list(groups.keys())
    cat_names = list(all_categories.keys())

    if not labels:
        return None

    # Матрица данных: [categories x groups]
    data_matrix = []
    for cat in cat_names:
        data_matrix.append([groups[lbl].get(cat, 0) for lbl in labels])

    x = np.arange(len(labels))
    width = 0.6

    fig, ax1 = plt.subplots(figsize=(max(10, len(labels) * 0.8), 6))

    # Стекированные столбики
    bottom = np.zeros(len(labels))
    bars_list = []
    for i, (cat, cat_data) in enumerate(zip(cat_names, data_matrix)):
        color = BAR_COLORS[i % len(BAR_COLORS)]
        bars = ax1.bar(x, cat_data, width, bottom=bottom,
                       label=cat, color=color)
        bars_list.append(bars)
        bottom += np.array(cat_data)

    # Линия тренда (общая сумма)
    totals = bottom  # уже содержит суммы
    ax1.plot(x, totals, color='#2C3E50', linewidth=2,
             marker='o', markersize=4, zorder=5)

    # Настройки осей
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=45 if len(
        labels) > 10 else 0, ha='right' if len(labels) > 10 else 'center', fontsize=9)
    ax1.set_ylabel(currency_symbol)

    group_labels = {'day': 'по дням',
                    'week': 'по неделям', 'month': 'по месяцам'}
    ax1.set_title(f'Динамика расходов ({group_labels.get(group_by, "")})')

    # Легенда
    ax1.legend(loc='upper left', fontsize=8, ncol=min(len(cat_names), 4))

    plt.tight_layout()
    chart_file = f'expenses_bar_chart_{user_id}.png'
    plt.savefig(chart_file, dpi=150)
    plt.close()
    return chart_file


async def generate_excel_report(start_date, end_date, user_id) -> str | None:
    try:
        safe_start = start_date.replace(':', '-')
        safe_end = end_date.replace(':', '-')
        file_name = f'expenses_report_{user_id}_{safe_start}_to_{safe_end}.xlsx'

        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name, e.name, e.price, e.quantity, e.total, e.date
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? AND e.date BETWEEN ? AND ?
                ORDER BY c.name
            ''', (user_id, start_date, end_date))
            expenses = await cursor.fetchall()

        if not expenses:
            return None

        df = pd.DataFrame(expenses, columns=[
                          'category', 'expense', 'price', 'quantity', 'total', 'date'])
        bold_font = Font(bold=True)

        with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
            workbook = writer.book
            worksheet = workbook.create_sheet('Expenses Report')
            # Удаляем дефолтный пустой лист
            if 'Sheet' in workbook.sheetnames:
                del workbook['Sheet']
            start_row, start_col, col_offset = 1, 1, 7

            for category, group in df.groupby('category'):
                total_sum = group['total'].sum()
                worksheet.cell(row=start_row, column=start_col,
                               value=str(category))
                worksheet.cell(row=start_row, column=start_col +
                               4, value=f"{total_sum:.2f}")
                start_row += 2

                for i, header in enumerate(["Наименование", "Цена", "Количество", "Сумма", "Дата"]):
                    cell = worksheet.cell(
                        row=start_row, column=start_col + i, value=header)
                    cell.font = bold_font

                start_row += 1
                for _, row in group.iterrows():
                    worksheet.cell(row=start_row, column=start_col,
                                   value=row['expense'] or '---')
                    worksheet.cell(row=start_row, column=start_col +
                                   1, value=row['price'] or '---')
                    worksheet.cell(row=start_row, column=start_col +
                                   2, value=row['quantity'] or '---')
                    worksheet.cell(
                        row=start_row, column=start_col + 3, value=row['total'])
                    date_val = datetime.strptime(
                        row['date'], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
                    worksheet.cell(
                        row=start_row, column=start_col + 4, value=date_val)
                    start_row += 1

                start_row = 1
                start_col += col_offset

        return file_name
    except Exception as e:
        logging.error(f"Ошибка generate_excel_report: {e}")
        return None


async def get_period_dates(period: str, user_id: int = None) -> tuple[str, str]:
    if user_id:
        user_tz = await get_user_timezone(user_id)
        today = datetime.now(user_tz)
    else:
        today = datetime.now(DEFAULT_TZ)
    periods = {"День": 0, "Неделя": 7, "Месяц": 30, "Квартал": 90, "Год": 365}
    days = periods.get(period)
    if days is not None:
        start = today.strftime("%Y-%m-%d 00:00:00") if days == 0 \
            else (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        end = today.strftime("%Y-%m-%d %H:%M:%S")
        return start, end
    return "", ""


# ─── Ежемесячное сравнение ────────────────────────────────────────────────────

async def send_monthly_comparisons():
    """Отправляет сравнение прошлого месяца с позапрошлым всем активным пользователям."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('SELECT DISTINCT user_id FROM expenses')
            users = await cursor.fetchall()

        for (user_id,) in users:
            try:
                user_tz = await get_user_timezone(user_id)
                now = datetime.now(user_tz)

                # Отправляем только если у пользователя сейчас 1-е число и 9:00
                if now.day != 1 or now.hour != 9:
                    continue

                first_of_current = now.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0)

                # Прошлый месяц
                last_month_end = first_of_current - timedelta(seconds=1)
                last_month_start = last_month_end.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0)

                # Позапрошлый месяц
                prev_month_end = last_month_start - timedelta(seconds=1)
                prev_month_start = prev_month_end.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0)

                fmt = "%Y-%m-%d %H:%M:%S"
                _, symbol = await get_user_currency(user_id)

                last = await get_expenses(
                    last_month_start.strftime(fmt), last_month_end.strftime(fmt), user_id)
                prev = await get_expenses(
                    prev_month_start.strftime(fmt), prev_month_end.strftime(fmt), user_id)

                if not last and not prev:
                    continue

                last_dict = {cat: total for cat, total in last}
                prev_dict = {cat: total for cat, total in prev}
                all_cats = sorted(set(last_dict) | set(prev_dict))

                last_month_name = last_month_start.strftime("%B %Y")
                prev_month_name = prev_month_start.strftime("%B %Y")

                report = f"📊 Сравнение: {last_month_name} vs {prev_month_name}\n\n"
                total_last = sum(last_dict.values())
                total_prev = sum(prev_dict.values())

                for cat in all_cats:
                    cur = last_dict.get(cat, 0)
                    prv = prev_dict.get(cat, 0)

                    if prv > 0:
                        change = ((cur - prv) / prv) * 100
                        arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                        report += f"{arrow} {cat}: {cur:.0f} {symbol} ({change:+.0f}%)\n"
                    elif cur > 0:
                        report += f"🆕 {cat}: {cur:.0f} {symbol} (новая)\n"

                if total_prev > 0:
                    total_change = (
                        (total_last - total_prev) / total_prev) * 100
                    report += f"\nИтого: {total_last:.0f} {symbol} ({total_change:+.0f}% к прошлому)"
                else:
                    report += f"\nИтого: {total_last:.0f} {symbol}"

                await app.send_message(user_id, report)

            except Exception as e:
                logging.error(f"Ошибка отправки сравнения для {user_id}: {e}")

    except Exception as e:
        logging.error(f"Ошибка send_monthly_comparisons: {e}")


# ─── Напоминания по шаблонам ─────────────────────────────────────────────────

async def send_template_reminders():
    """Отправляет напоминания по шаблонам, у которых день совпадает с сегодняшним."""
    try:
        templates = await get_templates_for_today()
        for tpl_id, user_id, cat_name, tpl_name, amount, category_id in templates:
            try:
                _, symbol = await get_user_currency(user_id)
                label = f"{cat_name} — {tpl_name}" if tpl_name else cat_name
                text = f"🔔 Напоминание: {label}, {amount:.0f} {symbol}\nЗаписать трату?"

                await app.send_message(
                    user_id, text,
                    reply_markup=template_confirm_keyboard(tpl_id)
                )
            except Exception as e:
                logging.error(
                    f"Ошибка напоминания шаблона {tpl_id} для {user_id}: {e}")
    except Exception as e:
        logging.error(f"Ошибка send_template_reminders: {e}")


# ─── Шаблонное сообщение при /start ──────────────────────────────────────────

async def send_pinned_template_message(client, message):
    user_id = message.from_user.id
    template_text = (
        "Добавьте трату:\n\n"
        "📝 Свободный формат:\n"
        "Продукты яблоки 100\n"
        "Такси 500\n"
        "Еда 1200р\n\n"
        "📋 Точный формат:\n"
        "Продукты, Яблоки, 100, 2\n"
        "Такси, 500"
    )
    try:
        sent = await client.send_message(user_id, template_text)
        await client.pin_chat_message(user_id, sent.id)
    except MessageNotModified:
        pass
    except Exception as e:
        logging.error(f"Ошибка при закреплении: {e}")


# ─── Обработчики: категории ──────────────────────────────────────────────────

async def handle_delete_category(message, category_id):
    user_id = message.from_user.id
    if await has_expenses_for_category(category_id, user_id):
        confirm_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Да"), KeyboardButton("Нет")]],
            resize_keyboard=True, one_time_keyboard=True)
        await set_user_state(user_id, "confirm_delete", {"category_id": category_id})
        await message.reply("По этой категории есть траты. Удалить?", reply_markup=confirm_kb)
    else:
        await delete_category(category_id, user_id)
        await message.reply("Категория удалена.", reply_markup=main_keyboard)
        await reset_user_state(user_id)


# ─── Обработчики: отчёты ─────────────────────────────────────────────────────

async def handle_report(message):
    await message.reply("Выберите тип отчёта:", reply_markup=report_submenu)
    await reset_user_state(message.from_user.id)


async def handle_report_category(message):
    user_id = message.from_user.id
    categories = await get_category_names(user_id)
    if categories:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton(c)] for c in categories] +
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)
        await message.reply("Выберите категорию:", reply_markup=kb)
        await set_user_state(user_id, "choose_category")
    else:
        await message.reply("Категорий пока нет.", reply_markup=main_keyboard)


async def handle_choose_category(message, text):
    user_id = message.from_user.id
    if text == "Назад":
        await message.reply("Главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    if text in await get_category_names(user_id):
        await set_user_state(user_id, "choose_period", {"category": text})
        await message.reply(f"Категория: {text}. Выберите период:", reply_markup=period_keyboard)
    else:
        await message.reply("Неверная категория.", reply_markup=await category_keyboard(user_id))


async def handle_period_report(message, period, data):
    user_id = message.from_user.id
    if period == "Назад":
        await message.reply("Главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return
    if period == "Ввести даты вручную":
        await set_user_state(user_id, "manual_dates", data or {})
        await message.reply("Введите даты: YYYY-MM-DD YYYY-MM-DD", reply_markup=ForceReply())
        return
    if period not in ("День", "Неделя", "Месяц", "Квартал", "Год"):
        await message.reply("Неверный период.", reply_markup=period_keyboard)
        return

    start_date, end_date = await get_period_dates(period, user_id)
    category = data.get("category") if data else None
    await _send_report(message, start_date, end_date, user_id, category)
    await reset_user_state(user_id)


async def handle_manual_dates(message, text, data):
    user_id = message.from_user.id
    try:
        parts = text.strip().split()
        if len(parts) != 2:
            raise ValueError
        datetime.strptime(parts[0], "%Y-%m-%d")
        datetime.strptime(parts[1], "%Y-%m-%d")
        start_date = parts[0] + " 00:00:00"
        end_date = parts[1] + " 23:59:59"
        category = data.get("category") if data else None
        await _send_report(message, start_date, end_date, user_id, category)
    except ValueError:
        await message.reply("Неверный формат. Используйте: YYYY-MM-DD YYYY-MM-DD", reply_markup=main_keyboard)
    await reset_user_state(user_id)


async def _send_report(message, start_date, end_date, user_id, category=None):
    """Отчёт за период (без категории) или по категории — с новым форматом."""
    _, symbol = await get_user_currency(user_id)

    if category:
        await _send_category_report(message, start_date, end_date, user_id, category, symbol)
    else:
        await _send_period_report(message, start_date, end_date, user_id, symbol)


async def _send_category_report(message, start_date, end_date, user_id, category, symbol):
    """Детальный отчёт по категории."""
    rows = await get_expenses_by_category_detailed(start_date, end_date, category, user_id)
    if rows is None:
        await message.reply(f"Категория '{category}' не найдена.", reply_markup=main_keyboard)
        return
    if not rows:
        await message.reply(f"Нет данных по категории '{category}' за период.", reply_markup=main_keyboard)
        return

    # Группируем: {name: [(date, amount), ...]} (без учёта регистра)
    items = OrderedDict()
    name_display = {}  # LOWER(name) → первое встреченное написание
    for name, total, date in rows:
        item_name = name or '---'
        key = item_name.lower()
        if key not in name_display:
            name_display[key] = item_name
        display = name_display[key]
        if display not in items:
            items[display] = []
        date_short = date[:10] if date else "---"
        items[display].append((date_short, total))

    report = f"<b>Траты по категории '{html_escape(category)}':</b>\n\n"
    grand_total = 0

    for item_name, entries in items.items():
        item_total = sum(amount for _, amount in entries)
        grand_total += item_total
        report += f"{html_escape(item_name)}: {item_total:.2f} {symbol}\n"
        for date_str, amount in entries:
            report += f"    {date_str} - {amount:.2f} {symbol}\n"
        report += "\n"

    report += f"<b>Общая сумма: {grand_total:.2f} {symbol}</b>"

    # Также готовим данные для графика и Excel (агрегированные)
    expenses_agg = await get_expenses_by_category(start_date, end_date, category, user_id)

    # Определяем группировку
    default_group, drill_options = _determine_grouping(start_date, end_date)

    chart_file = bar_file = excel_file = None
    try:
        await send_long_message(message, report, parse_mode=enums.ParseMode.HTML, reply_markup=main_keyboard)
        if expenses_agg:
            chart_file = await create_pie_chart(expenses_agg, user_id, symbol)
            excel_file = await generate_excel_report(start_date, end_date, user_id)
            if excel_file:
                await message.reply_document(excel_file, reply_markup=main_keyboard)
            await message.reply_photo(chart_file, reply_markup=main_keyboard)

            # Stacked bar (наименования как «категории» стека)
            if default_group:
                bar_rows = [(d, name, None, total) for name, total, d in rows]
                bar_file = await create_stacked_bar_chart(bar_rows, user_id, symbol, default_group)
                if bar_file:
                    await message.reply_photo(bar_file, reply_markup=main_keyboard)
    finally:
        safe_remove(chart_file, bar_file, excel_file)


async def _send_period_report(message, start_date, end_date, user_id, symbol):
    """Детальный отчёт за период."""
    rows = await get_expenses_by_period_detailed(start_date, end_date, user_id)
    if not rows:
        await message.reply("Нет данных за период.", reply_markup=main_keyboard)
        return

    # Форматируем даты для заголовка
    start_display = datetime.strptime(
        start_date[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    end_display = datetime.strptime(
        end_date[:10], "%Y-%m-%d").strftime("%d.%m.%Y")

    # Группируем: {date: {category: [(name, amount), ...]}}
    dates = OrderedDict()
    grand_total = 0

    cat_display = {}  # LOWER(name) → первое встреченное написание
    for date, cat_name, exp_name, total in rows:
        date_short = date[:10] if date else "---"
        if date_short not in dates:
            dates[date_short] = OrderedDict()
        # Объединяем категории с разным регистром
        cat_key = cat_name.lower()
        if cat_key not in cat_display:
            cat_display[cat_key] = cat_name
        display_cat = cat_display[cat_key]
        if display_cat not in dates[date_short]:
            dates[date_short][display_cat] = []
        dates[date_short][display_cat].append((exp_name or '---', total))
        grand_total += total

    report = f"<b>Траты с {start_display} по {end_display}</b>\n\n"

    for date_str, categories in dates.items():
        date_display = datetime.strptime(
            date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        report += f"{date_display}:\n"
        cat_list = list(categories.items())
        for i, (cat_name, items) in enumerate(cat_list):
            report += f"    {html_escape(cat_name)}:\n"
            for exp_name, amount in items:
                report += f"        {html_escape(exp_name)}: {amount:.2f} {symbol}\n"
            if i < len(cat_list) - 1:
                report += "\n"
        report += "\n"

    report += f"<b>Общая сумма: {grand_total:.2f} {symbol}</b>"

    # Определяем группировку для графика
    default_group, drill_options = _determine_grouping(start_date, end_date)

    # Графики и Excel
    expenses_agg = await get_expenses(start_date, end_date, user_id)
    chart_file = bar_file = excel_file = None
    try:
        await send_long_message(message, report, parse_mode=enums.ParseMode.HTML, reply_markup=main_keyboard)

        if expenses_agg:
            # Pie chart
            chart_file = await create_pie_chart(expenses_agg, user_id, symbol)
            excel_file = await generate_excel_report(start_date, end_date, user_id)
            if excel_file:
                await message.reply_document(excel_file, reply_markup=main_keyboard)
            await message.reply_photo(chart_file, reply_markup=main_keyboard)

            # Stacked bar chart (только для периодов > 1 дня)
            if default_group:
                bar_file = await create_stacked_bar_chart(rows, user_id, symbol, default_group)
                if bar_file:
                    # Формируем inline-кнопки для детализации
                    s = start_date[:10].replace('-', '')
                    e = end_date[:10].replace('-', '')
                    buttons = []
                    for opt in drill_options:
                        label = {'day': '📊 По дням',
                                 'week': '📊 По неделям'}[opt]
                        buttons.append(
                            InlineKeyboardButton(label, callback_data=f"chart:{opt}:{s}:{e}"))
                    kb = InlineKeyboardMarkup([buttons]) if buttons else None
                    await message.reply_photo(bar_file, reply_markup=kb or main_keyboard)
    finally:
        safe_remove(chart_file, bar_file, excel_file)


# ─── Обработчик ввода трат (свободный + точный формат) ────────────────────────

async def _propose_fuzzy_category(message, fuzzy_cat_name: str, expense_data: dict, user_id: int):
    """Предлагает пользователю нечётко найденную категорию."""
    await set_user_state(user_id, "pending_fuzzy_category", {
        **expense_data,
        "fuzzy_cat_name": fuzzy_cat_name,
    })
    input_name = expense_data.get('proposed_input', '')
    prefix = f"Категория «{input_name}» не найдена, но есть «{fuzzy_cat_name}».\n" \
        if input_name else f"Найдена похожая категория «{fuzzy_cat_name}».\n"
    await message.reply(prefix + "Использовать её?", reply_markup=fuzzy_category_keyboard(input_name))


async def _propose_create_category(message, proposed_name: str, expense_data: dict, user_id: int):
    """Предлагает пользователю создать новую категорию."""
    await set_user_state(user_id, "pending_create_category", {
        **expense_data,
        "proposed_cat_name": proposed_name,
    })
    await message.reply(
        f"Категория «{proposed_name}» не найдена.\nСоздать её?",
        reply_markup=create_category_keyboard())


async def handle_expense_entry(message, text):
    user_id = message.from_user.id
    _, symbol = await get_user_currency(user_id)

    # --- Точный формат с запятыми или прочел (|) ---
    if ',' in text or '|' in text:
        parts = [p.strip() for p in re.split(r'[,|]', text)]

        if len(parts) == 4:
            try:
                category_input, name, price, quantity = parts
                price, quantity = float(price), float(quantity)
                total = price * quantity
                category_id, category_real = await get_category_id_and_name(category_input, user_id)
                if category_id is None:
                    expense_data = {
                        'name': name, 'price': price, 'quantity': quantity, 'total': total,
                        'proposed_input': category_input,
                    }
                    fuzzy_cat = await find_fuzzy_category(category_input, user_id)
                    if fuzzy_cat:
                        await _propose_fuzzy_category(message, fuzzy_cat, expense_data, user_id)
                    else:
                        await _propose_create_category(message, category_input, expense_data, user_id)
                    return
                expense_id = await log_expense(user_id, category_id, name, price, quantity, total)
                await set_last_category_id(user_id, category_id)
                await message.reply(
                    f"✅ {category_real} — {name}: {total:.2f} {symbol}",
                    reply_markup=undo_keyboard(expense_id))
            except ValueError:
                await message.reply("Неверный формат. Используйте: Категория, Наименование, Цена, Количество")
            return

        if len(parts) == 2:
            try:
                category_input, total = parts[0], float(parts[1])
                category_id, category_real = await get_category_id_and_name(category_input, user_id)
                if category_id is None:
                    expense_data = {
                        'name': None, 'price': None, 'quantity': None, 'total': total,
                        'proposed_input': category_input,
                    }
                    fuzzy_cat = await find_fuzzy_category(category_input, user_id)
                    if fuzzy_cat:
                        await _propose_fuzzy_category(message, fuzzy_cat, expense_data, user_id)
                    else:
                        await _propose_create_category(message, category_input, expense_data, user_id)
                    return
                expense_id = await log_expense(user_id, category_id, None, None, None, total)
                await set_last_category_id(user_id, category_id)
                await message.reply(
                    f"✅ {category_real}: {total:.2f} {symbol}",
                    reply_markup=undo_keyboard(expense_id))
            except ValueError:
                await message.reply("Неверный формат. Используйте: Категория, Сумма")
            return

        await message.reply(
            "Неверный формат. Используйте:\n"
            "Категория, Наименование, Цена, Кол-во\n"
            "или: Категория, Сумма")
        return

    # --- Свободный формат ---
    parsed = await parse_free_form(text, user_id)
    if parsed is None:
        await message.reply(
            "Не удалось распознать трату. Примеры:\n"
            "Продукты яблоки 100\n"
            "Такси 500\n"
            "Продукты, Яблоки, 100, 2",
            reply_markup=main_keyboard)
        return

    if parsed['category']:
        # Категория найдена — записываем сразу
        category_id = await get_category_id(parsed['category'], user_id)
        if category_id is None:
            await message.reply("Ошибка: категория не найдена.", reply_markup=main_keyboard)
            return
        expense_id = await log_expense(
            user_id, category_id, parsed['name'],
            parsed['price'], parsed['quantity'], parsed['total'])
        await set_last_category_id(user_id, category_id)

        label = f"{parsed['category']} — {parsed['name']}" if parsed['name'] else parsed['category']
        await message.reply(
            f"✅ {label}: {parsed['total']:.2f} {symbol}",
            reply_markup=undo_keyboard(expense_id))
        return

    # Категория не найдена в свободном формате
    numbers, words = extract_numbers_and_words(text)

    if words:
        # Есть слова — пробуем нечёткий поиск по префиксам слов
        fuzzy_cat, remaining_words = await find_fuzzy_from_words(words, user_id)
        if fuzzy_cat:
            # Нашли нечёткое совпадение
            input_candidate = ' '.join(
                words[:len(words) - len(remaining_words)])
            expense_data = {
                'name': ' '.join(remaining_words).strip() or parsed.get('name'),
                'price': parsed['price'],
                'quantity': parsed['quantity'],
                'total': parsed['total'],
                'proposed_input': input_candidate,
            }
            await _propose_fuzzy_category(message, fuzzy_cat, expense_data, user_id)
            return

        # Нечёткого совпадения нет — предлагаем последнюю категорию или создать
        last_cat_id = await get_last_category_id(user_id)
        if last_cat_id:
            last_cat_name = await get_category_name_by_id(last_cat_id)
            if last_cat_name:
                await set_user_state(user_id, "pending_expense", {
                    "name": parsed['name'],
                    "price": parsed['price'],
                    "quantity": parsed['quantity'],
                    "total": parsed['total'],
                    "category_id": last_cat_id,
                    "category_name": last_cat_name,
                })
                label = f"{parsed['name']} {parsed['total']:.2f}" if parsed['name'] \
                    else f"{parsed['total']:.2f}"
                await message.reply(
                    f"Записать «{label} {symbol}» в категорию «{last_cat_name}»?",
                    reply_markup=confirm_category_keyboard())
                return

        # Предлагаем создать категорию из первого слова
        proposed_name = words[0] if words else text.strip()
        expense_data = {
            'name': parsed.get('name'),
            'price': parsed['price'],
            'quantity': parsed['quantity'],
            'total': parsed['total'],
            'proposed_input': proposed_name,
        }
        await _propose_create_category(message, proposed_name, expense_data, user_id)

    else:
        # Нет слов (только числа) — предлагаем последнюю категорию
        last_cat_id = await get_last_category_id(user_id)
        if last_cat_id:
            last_cat_name = await get_category_name_by_id(last_cat_id)
            if last_cat_name:
                await set_user_state(user_id, "pending_expense", {
                    "name": parsed['name'],
                    "price": parsed['price'],
                    "quantity": parsed['quantity'],
                    "total": parsed['total'],
                    "category_id": last_cat_id,
                    "category_name": last_cat_name,
                })
                label = f"{parsed['total']:.2f}"
                await message.reply(
                    f"Записать «{label} {symbol}» в категорию «{last_cat_name}»?",
                    reply_markup=confirm_category_keyboard())
                return

        await message.reply(
            "Категория не найдена. Укажите категорию или добавьте новую.",
            reply_markup=main_keyboard)


async def handle_all_expenses(message):
    user_id = message.from_user.id
    _, symbol = await get_user_currency(user_id)
    expenses = await get_all_expenses(user_id)
    if not expenses:
        await message.reply("Нет записей о тратах.", reply_markup=main_keyboard)
        return

    report = "<b>Все траты:</b>\n\n"
    for idx, (exp_id, category, name, price, quantity, total, date) in enumerate(expenses, 1):
        date_short = date[:10] if date else "---"
        report += f"{idx}. {date_short} | {html_escape(category)} | {html_escape(name) if name else '---'} | {total:.2f} {symbol}\n"

    await send_long_message(message, report, parse_mode=enums.ParseMode.HTML, reply_markup=main_keyboard)


# ─── Callback-обработчик (inline-кнопки) ─────────────────────────────────────

@app.on_callback_query()
async def handle_callback(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data

    # ── Отмена последней траты ──
    if data.startswith("undo:"):
        expense_id = int(data.split(":")[1])
        if await delete_expense_by_id(expense_id, user_id):
            await callback_query.message.edit_text("❌ Трата отменена.")
        else:
            await callback_query.message.edit_text("Не удалось отменить (уже удалена).")
        await callback_query.answer()
        return

    # ── Детализация графика ──
    if data.startswith("chart:"):
        parts = data.split(":")
        if len(parts) == 4:
            group_by, s, e = parts[1], parts[2], parts[3]
            start_date = f"{s[:4]}-{s[4:6]}-{s[6:8]} 00:00:00"
            end_date = f"{e[:4]}-{e[4:6]}-{e[6:8]} 23:59:59"

            await callback_query.answer("Строю график...")

            _, symbol = await get_user_currency(user_id)
            rows = await get_expenses_by_period_detailed(start_date, end_date, user_id)

            if rows:
                bar_file = await create_stacked_bar_chart(rows, user_id, symbol, group_by)
                if bar_file:
                    # Кнопки для дальнейшей детализации
                    buttons = []
                    if group_by == 'week':
                        buttons.append(
                            InlineKeyboardButton("📊 По дням", callback_data=f"chart:day:{s}:{e}"))
                    kb = InlineKeyboardMarkup([buttons]) if buttons else None

                    await callback_query.message.reply_photo(bar_file, reply_markup=kb)
                    safe_remove(bar_file)
            else:
                await callback_query.message.reply("Нет данных за период.")
        return

    # ── Подтверждение последней категории ──
    if data == "cat_yes":
        state, state_data = await get_user_state(user_id)
        if state != "pending_expense" or not state_data:
            await callback_query.answer("Данные устарели, введите трату заново.")
            return

        _, symbol = await get_user_currency(user_id)
        expense_id = await log_expense(
            user_id, state_data['category_id'], state_data.get('name'),
            state_data.get('price'), state_data.get('quantity'), state_data['total'])

        await set_last_category_id(user_id, state_data['category_id'])
        await reset_user_state(user_id)

        label = f"{state_data['category_name']} — {state_data['name']}" \
            if state_data.get('name') else state_data['category_name']

        await callback_query.message.edit_text(
            f"✅ {label}: {state_data['total']:.2f} {symbol}",
            reply_markup=undo_keyboard(expense_id))
        await callback_query.answer()
        return

    if data == "cat_other":
        # Сохраняем данные о трате при смене категории
        state, pending = await get_user_state(user_id)
        kb = await category_keyboard(user_id)
        await callback_query.message.edit_text("Выберите категорию:")
        await app.send_message(user_id, "Выберите категорию:", reply_markup=kb)
        await set_user_state(user_id, "choose_category_for_expense", pending)
        await callback_query.answer()
        return

    # ── Подтверждение шаблона ──
    if data.startswith("tpl_yes:"):
        tpl_id = int(data.split(":")[1])
        try:
            async with DatabaseConnection() as cursor:
                await cursor.execute('''
                    SELECT t.user_id, t.category_id, t.name, t.amount, c.name
                    FROM recurring_templates t
                    JOIN categories c ON t.category_id = c.id
                    WHERE t.id = ?
                ''', (tpl_id,))
                row = await cursor.fetchone()

            if row and row[0] == user_id:
                tpl_user_id, cat_id, tpl_name, amount, cat_name = row
                _, symbol = await get_user_currency(user_id)
                expense_id = await log_expense(user_id, cat_id, tpl_name, None, None, amount)
                label = f"{cat_name} — {tpl_name}" if tpl_name else cat_name
                await callback_query.message.edit_text(
                    f"✅ {label}: {amount:.2f} {symbol}",
                    reply_markup=undo_keyboard(expense_id))
            else:
                await callback_query.message.edit_text("Шаблон не найден.")
        except Exception as e:
            logging.error(f"Ошибка tpl_yes: {e}")
            await callback_query.message.edit_text("Произошла ошибка.")
        await callback_query.answer()
        return

    if data.startswith("tpl_no:"):
        await callback_query.message.edit_text("⏭ Пропущено.")
        await callback_query.answer()
        return

    # ── Возврат в главное меню ──
    if data == "back_to_menu":
        await reset_user_state(user_id)
        await callback_query.message.edit_text("Главное меню.")
        await app.send_message(user_id, "Главное меню.", reply_markup=main_keyboard)
        await callback_query.answer()
        return

    # ── Создание новой категории для ожидающей траты ──
    if data == "create_cat_yes":
        state, state_data = await get_user_state(user_id)
        if state != "pending_create_category" or not state_data:
            await callback_query.answer("Данные устарели, введите трату заново.")
            return

        cat_name = state_data.get('proposed_cat_name', '')
        _, symbol = await get_user_currency(user_id)

        if not cat_name:
            await callback_query.answer("Не удалось определить имя категории.")
            return

        await add_category(cat_name, user_id)
        category_id = await get_category_id(cat_name, user_id)

        if category_id is None:
            await callback_query.message.edit_text("Ошибка создания категории.")
            await callback_query.answer()
            return

        expense_id = await log_expense(
            user_id, category_id, state_data.get('name'),
            state_data.get('price'), state_data.get('quantity'), state_data['total'])
        await set_last_category_id(user_id, category_id)
        await reset_user_state(user_id)

        label = f"{cat_name} — {state_data['name']}" if state_data.get(
            'name') else cat_name
        await callback_query.message.edit_text(
            f"✅ Категория «{cat_name}» создана.\n"
            f"✅ {label}: {state_data['total']:.2f} {symbol}",
            reply_markup=undo_keyboard(expense_id))
        await callback_query.answer()
        return

    if data == "create_cat_no":
        state, state_data = await get_user_state(user_id)
        # Переводим в режим выбора категории для записи траты
        await set_user_state(user_id, "choose_category_for_expense", {
            k: v for k, v in state_data.items() if k != 'proposed_cat_name'
        })
        kb = await category_keyboard(user_id)
        await callback_query.message.edit_text(
            "Трата должна быть привязана к категории.\n"
            "Выберите существующую категорию или создайте новую через меню «Категории»."
        )
        await app.send_message(user_id, "Выберите категорию:", reply_markup=kb)
        await callback_query.answer()
        return

    # ── Нечёткое совпадение категории ──
    if data == "fuzzy_cat_yes":
        state, state_data = await get_user_state(user_id)
        if state != "pending_fuzzy_category" or not state_data:
            await callback_query.answer("Данные устарели, введите трату заново.")
            return

        fuzzy_cat_name = state_data.get('fuzzy_cat_name', '')
        _, symbol = await get_user_currency(user_id)

        category_id = await get_category_id(fuzzy_cat_name, user_id)
        if category_id is None:
            await callback_query.message.edit_text("Категория не найдена.")
            await callback_query.answer()
            return

        expense_id = await log_expense(
            user_id, category_id, state_data.get('name'),
            state_data.get('price'), state_data.get('quantity'), state_data['total'])
        await set_last_category_id(user_id, category_id)
        await reset_user_state(user_id)

        label = f"{fuzzy_cat_name} — {state_data['name']}" if state_data.get(
            'name') else fuzzy_cat_name
        await callback_query.message.edit_text(
            f"✅ {label}: {state_data['total']:.2f} {symbol}",
            reply_markup=undo_keyboard(expense_id))
        await callback_query.answer()
        return

    if data == "fuzzy_cat_new":
        state, state_data = await get_user_state(user_id)
        if state != "pending_fuzzy_category" or not state_data:
            await callback_query.answer("Данные устарели, введите трату заново.")
            return

        # Предлагаем создать новую категорию с тем именем, что ввёл пользователь
        proposed_name = state_data.get('proposed_input', '')
        new_state_data = {
            k: v for k, v in state_data.items()
            if k not in ('fuzzy_cat_name',)
        }
        new_state_data['proposed_cat_name'] = proposed_name

        await set_user_state(user_id, "pending_create_category", new_state_data)
        await callback_query.message.edit_text(
            f"Создать категорию «{proposed_name}»?",
            reply_markup=create_category_keyboard())
        await callback_query.answer()
        return

    if data == "fuzzy_cat_back":
        await reset_user_state(user_id)
        await callback_query.message.edit_text("Отменено.")
        await app.send_message(user_id, "Главное меню.", reply_markup=main_keyboard)
        await callback_query.answer()
        return

    await callback_query.answer()


# ─── Главный обработчик сообщений ────────────────────────────────────────────

@app.on_message(filters.text & filters.private)
async def handle_message(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    logging.info(f"[{user_id}] {text}")

    state, data = await get_user_state(user_id)

    # ── Команды ──

    if text == "/start":
        await message.reply(
            "Привет! Я помогу следить за расходами.\n\n"
            "Просто пиши траты в свободном формате:\n"
            "Продукты яблоки 100\n"
            "Такси 500\n\n"
            "Команды:\n"
            "/currency KZT — установить валюту\n"
            "/timezone Asia/Almaty — часовой пояс\n"
            "/help — справка",
            reply_markup=main_keyboard)
        await send_pinned_template_message(client, message)
        await reset_user_state(user_id)
        return

    if text.startswith("/currency"):
        parts = text.split()
        if len(parts) < 2:
            _, symbol = await get_user_currency(user_id)
            codes = ', '.join(CURRENCY_SYMBOLS.keys())
            await message.reply(
                f"Текущая валюта: {symbol}\n"
                f"Доступные: {codes}\n\n"
                f"Использование: /currency KZT")
            return
        code = parts[1].upper()
        if code not in CURRENCY_SYMBOLS:
            await message.reply(f"Неизвестная валюта. Доступные: {', '.join(CURRENCY_SYMBOLS.keys())}")
            return
        await set_user_currency(user_id, code)
        await message.reply(f"Валюта установлена: {CURRENCY_SYMBOLS[code]} ({code})", reply_markup=main_keyboard)
        return

    if text.startswith("/timezone"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tz_name = await get_user_timezone_name(user_id)
            await message.reply(
                f"Текущий часовой пояс: {tz_name}\n\n"
                f"Использование: /timezone Asia/Almaty")
            return
        tz_input = parts[1].strip()
        success = await set_user_timezone(user_id, tz_input)
        if success:
            await message.reply(f"Часовой пояс установлен: {tz_input}", reply_markup=main_keyboard)
        else:
            await message.reply(f"Неизвестный часовой пояс: {tz_input}")
        return

    if text == "/help":
        await message.reply(HELP_TEXT, reply_markup=main_keyboard)
        return

    # ── Меню ──

    if text == "Назад":
        await message.reply("Главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    if text == "Категории":
        await message.reply("Выберите действие:", reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    if text == "Мои категории":
        categories = await get_categories(user_id)
        await message.reply(
            "\n".join(categories) if categories else "Категорий пока нет.",
            reply_markup=category_submenu)
        return

    if text == "Добавить категорию":
        await message.reply("Введите название категории:", reply_markup=ForceReply())
        await set_user_state(user_id, "adding_category")
        return

    if text == "Удалить категорию":
        categories = await get_categories(user_id)
        if categories:
            await message.reply(
                "Ваши категории:\n" +
                "\n".join(categories) + "\n\nВведите id для удаления:",
                reply_markup=ForceReply())
            await set_user_state(user_id, "delete_category")
        else:
            await message.reply("Категорий пока нет.", reply_markup=category_submenu)
        return

    if text == "Объединить категории":
        categories = await get_categories(user_id)
        if len(categories) < 2:
            await message.reply(
                "Для объединения нужно минимум две категории.",
                reply_markup=category_submenu)
            return
        back_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)
        await message.reply(
            "Ваши категории:\n" + "\n".join(categories) +
            "\n\nВведите id или названия категорий для объединения "
            "(через запятую или пробел).\nПример: 1, 3  или  Такси, тахи",
            reply_markup=back_kb)
        await set_user_state(user_id, "merge_categories_input")
        return

    if text == "Отчет":
        await handle_report(message)
        return

    if text == "По категориям":
        await handle_report_category(message)
        return

    if text == "За период":
        await message.reply("Выберите период:", reply_markup=period_keyboard)
        await set_user_state(user_id, "choose_period", {})
        return

    if text == "Все траты":
        await handle_all_expenses(message)
        return

    # ── Сервис ──

    if text == "Сервис":
        await message.reply("Сервис:", reply_markup=service_submenu)
        await reset_user_state(user_id)
        return

    if text == "Валюта":
        _, symbol = await get_user_currency(user_id)
        codes = ', '.join(CURRENCY_SYMBOLS.keys())
        await message.reply(
            f"Текущая валюта: {symbol}\n"
            f"Доступные: {codes}\n\n"
            f"Использование: /currency KZT",
            reply_markup=service_submenu)
        return

    if text == "Часовой пояс":
        tz_name = await get_user_timezone_name(user_id)
        await message.reply(
            f"Текущий часовой пояс: {tz_name}\n\n"
            "Выберите из списка или введите вручную\n"
            "(например: Europe/Moscow, Asia/Almaty):",
            reply_markup=timezone_keyboard)
        await set_user_state(user_id, "set_timezone")
        return

    if text == "Справка":
        await message.reply(HELP_TEXT, reply_markup=service_submenu)
        return

    if text == "Редактировать траты":
        await message.reply("Выберите период:", reply_markup=edit_period_keyboard)
        await set_user_state(user_id, "edit_choose_period")
        return

    if text == "Удалить траты":
        await message.reply("Выберите период:", reply_markup=edit_period_keyboard)
        await set_user_state(user_id, "del_choose_period")
        return

    # ── Шаблоны ──

    if text == "Шаблоны":
        await message.reply("Управление шаблонами:", reply_markup=template_submenu)
        await reset_user_state(user_id)
        return

    if text == "Мои шаблоны":
        templates = await get_templates(user_id)
        _, symbol = await get_user_currency(user_id)
        if templates:
            lines = []
            for tpl_id, cat_name, tpl_name, amount, day in templates:
                label = f"{cat_name} — {tpl_name}" if tpl_name else cat_name
                lines.append(
                    f"{tpl_id}: {label}, {amount:.0f} {symbol}, {day}-го числа")
            await message.reply("\n".join(lines), reply_markup=template_submenu)
        else:
            await message.reply("Шаблонов пока нет.", reply_markup=template_submenu)
        return

    if text == "Добавить шаблон":
        await message.reply(
            "Введите шаблон в формате:\n"
            "Категория, Наименование, Сумма, День\n\n"
            "Пример: Аренда, Квартира, 150000, 5\n"
            "Или: Аренда, 150000, 5",
            reply_markup=ForceReply())
        await set_user_state(user_id, "adding_template")
        return

    if text == "Удалить шаблон":
        templates = await get_templates(user_id)
        _, symbol = await get_user_currency(user_id)
        if templates:
            lines = []
            for tpl_id, cat_name, tpl_name, amount, day in templates:
                label = f"{cat_name} — {tpl_name}" if tpl_name else cat_name
                lines.append(
                    f"{tpl_id}: {label}, {amount:.0f} {symbol}, {day}-го числа")
            await message.reply(
                "\n".join(lines) + "\n\nВведите id шаблона для удаления:",
                reply_markup=ForceReply())
            await set_user_state(user_id, "delete_template")
        else:
            await message.reply("Шаблонов пока нет.", reply_markup=template_submenu)
        return

    # ── Обработка состояний ──

    if state == "adding_category":
        name = text.strip()
        if name:
            await add_category(name, user_id)
            await message.reply(f"Категория '{name}' добавлена.", reply_markup=category_submenu)
        else:
            await message.reply("Название не должно быть пустым.", reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    if state == "delete_category":
        try:
            category_id = int(text)
            await handle_delete_category(message, category_id)
        except ValueError:
            await message.reply("Введите число.", reply_markup=category_submenu)
        return

    if state == "confirm_delete":
        category_id = data.get("category_id")
        if text == "Да":
            await delete_expenses_for_category(category_id, user_id)
            await delete_category(category_id, user_id)
            await message.reply("Категория и траты удалены.", reply_markup=category_submenu)
        else:
            await message.reply("Удаление отменено.", reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    if state == "choose_category":
        await handle_choose_category(message, text)
        return

    if state == "choose_period":
        await handle_period_report(message, text, data)
        return

    if state == "manual_dates":
        await handle_manual_dates(message, text, data)
        return

    if state == "adding_template":
        parts = [p.strip() for p in text.split(',')]
        try:
            if len(parts) == 4:
                cat_name, tpl_name, amount, day = parts
                amount, day = float(amount), int(day)
            elif len(parts) == 3:
                cat_name, amount, day = parts
                tpl_name = None
                amount, day = float(amount), int(day)
            else:
                await message.reply("Неверный формат. Используйте: Категория, Сумма, День",
                                    reply_markup=template_submenu)
                await reset_user_state(user_id)
                return

            if not (1 <= day <= 28):
                await message.reply("День должен быть от 1 до 28.", reply_markup=template_submenu)
                await reset_user_state(user_id)
                return

            category_id = await get_category_id(cat_name, user_id)
            if category_id is None:
                await message.reply(f"Категория '{cat_name}' не найдена.", reply_markup=template_submenu)
                await reset_user_state(user_id)
                return

            await add_template(user_id, category_id, tpl_name, amount, day)
            _, symbol = await get_user_currency(user_id)
            label = f"{cat_name} — {tpl_name}" if tpl_name else cat_name
            await message.reply(
                f"Шаблон добавлен: {label}, {amount:.0f} {symbol}, {day}-го числа",
                reply_markup=main_keyboard)
        except ValueError:
            await message.reply("Неверный формат числа.", reply_markup=template_submenu)
        await reset_user_state(user_id)
        return

    if state == "delete_template":
        try:
            tpl_id = int(text)
            await delete_template(tpl_id, user_id)
            await message.reply("Шаблон удалён.", reply_markup=template_submenu)
        except ValueError:
            await message.reply("Введите число.", reply_markup=template_submenu)
        await reset_user_state(user_id)
        return

    # Выбор категории для траты из pending_expense (после "Другая")
    if state == "choose_category_for_expense":
        if text == "Назад":
            await message.reply("Главное меню.", reply_markup=main_keyboard)
            await reset_user_state(user_id)
            return

        category_id = await get_category_id(text, user_id)
        if category_id is None:
            await message.reply("Неверная категория.", reply_markup=await category_keyboard(user_id))
            return

        pending = data if data else {}
        _, symbol = await get_user_currency(user_id)

        if pending.get('total'):
            expense_id = await log_expense(
                user_id, category_id, pending.get('name'),
                pending.get('price'), pending.get('quantity'), pending['total'])
            await set_last_category_id(user_id, category_id)
            label = f"{text} — {pending['name']}" if pending.get(
                'name') else text
            await message.reply(
                f"✅ {label}: {pending['total']:.2f} {symbol}",
                reply_markup=undo_keyboard(expense_id))
        else:
            await message.reply("Данные устарели, введите трату заново.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    # ── Объединение категорий: шаг 1 — ввод категорий ──
    if state == "merge_categories_input":
        if text == "Назад":
            await message.reply("Главное меню.", reply_markup=main_keyboard)
            await reset_user_state(user_id)
            return

        # Разбиваем по запятым и пробелам
        raw_tokens = re.split(r'[,\s]+', text.strip())
        raw_tokens = [t.strip() for t in raw_tokens if t.strip()]

        found_ids = []
        not_found = []

        for token in raw_tokens:
            # Пробуем как id
            try:
                cat_id = int(token)
                cat_name = await get_category_name_by_id_for_user(cat_id, user_id)
                if cat_name:
                    found_ids.append((cat_id, cat_name))
                else:
                    not_found.append(token)
            except ValueError:
                # Пробуем как название (case-insensitive)
                cat_id = await get_category_id(token, user_id)
                if cat_id is not None:
                    cat_name = await get_category_name_by_id_for_user(cat_id, user_id)
                    found_ids.append((cat_id, cat_name))
                else:
                    not_found.append(token)

        # Убираем дубликаты
        seen = set()
        unique_found = []
        for cat_id, cat_name in found_ids:
            if cat_id not in seen:
                seen.add(cat_id)
                unique_found.append((cat_id, cat_name))

        if len(unique_found) < 2:
            back_kb = ReplyKeyboardMarkup(
                [[KeyboardButton("Назад")]],
                resize_keyboard=True, one_time_keyboard=True)
            msg = "Найдено менее двух категорий"
            if not_found:
                msg += f"\nНе найдено: {', '.join(not_found)}"
            msg += "\n\nВведите заново или нажмите «Назад»."
            await message.reply(msg, reply_markup=back_kb)
            return

        names_list = "\n".join(
            f"  • {name} (id {cid})" for cid, name in unique_found)
        warn = ""
        if not_found:
            warn = f"\n\n⚠️ Не найдено: {', '.join(not_found)}"

        back_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)

        await message.reply(
            f"Будут объединены:\n{names_list}{warn}"
            "\n\nКак назвать итоговую категорию?\n"
            "(введите новое название или одно из существующих)",
            reply_markup=back_kb)

        ids_only = [cid for cid, _ in unique_found]
        await set_user_state(user_id, "merge_categories_name", {"ids": ids_only})
        return

    # ── Объединение категорий: шаг 2 — ввод имени ──
    if state == "merge_categories_name":
        if text == "Назад":
            categories = await get_categories(user_id)
            back_kb = ReplyKeyboardMarkup(
                [[KeyboardButton("Назад")]],
                resize_keyboard=True, one_time_keyboard=True)
            await message.reply(
                "Ваши категории:\n" + "\n".join(categories) +
                "\n\nВведите id или названия категорий для объединения.",
                reply_markup=back_kb)
            await set_user_state(user_id, "merge_categories_input")
            return

        new_name = text.strip()
        if not new_name:
            await message.reply("Название не должно быть пустым.")
            return

        ids_to_merge = data.get("ids", [])
        result_id = await merge_categories_db(ids_to_merge, new_name, user_id)

        if result_id:
            await message.reply(
                f"✅ Категории успешно объединены в «{new_name}».",
                reply_markup=category_submenu)
        else:
            await message.reply(
                "Ошибка при объединении категорий.",
                reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    # ── Установка часового пояса ──
    if state == "set_timezone":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        # Проверяем, выбрал ли пользователь из кнопок
        tz_name = POPULAR_TIMEZONES.get(text)
        if tz_name is None:
            # Пробуем как прямой ввод (например: Europe/Moscow)
            tz_name = text.strip()

        success = await set_user_timezone(user_id, tz_name)
        if success:
            await message.reply(
                f"✅ Часовой пояс установлен: {tz_name}",
                reply_markup=main_keyboard)
        else:
            await message.reply(
                f"Неизвестный часовой пояс: «{text}».\n"
                "Выберите из списка или введите в формате: Region/City",
                reply_markup=timezone_keyboard)
            return
        await reset_user_state(user_id)
        return

    # ── Редактирование трат: шаг 1 — выбор периода ──
    if state == "edit_choose_period":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        periods = {"День": 0, "Неделя": 7, "Месяц": 30}
        days = periods.get(text)
        if days is None:
            await message.reply("Выберите период:", reply_markup=edit_period_keyboard)
            return

        user_tz = await get_user_timezone(user_id)
        today = datetime.now(user_tz)
        start = today.strftime("%Y-%m-%d 00:00:00") if days == 0 \
            else (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        end = today.strftime("%Y-%m-%d %H:%M:%S")

        expenses = await get_expenses_for_period(start, end, user_id)
        if not expenses:
            await message.reply("Нет трат за этот период.", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        _, symbol = await get_user_currency(user_id)
        # Формируем нумерованный список и запоминаем маппинг номер→id
        lines = []
        id_map = {}
        for idx, (exp_id, cat_name, exp_name, total, date) in enumerate(expenses, 1):
            date_short = date[:10] if date else "---"
            label = f"{cat_name} — {exp_name}" if exp_name else cat_name
            lines.append(
                f"{idx}. {date_short} | {label} | {total:.2f} {symbol}")
            id_map[str(idx)] = exp_id

        report = "\n".join(lines)
        back_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)

        await send_long_message(
            message,
            f"{report}\n\nВведите номер записи для редактирования:",
            reply_markup=back_kb)
        await set_user_state(user_id, "edit_choose_expense", {"id_map": id_map})
        return

    # ── Редактирование трат: шаг 2 — выбор записи ──
    if state == "edit_choose_expense":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        id_map = data.get("id_map", {})
        if text not in id_map:
            await message.reply("Неверный номер. Введите номер из списка.")
            return

        expense_id = id_map[text]
        _, symbol = await get_user_currency(user_id)

        # Получаем данные записи
        try:
            async with DatabaseConnection() as cursor:
                await cursor.execute('''
                    SELECT c.name, e.name, e.price, e.quantity, e.total, e.date
                    FROM expenses e JOIN categories c ON e.category_id = c.id
                    WHERE e.id = ? AND e.user_id = ?
                ''', (expense_id, user_id))
                row = await cursor.fetchone()
        except aiosqlite.Error:
            row = None

        if not row:
            await message.reply("Запись не найдена.", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        cat_name, exp_name, price, quantity, total, date = row
        date_short = date[:10] if date else "---"
        label = f"{cat_name} — {exp_name}" if exp_name else cat_name
        details = f"{date_short} | {label} | {total:.2f} {symbol}"

        back_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)

        await message.reply(
            f"Редактируем запись:\n{details}\n\n"
            "Введите новые данные:\n"
            "Категория, Наименование, Сумма\n"
            "или: Категория, Наименование, Цена, Количество\n"
            "или: Категория, Сумма",
            reply_markup=back_kb)
        await set_user_state(user_id, "edit_enter_new", {"expense_id": expense_id})
        return

    # ── Редактирование трат: шаг 3 — ввод новых данных ──
    if state == "edit_enter_new":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        expense_id = data.get("expense_id")
        _, symbol = await get_user_currency(user_id)

        category_input = name = None
        price = quantity = total = None

        # Проверяем наличие разделителей
        if ',' in text or '|' in text:
            # Точный формат с разделителями
            parts = [p.strip() for p in re.split(r'[,|]', text)]

            try:
                if len(parts) == 4:
                    category_input, name, price, quantity = parts
                    price, quantity = float(price), float(quantity)
                    total = price * quantity
                elif len(parts) == 3:
                    category_input, name, total = parts
                    total = float(total)
                elif len(parts) == 2:
                    category_input, total = parts
                    total = float(total)
                else:
                    await message.reply(
                        "Неверный формат. Используйте:\n"
                        "Категория, Наименование, Сумма\n"
                        "или: Категория, Наименование, Цена, Количество\n"
                        "или: Категория, Сумма")
                    return
            except ValueError:
                await message.reply("Неверный формат числа.")
                return
        else:
            # Свободный формат через пробелы
            tokens = text.split()
            if not tokens:
                await message.reply("Пустой ввод.")
                return

            # Собираем числа и слова
            numbers = []
            words = []
            for token in tokens:
                try:
                    numbers.append(float(token))
                except ValueError:
                    words.append(token)

            if not numbers:
                await message.reply("Не найдено чисел. Введите сумму.")
                return

            if not words:
                await message.reply("Не найдено категории. Введите категорию.")
                return

            # Первое слово - категория
            category_input = words[0]

            # Остальные слова - название (если есть)
            if len(words) > 1:
                name = ' '.join(words[1:])

            # Числа
            if len(numbers) == 1:
                total = numbers[0]
            elif len(numbers) >= 2:
                price = numbers[0]
                quantity = numbers[1]
                total = price * quantity

        category_id = await get_category_id(category_input, user_id)
        if category_id is None:
            await message.reply(
                f"Категория '{category_input}' не найдена. Проверьте название.",
                reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        success = await update_expense(expense_id, user_id, category_id, name, price, quantity, total)
        if success:
            label = f"{category_input} — {name}" if name else category_input
            await message.reply(
                f"✅ Запись обновлена: {label}: {total:.2f} {symbol}",
                reply_markup=main_keyboard)
        else:
            await message.reply("Не удалось обновить запись.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    # ── Удаление трат: шаг 1 — выбор периода ──
    if state == "del_choose_period":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        periods = {"День": 0, "Неделя": 7, "Месяц": 30}
        days = periods.get(text)
        if days is None:
            await message.reply("Выберите период:", reply_markup=edit_period_keyboard)
            return

        user_tz = await get_user_timezone(user_id)
        today = datetime.now(user_tz)
        start = today.strftime("%Y-%m-%d 00:00:00") if days == 0 \
            else (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        end = today.strftime("%Y-%m-%d %H:%M:%S")

        expenses = await get_expenses_for_period(start, end, user_id)
        if not expenses:
            await message.reply("Нет трат за этот период.", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        _, symbol = await get_user_currency(user_id)
        lines = []
        id_map = {}
        for idx, (exp_id, cat_name, exp_name, total, date) in enumerate(expenses, 1):
            date_short = date[:10] if date else "---"
            label = f"{cat_name} — {exp_name}" if exp_name else cat_name
            lines.append(
                f"{idx}. {date_short} | {label} | {total:.2f} {symbol}")
            id_map[str(idx)] = exp_id

        report = "\n".join(lines)
        back_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True)

        await send_long_message(
            message,
            f"{report}\n\nВведите номер записи для удаления:",
            reply_markup=back_kb)
        await set_user_state(user_id, "del_choose_expense", {"id_map": id_map})
        return

    # ── Удаление трат: шаг 2 — выбор записи ──
    if state == "del_choose_expense":
        if text == "Назад":
            await message.reply("Сервис:", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        id_map = data.get("id_map", {})
        if text not in id_map:
            await message.reply("Неверный номер. Введите номер из списка.")
            return

        expense_id = id_map[text]
        _, symbol = await get_user_currency(user_id)

        try:
            async with DatabaseConnection() as cursor:
                await cursor.execute('''
                    SELECT c.name, e.name, e.total, e.date
                    FROM expenses e JOIN categories c ON e.category_id = c.id
                    WHERE e.id = ? AND e.user_id = ?
                ''', (expense_id, user_id))
                row = await cursor.fetchone()
        except aiosqlite.Error:
            row = None

        if not row:
            await message.reply("Запись не найдена.", reply_markup=service_submenu)
            await reset_user_state(user_id)
            return

        cat_name, exp_name, total, date = row
        date_short = date[:10] if date else "---"
        label = f"{cat_name} — {exp_name}" if exp_name else cat_name
        details = f"{date_short} | {label} | {total:.2f} {symbol}"

        confirm_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Да"), KeyboardButton("Нет")]],
            resize_keyboard=True, one_time_keyboard=True)

        await message.reply(
            f"Удалить запись?\n{details}",
            reply_markup=confirm_kb)
        await set_user_state(user_id, "del_confirm", {"expense_id": expense_id})
        return

    # ── Удаление трат: шаг 3 — подтверждение ──
    if state == "del_confirm":
        expense_id = data.get("expense_id")
        if text == "Да":
            success = await delete_expense_by_id(expense_id, user_id)
            if success:
                await message.reply("✅ Запись удалена.", reply_markup=main_keyboard)
            else:
                await message.reply("Не удалось удалить запись.", reply_markup=main_keyboard)
        else:
            await message.reply("Удаление отменено.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    # ── Ввод траты (свободный + точный формат) ──
    await handle_expense_entry(message, text)


# ─── Планировщик ─────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler(timezone=pytz.UTC)

# Проверяем каждый час — функции сами определяют, нужно ли отправлять по часовому поясу пользователя
scheduler.add_job(send_monthly_comparisons, 'cron', hour='*', minute=0,
                  id='monthly_comparison', replace_existing=True)

scheduler.add_job(send_template_reminders, 'cron', hour='*', minute=0,
                  id='template_reminders', replace_existing=True)


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    await ensure_timezone_column()
    async with app:
        scheduler.start()
        logging.info("Бот запущен. Планировщик активен.")
        await idle()
        scheduler.shutdown()
        await DatabaseConnection.close()

if __name__ == "__main__":
    app.run(main())
