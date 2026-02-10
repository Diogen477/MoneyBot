from dotenv import load_dotenv
import os
import aiosqlite
from datetime import datetime, timedelta
import logging
import json
import pandas as pd
from openpyxl.styles import Font

from pyrogram.errors import MessageNotModified

from matplotlib import pyplot as plt
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton, ForceReply

from setup_db import DatabaseConnection

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Загружаем переменные из файла .env
load_dotenv()

api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('BOT_TOKEN')

if not all([api_id, api_hash, bot_token]):
    raise RuntimeError("Не заданы переменные окружения API_ID, API_HASH или BOT_TOKEN. Проверьте .env файл.")

app = Client(
    "expense_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

# Максимальная длина сообщения Telegram
MAX_MESSAGE_LENGTH = 4000

# ─── Клавиатуры ──────────────────────────────────────────────────────────────

main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Категории")],
        [KeyboardButton("Отчет"), KeyboardButton("Отчет по категории")],
        [KeyboardButton("Все траты")]
    ],
    resize_keyboard=True
)

category_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Мои категории"), KeyboardButton("Добавить категорию")],
        [KeyboardButton("Удалить категорию"), KeyboardButton("Назад")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

period_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("День"), KeyboardButton("Неделя")],
        [KeyboardButton("Месяц"), KeyboardButton("Квартал"), KeyboardButton("Год")],
        [KeyboardButton("Ввести даты вручную")],
        [KeyboardButton("Назад")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)


async def category_keyboard(user_id):
    """Клавиатура для выбора категории с кнопкой 'Назад'."""
    categories = await get_category_names(user_id)
    buttons = [[KeyboardButton(cat)] for cat in categories]
    buttons.append([KeyboardButton("Назад")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)


# ─── Утилиты ─────────────────────────────────────────────────────────────────

async def send_long_message(message, text, **kwargs):
    """Отправка длинных сообщений с разбивкой по лимиту Telegram."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        await message.reply(text, **kwargs)
        return
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await message.reply(text[i:i + MAX_MESSAGE_LENGTH], **kwargs)


def safe_remove(*paths):
    """Безопасное удаление временных файлов."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logging.warning(f"Не удалось удалить файл {path}: {e}")


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
                'SELECT state, data FROM user_states WHERE user_id = ?',
                (user_id,)
            )
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
            await cursor.execute(
                'DELETE FROM user_states WHERE user_id = ?',
                (user_id,)
            )
    except aiosqlite.Error as e:
        logging.error(f"Ошибка reset_user_state: {e}")


# ─── Работа с категориями ────────────────────────────────────────────────────

async def get_categories(user_id):
    """Возвращает список категорий в формате 'id: name'."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id, name FROM categories WHERE user_id = ?',
                (user_id,)
            )
            return [f"{row[0]}: {row[1]}" for row in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_categories: {e}")
        return []


async def get_category_names(user_id):
    """Возвращает список имён категорий."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT name FROM categories WHERE user_id = ?',
                (user_id,)
            )
            return [row[0] for row in await cursor.fetchall()]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_category_names: {e}")
        return []


async def get_category_id(category_name, user_id):
    """Возвращает ID категории по имени или None."""
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT id FROM categories WHERE name = ? AND user_id = ?',
                (category_name, user_id)
            )
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
                (category_name, user_id)
            )
    except aiosqlite.Error as e:
        logging.error(f"Ошибка add_category: {e}")


async def delete_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'DELETE FROM categories WHERE id = ? AND user_id = ?',
                (category_id, user_id)
            )
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_category: {e}")


async def has_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'SELECT COUNT(*) FROM expenses WHERE category_id = ? AND user_id = ?',
                (category_id, user_id)
            )
            result = await cursor.fetchone()
            return result[0] > 0
    except aiosqlite.Error as e:
        logging.error(f"Ошибка has_expenses_for_category: {e}")
        return False


async def delete_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute(
                'DELETE FROM expenses WHERE category_id = ? AND user_id = ?',
                (category_id, user_id)
            )
    except aiosqlite.Error as e:
        logging.error(f"Ошибка delete_expenses_for_category: {e}")


# ─── Работа с расходами ──────────────────────────────────────────────────────

async def log_expense(user_id, category_id, name, price, quantity, total):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO expenses (user_id, category_id, name, price, quantity, total, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, category_id, name, price, quantity, total,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка log_expense: {e}")


async def get_expenses(start_date, end_date, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name, SUM(e.total) as total
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.date BETWEEN ? AND ? AND e.user_id = ?
                GROUP BY e.category_id
            ''', (start_date, end_date, user_id))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_expenses: {e}")
        return []


async def get_expenses_by_category(start_date, end_date, category_name, user_id):
    """Получает расходы по категории. Возвращает список или None если категория не найдена."""
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
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ?
                ORDER BY e.date DESC
            ''', (user_id,))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка get_all_expenses: {e}")
        return []


# ─── Отчёты ──────────────────────────────────────────────────────────────────

async def format_expense_report(data):
    report = ""
    total_expense = 0
    for category, total in data:
        report += f"{category}: {total:.2f}\n"
        total_expense += total
    report += f"\nОбщая сумма: {total_expense:.2f}"
    return report


async def create_pie_chart(data, user_id):
    categories = [item[0] for item in data]
    totals = [item[1] for item in data]

    def func(pct, allvals):
        absolute = int(pct / 100. * sum(allvals))
        return f"{pct:.1f}%\n({absolute})"

    plt.figure(figsize=(10, 6))
    plt.pie(totals, labels=categories, autopct=lambda pct: func(pct, totals), startangle=140)
    plt.title('Расходы по категориям')
    chart_file = f'expenses_pie_chart_{user_id}.png'
    plt.savefig(chart_file)
    plt.close()
    return chart_file


async def generate_excel_report(start_date: str, end_date: str, user_id: int) -> str | None:
    try:
        safe_start = start_date.replace(':', '-')
        safe_end = end_date.replace(':', '-')
        file_name = f'expenses_report_{user_id}_{safe_start}_to_{safe_end}.xlsx'

        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name as category_name, e.name as expense_name,
                       e.price, e.quantity, e.total, e.date
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? AND e.date BETWEEN ? AND ?
                ORDER BY c.name
            ''', (user_id, start_date, end_date))
            expenses = await cursor.fetchall()

        if not expenses:
            return None

        df = pd.DataFrame(expenses, columns=['category', 'expense', 'price', 'quantity', 'total', 'date'])

        with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
            workbook = writer.book
            worksheet = workbook.create_sheet('Expenses Report')
            bold_font = Font(bold=True)

            start_row = 1
            start_col = 1
            col_offset = 7

            for category, group in df.groupby('category'):
                total_sum = group['total'].sum()
                worksheet.cell(row=start_row, column=start_col, value=str(category))
                worksheet.cell(row=start_row, column=start_col + 4, value=f"{total_sum:.2f}")

                start_row += 2
                headers = ["Наименование", "Цена", "Количество", "Сумма", "Дата"]
                for i, header in enumerate(headers):
                    cell = worksheet.cell(row=start_row, column=start_col + i, value=header)
                    cell.font = bold_font

                start_row += 1
                for _, row in group.iterrows():
                    worksheet.cell(row=start_row, column=start_col, value=row['expense'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 1, value=row['price'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 2, value=row['quantity'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 3, value=row['total'])
                    date_value = datetime.strptime(row['date'], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
                    worksheet.cell(row=start_row, column=start_col + 4, value=date_value)
                    start_row += 1

                start_row = 1
                start_col += col_offset

        return file_name

    except Exception as e:
        logging.error(f"Ошибка generate_excel_report: {e}")
        return None


async def get_period_dates(period: str) -> tuple[str, str]:
    today = datetime.now()
    periods = {
        "День": 0,
        "Неделя": 7,
        "Месяц": 30,
        "Квартал": 90,
        "Год": 365,
    }
    days = periods.get(period)
    if days is not None:
        if days == 0:
            start_date = today.strftime("%Y-%m-%d 00:00:00")
        else:
            start_date = (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        end_date = today.strftime("%Y-%m-%d %H:%M:%S")
        return start_date, end_date
    return "", ""


# ─── Шаблонное сообщение ─────────────────────────────────────────────────────

async def send_pinned_template_message(client, message):
    user_id = message.from_user.id
    template_message = (
        "Добавьте трату в формате:\n"
        "Категория, Наименование, Цена, Количество\n\n"
        "Пример: Продукты, Яблоки, 100, 2\n"
        "Или: Такси, 500"
    )
    try:
        sent = await client.send_message(user_id, template_message)
        await client.pin_chat_message(user_id, sent.id)
        await message.reply("Шаблон для добавления трат отправлен и закреплен.")
    except MessageNotModified:
        await message.reply("Сообщение уже закреплено.")
    except Exception as e:
        logging.error(f"Ошибка при закреплении сообщения: {e}")
        await message.reply("Не удалось закрепить сообщение.")


# ─── Обработчики ──────────────────────────────────────────────────────────────

async def handle_delete_category(message, category_id):
    user_id = message.from_user.id
    if await has_expenses_for_category(category_id, user_id):
        confirm_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Да"), KeyboardButton("Нет")]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await set_user_state(user_id, "confirm_delete", {"category_id": category_id})
        await message.reply(
            "По данной категории есть траты. Вы уверены, что хотите удалить её?",
            reply_markup=confirm_kb
        )
    else:
        await delete_category(category_id, user_id)
        await message.reply("Категория удалена.", reply_markup=main_keyboard)
        await reset_user_state(user_id)


async def handle_report(message):
    user_id = message.from_user.id
    await message.reply("Выберите период:", reply_markup=period_keyboard)
    await set_user_state(user_id, "choose_period", {})


async def handle_report_category(message):
    user_id = message.from_user.id
    categories = await get_category_names(user_id)
    if categories:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton(cat)] for cat in categories] + [[KeyboardButton("Назад")]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await message.reply("Выберите категорию:", reply_markup=kb)
        await set_user_state(user_id, "choose_category")
    else:
        await message.reply("Категорий пока нет.", reply_markup=main_keyboard)


async def handle_choose_category(message, text):
    user_id = message.from_user.id
    if text == "Назад":
        await message.reply("Возвращаемся в главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    category_names = await get_category_names(user_id)
    if text in category_names:
        await set_user_state(user_id, "choose_period", {"category": text})
        await message.reply(
            f"Вы выбрали категорию: {text}. Теперь выберите период:",
            reply_markup=period_keyboard
        )
    else:
        await message.reply(
            "Неверная категория. Попробуйте снова.",
            reply_markup=await category_keyboard(user_id)
        )


async def handle_period_report(message, period, data):
    """Обработка выбора периода для отчёта (общего и по категории)."""
    user_id = message.from_user.id

    if period == "Назад":
        await message.reply("Возвращаемся в главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    if period == "Ввести даты вручную":
        await set_user_state(user_id, "manual_dates", data or {})
        await message.reply(
            "Введите даты в формате: YYYY-MM-DD YYYY-MM-DD",
            reply_markup=ForceReply()
        )
        return

    if period not in ("День", "Неделя", "Месяц", "Квартал", "Год"):
        await message.reply("Неверный период. Попробуйте снова.", reply_markup=period_keyboard)
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
            raise ValueError("Нужно две даты")

        start_date = parts[0] + " 00:00:00"
        end_date = parts[1] + " 23:59:59"

        # Валидация формата дат
        datetime.strptime(parts[0], "%Y-%m-%d")
        datetime.strptime(parts[1], "%Y-%m-%d")

        category = data.get("category") if data else None
        await _send_report(message, start_date, end_date, user_id, category)
    except ValueError:
        await message.reply(
            "Неверный формат дат. Используйте: YYYY-MM-DD YYYY-MM-DD",
            reply_markup=main_keyboard
        )
    await reset_user_state(user_id)


async def _send_report(message, start_date, end_date, user_id, category=None):
    """Общая логика формирования и отправки отчёта."""
    if category:
        expenses = await get_expenses_by_category(start_date, end_date, category, user_id)
        if expenses is None:
            await message.reply(
                f"Категория '{category}' не найдена. Добавьте категорию сначала.",
                reply_markup=main_keyboard
            )
            return
    else:
        expenses = await get_expenses(start_date, end_date, user_id)

    if not expenses:
        period_label = f" по категории '{category}'" if category else ""
        await message.reply(
            f"Нет данных{period_label} за выбранный период.",
            reply_markup=main_keyboard
        )
        return

    report = await format_expense_report(expenses)
    if category:
        report = f"Траты по категории '{category}':\n{report}"

    chart_file = None
    excel_file = None

    try:
        chart_file = await create_pie_chart(expenses, user_id)
        excel_file = await generate_excel_report(start_date, end_date, user_id)

        await send_long_message(message, report, reply_markup=main_keyboard)
        if excel_file:
            await message.reply_document(excel_file, reply_markup=main_keyboard)
        await message.reply_photo(chart_file, reply_markup=main_keyboard)
    finally:
        safe_remove(chart_file, excel_file)


async def handle_expense_entry(message, text):
    parts = text.split(',')
    user_id = message.from_user.id

    if len(parts) == 4:
        try:
            category, name, price, quantity = [p.strip() for p in parts]
            price = float(price)
            quantity = float(quantity)
            total = price * quantity
            category_id = await get_category_id(category, user_id)
            if category_id is None:
                await message.reply(f"Категория '{category}' не найдена. Добавьте категорию сначала.")
            else:
                await log_expense(user_id, category_id, name, price, quantity, total)
                await message.reply(f"✅ {category} — {name}: {total:.2f}", reply_markup=main_keyboard)
        except ValueError:
            await message.reply(
                "Неверный формат данных. Используйте: Категория, Наименование, Цена, Количество",
                reply_markup=main_keyboard
            )

    elif len(parts) == 2:
        try:
            category, total = [p.strip() for p in parts]
            total = float(total)
            category_id = await get_category_id(category, user_id)
            if category_id is None:
                await message.reply(f"Категория '{category}' не найдена. Добавьте категорию сначала.")
            else:
                await log_expense(user_id, category_id, None, None, None, total)
                await message.reply(f"✅ {category}: {total:.2f}", reply_markup=main_keyboard)
        except ValueError:
            await message.reply(  # Исправлен отсутствующий await
                "Неверный формат данных. Используйте: Категория, Сумма",
                reply_markup=main_keyboard
            )
    else:
        await message.reply(
            "Неверный формат. Используйте:\n"
            "Категория, Наименование, Цена, Количество\n"
            "или: Категория, Сумма",
            reply_markup=main_keyboard
        )


async def handle_all_expenses(message):
    user_id = message.from_user.id
    expenses = await get_all_expenses(user_id)
    if not expenses:
        await message.reply("Нет записей о тратах.", reply_markup=main_keyboard)
        return

    report = ""
    for category, name, price, quantity, total, date in expenses:
        date_short = date[:10] if date else "---"
        report += (
            f"{date_short} | {category} | "
            f"{name or '---'} | {total:.2f}\n"
        )

    await send_long_message(message, report, reply_markup=main_keyboard)


# ─── Главный обработчик сообщений ────────────────────────────────────────────

@app.on_message(filters.text & filters.private)
async def handle_message(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    logging.info(f"[{user_id}] Сообщение: {text}")

    state, data = await get_user_state(user_id)

    # ── Меню-команды (приоритет над состояниями) ──

    if text == "/start":
        await message.reply("Привет! Я помогу тебе следить за твоими расходами. Добавляй траты по шаблону.")
        await send_pinned_template_message(client, message)
        await reset_user_state(user_id)
        return

    if text == "Назад":
        await message.reply("Возвращаемся в главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    if text == "Категории":
        await message.reply("Выберите действие:", reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    if text == "Мои категории":
        categories = await get_categories(user_id)
        reply = "\n".join(categories) if categories else "Категорий пока нет."
        await message.reply(reply, reply_markup=category_submenu)
        return

    if text == "Добавить категорию":
        await message.reply("Введите название категории:", reply_markup=ForceReply())
        await set_user_state(user_id, "adding_category")
        return

    if text == "Удалить категорию":
        categories = await get_categories(user_id)
        if categories:
            await message.reply(
                "Ваши категории:\n" + "\n".join(categories) +
                "\n\nВведите id категории для удаления:",
                reply_markup=ForceReply()
            )
        else:
            await message.reply("Категорий пока нет.", reply_markup=category_submenu)
            return
        await set_user_state(user_id, "delete_category")
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

    # ── Обработка состояний ──

    if state == "adding_category":
        category_name = text.strip()
        if category_name:
            await add_category(category_name, user_id)
            await message.reply(f"Категория '{category_name}' добавлена.", reply_markup=category_submenu)
        else:
            await message.reply("Название категории не должно быть пустым.", reply_markup=category_submenu)
        await reset_user_state(user_id)
        return

    if state == "delete_category":
        try:
            category_id = int(text)
            await handle_delete_category(message, category_id)
        except ValueError:
            await message.reply(
                "Неверный формат id. Введите число.",
                reply_markup=category_submenu
            )
        return

    if state == "confirm_delete":
        category_id = data.get("category_id")
        if text == "Да":
            await delete_expenses_for_category(category_id, user_id)
            await delete_category(category_id, user_id)
            await message.reply("Категория и связанные траты удалены.", reply_markup=category_submenu)
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

    # ── Ввод траты по умолчанию ──
    await handle_expense_entry(message, text)


if __name__ == "__main__":
    app.run()
