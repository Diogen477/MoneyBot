import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta

import aiosqlite
import pandas as pd
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from matplotlib import pyplot as plt
from openpyxl.styles import Font
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from setup_db import DatabaseConnection

# ─── Конфигурация ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()

api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('BOT_TOKEN')

if not all([api_id, api_hash, bot_token]):
    raise RuntimeError("Не заданы API_ID, API_HASH или BOT_TOKEN. Проверьте .env файл.")

app = Client("expense_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

ALMATY_TZ = pytz.timezone('Asia/Almaty')
MAX_MESSAGE_LENGTH = 4000

CURRENCY_SYMBOLS = {
    'KZT': '₸', 'RUB': '₽', 'USD': '$', 'EUR': '€',
    'UAH': '₴', 'GBP': '£', 'UZS': 'сўм', 'KGS': 'сом',
}

# ─── Клавиатуры ──────────────────────────────────────────────────────────────

main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Категории"), KeyboardButton("Шаблоны")],
        [KeyboardButton("Отчет"), KeyboardButton("Отчет по категории")],
        [KeyboardButton("Все траты")],
    ],
    resize_keyboard=True,
)

category_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Мои категории"), KeyboardButton("Добавить категорию")],
        [KeyboardButton("Удалить категорию"), KeyboardButton("Назад")],
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
        [KeyboardButton("Месяц"), KeyboardButton("Квартал"), KeyboardButton("Год")],
        [KeyboardButton("Ввести даты вручную")],
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩ Отменить", callback_data=f"undo:{expense_id}")]
    ])


def confirm_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data="cat_yes"),
         InlineKeyboardButton("Другая", callback_data="cat_other")]
    ])


def template_confirm_keyboard(template_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записать", callback_data=f"tpl_yes:{template_id}"),
         InlineKeyboardButton("❌ Пропустить", callback_data=f"tpl_no:{template_id}")]
    ])


# ─── Утилиты ─────────────────────────────────────────────────────────────────

async def send_long_message(message, text, **kwargs):
    if len(text) <= MAX_MESSAGE_LENGTH:
        return await message.reply(text, **kwargs)
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await message.reply(text[i:i + MAX_MESSAGE_LENGTH], **kwargs)


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


async def get_category_name_by_id(category_id) -> str | None:
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('SELECT name FROM categories WHERE id = ?', (category_id,))
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
                'SELECT id, name FROM categories WHERE user_id = ?', (user_id,))
            return [f"{r[0]}: {r[1]}" for r in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_categories: {e}")
        return []


async def get_category_names(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT name FROM categories WHERE user_id = ?', (user_id,))
            return [r[0] for r in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_names: {e}")
        return []


async def get_category_id(category_name, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id FROM categories WHERE name = ? AND user_id = ?',
                (category_name, user_id))
            row = await cursor.fetchone()
            return row[0] if row else None
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_id: {e}")
        return None


async def add_category(category_name, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'INSERT OR IGNORE INTO categories (name, user_id) VALUES (?, ?)',
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


# ─── Расходы ─────────────────────────────────────────────────────────────────

async def log_expense(user_id, category_id, name, price, quantity, total) -> int | None:
    """Записывает расход и возвращает его ID (для отмены)."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO expenses (user_id, category_id, name, price, quantity, total, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, category_id, name, price, quantity, total,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
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
                GROUP BY e.category_id
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
                GROUP BY e.name
            ''', (category_id, start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses_by_category: {e}")
        return []


async def get_all_expenses(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name, e.name, e.price, e.quantity, e.total, e.date
                FROM expenses e JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? ORDER BY e.date DESC
            ''', (user_id,))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_all_expenses: {e}")
        return []


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
    """Получает все шаблоны, у которых день совпадает с сегодняшним."""
    today = datetime.now(ALMATY_TZ).day
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT t.id, t.user_id, c.name, t.name, t.amount, t.category_id
                FROM recurring_templates t
                JOIN categories c ON t.category_id = c.id
                WHERE t.day_of_month = ?
            ''', (today,))
            return await cursor.fetchall()
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

async def format_expense_report(data, currency_symbol='₸'):
    report = ""
    total_expense = 0
    for category, total in data:
        report += f"{category}: {total:.2f} {currency_symbol}\n"
        total_expense += total
    report += f"\nОбщая сумма: {total_expense:.2f} {currency_symbol}"
    return report


async def create_pie_chart(data, user_id, currency_symbol='₸'):
    categories = [item[0] for item in data]
    totals = [item[1] for item in data]

    def func(pct, allvals):
        absolute = int(pct / 100. * sum(allvals))
        return f"{pct:.1f}%\n({absolute} {currency_symbol})"

    plt.figure(figsize=(10, 6))
    plt.pie(totals, labels=categories, autopct=lambda pct: func(pct, totals), startangle=140)
    plt.title('Расходы по категориям')
    chart_file = f'expenses_pie_chart_{user_id}.png'
    plt.savefig(chart_file)
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

        df = pd.DataFrame(expenses, columns=['category', 'expense', 'price', 'quantity', 'total', 'date'])
        bold_font = Font(bold=True)

        with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
            workbook = writer.book
            worksheet = workbook.create_sheet('Expenses Report')
            start_row, start_col, col_offset = 1, 1, 7

            for category, group in df.groupby('category'):
                total_sum = group['total'].sum()
                worksheet.cell(row=start_row, column=start_col, value=str(category))
                worksheet.cell(row=start_row, column=start_col + 4, value=f"{total_sum:.2f}")
                start_row += 2

                for i, header in enumerate(["Наименование", "Цена", "Количество", "Сумма", "Дата"]):
                    cell = worksheet.cell(row=start_row, column=start_col + i, value=header)
                    cell.font = bold_font

                start_row += 1
                for _, row in group.iterrows():
                    worksheet.cell(row=start_row, column=start_col, value=row['expense'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 1, value=row['price'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 2, value=row['quantity'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 3, value=row['total'])
                    date_val = datetime.strptime(row['date'], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
                    worksheet.cell(row=start_row, column=start_col + 4, value=date_val)
                    start_row += 1

                start_row = 1
                start_col += col_offset

        return file_name
    except Exception as e:
        logging.error(f"Ошибка generate_excel_report: {e}")
        return None


async def get_period_dates(period: str) -> tuple[str, str]:
    today = datetime.now()
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
        now = datetime.now(ALMATY_TZ)
        first_of_current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Прошлый месяц
        last_month_end = first_of_current - timedelta(seconds=1)
        last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Позапрошлый месяц
        prev_month_end = last_month_start - timedelta(seconds=1)
        prev_month_start = prev_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        fmt = "%Y-%m-%d %H:%M:%S"

        async with DatabaseConnection() as cursor:
            await cursor.execute('SELECT DISTINCT user_id FROM expenses')
            users = await cursor.fetchall()

        for (user_id,) in users:
            try:
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
                    total_change = ((total_last - total_prev) / total_prev) * 100
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
                logging.error(f"Ошибка напоминания шаблона {tpl_id} для {user_id}: {e}")
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
    await message.reply("Выберите период:", reply_markup=period_keyboard)
    await set_user_state(message.from_user.id, "choose_period", {})


async def handle_report_category(message):
    user_id = message.from_user.id
    categories = await get_category_names(user_id)
    if categories:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton(c)] for c in categories] + [[KeyboardButton("Назад")]],
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

    start_date, end_date = await get_period_dates(period)
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
    _, symbol = await get_user_currency(user_id)

    if category:
        expenses = await get_expenses_by_category(start_date, end_date, category, user_id)
        if expenses is None:
            await message.reply(f"Категория '{category}' не найдена.", reply_markup=main_keyboard)
            return
    else:
        expenses = await get_expenses(start_date, end_date, user_id)

    if not expenses:
        label = f" по категории '{category}'" if category else ""
        await message.reply(f"Нет данных{label} за период.", reply_markup=main_keyboard)
        return

    report = await format_expense_report(expenses, symbol)
    if category:
        report = f"Траты по категории '{category}':\n{report}"

    chart_file = excel_file = None
    try:
        chart_file = await create_pie_chart(expenses, user_id, symbol)
        excel_file = await generate_excel_report(start_date, end_date, user_id)
        await send_long_message(message, report, reply_markup=main_keyboard)
        if excel_file:
            await message.reply_document(excel_file, reply_markup=main_keyboard)
        await message.reply_photo(chart_file, reply_markup=main_keyboard)
    finally:
        safe_remove(chart_file, excel_file)


# ─── Обработчик ввода трат (свободный + точный формат) ────────────────────────

async def handle_expense_entry(message, text):
    user_id = message.from_user.id
    _, symbol = await get_user_currency(user_id)

    # --- Точный формат с запятыми ---
    if ',' in text:
        parts = [p.strip() for p in text.split(',')]

        if len(parts) == 4:
            try:
                category, name, price, quantity = parts
                price, quantity = float(price), float(quantity)
                total = price * quantity
                category_id = await get_category_id(category, user_id)
                if category_id is None:
                    await message.reply(f"Категория '{category}' не найдена. Добавьте сначала.")
                    return
                expense_id = await log_expense(user_id, category_id, name, price, quantity, total)
                await set_last_category_id(user_id, category_id)
                await message.reply(
                    f"✅ {category} — {name}: {total:.2f} {symbol}",
                    reply_markup=undo_keyboard(expense_id))
            except ValueError:
                await message.reply("Неверный формат. Используйте: Категория, Наименование, Цена, Количество")
            return

        if len(parts) == 2:
            try:
                category, total = parts[0], float(parts[1])
                category_id = await get_category_id(category, user_id)
                if category_id is None:
                    await message.reply(f"Категория '{category}' не найдена. Добавьте сначала.")
                    return
                expense_id = await log_expense(user_id, category_id, None, None, None, total)
                await set_last_category_id(user_id, category_id)
                await message.reply(
                    f"✅ {category}: {total:.2f} {symbol}",
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
        expense_id = await log_expense(
            user_id, category_id, parsed['name'],
            parsed['price'], parsed['quantity'], parsed['total'])
        await set_last_category_id(user_id, category_id)

        label = f"{parsed['category']} — {parsed['name']}" if parsed['name'] else parsed['category']
        await message.reply(
            f"✅ {label}: {parsed['total']:.2f} {symbol}",
            reply_markup=undo_keyboard(expense_id))
    else:
        # Категория не найдена — предлагаем последнюю
        last_cat_id = await get_last_category_id(user_id)
        if last_cat_id:
            last_cat_name = await get_category_name_by_id(last_cat_id)
            if last_cat_name:
                # Сохраняем данные в состоянии для обработки callback
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

        # Нет последней категории
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

    report = ""
    for category, name, price, quantity, total, date in expenses:
        date_short = date[:10] if date else "---"
        report += f"{date_short} | {category} | {name or '---'} | {total:.2f} {symbol}\n"

    await send_long_message(message, report, reply_markup=main_keyboard)


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

    if text == "/help":
        await message.reply(
            "📖 Как пользоваться:\n\n"
            "Свободный формат:\n"
            "  Продукты яблоки 100\n"
            "  Такси 500\n"
            "  Еда пицца 400р\n\n"
            "Точный формат:\n"
            "  Продукты, Яблоки, 100, 2\n"
            "  Такси, 500\n\n"
            "Если категория не указана — бот предложит последнюю использованную.\n\n"
            "Команды:\n"
            "/currency KZT — установить валюту (KZT, RUB, USD, EUR...)\n"
            "/help — эта справка\n\n"
            "Кнопки:\n"
            "Категории — управление категориями\n"
            "Шаблоны — повторяющиеся траты\n"
            "Отчет — отчёт за период\n"
            "Все траты — полный список",
            reply_markup=main_keyboard)
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
                "Ваши категории:\n" + "\n".join(categories) + "\n\nВведите id для удаления:",
                reply_markup=ForceReply())
            await set_user_state(user_id, "delete_category")
        else:
            await message.reply("Категорий пока нет.", reply_markup=category_submenu)
        return

    if text == "Отчет":
        await handle_report(message)
        return

    if text == "Отчет по категории":
        await handle_report_category(message)
        return

    if text == "Все траты":
        await handle_all_expenses(message)
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
                lines.append(f"{tpl_id}: {label}, {amount:.0f} {symbol}, {day}-го числа")
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
                lines.append(f"{tpl_id}: {label}, {amount:.0f} {symbol}, {day}-го числа")
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
            label = f"{text} — {pending['name']}" if pending.get('name') else text
            await message.reply(
                f"✅ {label}: {pending['total']:.2f} {symbol}",
                reply_markup=undo_keyboard(expense_id))
        else:
            await message.reply("Данные устарели, введите трату заново.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    # ── Ввод траты (свободный + точный формат) ──
    await handle_expense_entry(message, text)


# ─── Планировщик ─────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler(timezone=ALMATY_TZ)

# Ежемесячное сравнение — 1-го числа в 9:00
scheduler.add_job(send_monthly_comparisons, 'cron', day=1, hour=9, minute=0,
                  id='monthly_comparison', replace_existing=True)

# Напоминания по шаблонам — каждый день в 9:00
scheduler.add_job(send_template_reminders, 'cron', hour=9, minute=0,
                  id='template_reminders', replace_existing=True)


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def main():
    async with app:
        scheduler.start()
        logging.info("Бот запущен. Планировщик активен.")
        await idle()
        scheduler.shutdown()
        await DatabaseConnection.close()

if __name__ == "__main__":
    app.run(main())
