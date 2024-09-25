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

# Импортируем функцию для инициализации базы данных из db_setup.py
from setup_db import DatabaseConnection

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Загружаем переменные из файла .env
load_dotenv()

# Получаем переменные окружения
api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
bot_token = os.getenv('BOT_TOKEN')

# Инициализируем клиента Pyrogram с переменными окружения
app = Client(
    "expense_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

# Клавиатура для команд
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Категории")],
        [KeyboardButton("Отчет"), KeyboardButton("Отчет по категории")],
        [KeyboardButton("Все траты")]
    ],
    resize_keyboard=True
)

# Подменю для "Категории"
category_submenu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Мои категории"), KeyboardButton("Добавить категорию")],
        [KeyboardButton("Удалить категорию"), KeyboardButton("Назад")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# Функция для отправки шаблонного сообщения и закрепления
async def send_pinned_template_message(client, message):
    user_id = message.from_user.id

    # Шаблонное сообщение для добавления трат
    template_message = """
Добавьте трату в формате:
Категория, Наименование, Цена, Количество

Пример: Продукты, Яблоки, 100, 2
Или: Такси, 500
    """
    try:
        # Отправляем сообщение
        sent_message = await client.send_message(user_id, template_message)

        # Закрепляем сообщение
        await client.pin_chat_message(user_id, sent_message.id)

        await message.reply("Шаблон для добавления трат отправлен и закреплен.")
    except MessageNotModified:
        await message.reply("Сообщение уже закреплено.")
    except Exception as e:
        logging.error(f"Ошибка при закреплении сообщения: {e}")
        await message.reply("Не удалось закрепить сообщение.")

# Клавиатура для выбора категории с добавлением кнопки "Назад"
async def category_keyboard(user_id):
    categories = await get_category_names(user_id)  # Получаем только имена категорий
    buttons = [[KeyboardButton(category)] for category in categories]
    buttons.append([KeyboardButton("Назад")])  # Добавляем кнопку "Назад"
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)

# Клавиатура для выбора периода
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

# Функция для установки состояния пользователя
async def set_user_state(user_id, state, data=None):
    try:
        async with DatabaseConnection() as cursor:
            if data is None:
                data = {}
            data_json = json.dumps(data)
            await cursor.execute('''
                INSERT OR REPLACE INTO user_states (user_id, state, data)
                VALUES (?, ?, ?)
            ''', (user_id, state, data_json))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")

# Функция для получения состояния пользователя
async def get_user_state(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT state, data
                FROM user_states
                WHERE user_id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
            if row:
                state, data = row
                data = json.loads(data) if data else {}
            else:
                state, data = None, {}
        return state, data
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return None, {}

# Сброс состояния пользователя
async def reset_user_state(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                DELETE FROM user_states
                WHERE user_id = ?
            ''', (user_id,))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")

# Функция для получения списка категорий пользователя в формате id: name
async def get_categories(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT id, name FROM categories WHERE user_id = ?
            ''', (user_id,))
            categories = await cursor.fetchall()
        return [f"{row[0]}: {row[1]}" for row in categories]  # Возвращаем список в формате id: name
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return []

# Функция для добавления категории
async def add_category(category_name, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT OR IGNORE INTO categories (name, user_id)
                VALUES (?, ?)
            ''', (category_name, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")

# Функция для удаления категории
async def delete_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                DELETE FROM categories WHERE id = ? AND user_id = ?
            ''', (category_id, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")

# Проверка наличия расходов по категории
async def has_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT COUNT(*) FROM expenses WHERE category_id = ? AND user_id = ?
            ''', (category_id, user_id))
            result = await cursor.fetchone()
        return result[0] > 0
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return False

# Удаление всех записей по категории
async def delete_expenses_for_category(category_id, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                DELETE FROM expenses WHERE category_id = ? AND user_id = ?
            ''', (category_id, user_id))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")

# Обработка выбора категории для удаления
async def handle_delete_category(message, category_id):
    user_id = message.from_user.id
    if await has_expenses_for_category(category_id, user_id):
        confirm_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("Да"), KeyboardButton("Нет")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await set_user_state(user_id, "confirm_delete", {"category_id": category_id})
        await message.reply("По данной категории есть траты. Вы уверены, что хотите удалить её?", reply_markup=confirm_keyboard)
    else:
        await delete_category(category_id, user_id)
        await message.reply("Категория удалена.", reply_markup=main_keyboard)
        await reset_user_state(user_id)

# Функция для получения только имен категорий пользователя
async def get_category_names(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT name FROM categories WHERE user_id = ?
            ''', (user_id,))
            categories = await cursor.fetchall()
        return [row[0] for row in categories]
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return []

# Получение идентификатора категории пользователя
# Получение идентификатора категории пользователя
async def get_category_id(category_name, user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT id FROM categories WHERE name = ? AND user_id = ?
            ''', (category_name, user_id))
            row = await cursor.fetchone()
        if row:
            return row[0]
        else:
            logging.info(f"Категория '{category_name}' не найдена для пользователя {user_id}.")
            return None
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return None

# Функция для записи расхода
async def log_expense(user_id, category_id, name, price, quantity, total):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                INSERT INTO expenses (user_id, category_id, name, price, quantity, total, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, category_id, name, price, quantity, total, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")


# Функция для получения расходов за период для пользователя
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
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return []

async def get_expenses_by_category(start_date, end_date, category_name, message):
    user_id = message.from_user.id
    try:
        category_id = await get_category_id(category_name, user_id)
        if category_id is None:
            await message.reply(f"Категория '{category_name}' не найдена. Добавьте категорию сначала.")
        else:
            async with DatabaseConnection() as cursor:
                await cursor.execute('''
                    SELECT e.name, SUM(e.total) as total
                    FROM expenses e
                    WHERE e.category_id = ? AND e.date BETWEEN ? AND ? AND e.user_id = ?
                    GROUP BY e.name
                ''', (category_id, start_date, end_date, user_id))
                return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return []

# Форматирование отчета
async def format_expense_report(data):
    report = ""
    total_expense = 0
    for category, total in data:
        report += f"{category}: {total:.2f}\n"
        total_expense += total
    report += f"\nОбщая сумма: {total_expense:.2f}"
    return report

# Генерация отчета в Excel (добавьте pandas)

async def generate_excel_report(start_date: str, end_date: str, user_id: int) -> str:
    try:
        # Заменяем двоеточия на тире, чтобы избежать недопустимых символов в имени файла
        safe_start_date = start_date.replace(':', '-')
        safe_end_date = end_date.replace(':', '-')

        # Имя файла с безопасными символами
        file_name = f'expenses_report_{user_id}_{safe_start_date}_to_{safe_end_date}.xlsx'

        async with DatabaseConnection() as cursor:
            # Получаем расходы по каждой категории за указанный период для пользователя
            await cursor.execute('''
                SELECT c.name as category_name, e.name as expense_name, e.price, e.quantity, e.total, e.date
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ? AND e.date BETWEEN ? AND ?
                ORDER BY c.name
            ''', (user_id, start_date, end_date))

            expenses = await cursor.fetchall()

        if not expenses:
            logging.info(f"Нет данных для отчета за период {start_date} - {end_date}")
            return None

        # Создаем DataFrame для удобного форматирования данных
        df = pd.DataFrame(expenses, columns=['category', 'expense', 'price', 'quantity', 'total', 'date'])

        # Создаем Excel writer
        with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
            # Создаем новый Excel файл
            workbook = writer.book
            worksheet = workbook.create_sheet('Expenses Report')

            # Переменные для отслеживания положения в Excel
            start_row = 1  # Начальная строка
            start_col = 1  # Начальный столбец
            col_offset = 7  # Отступ на два столбца для каждой новой категории

            # Жирный шрифт для заголовков
            bold_font = Font(bold=True)

            # Проходим по каждой категории и группируем данные
            grouped = df.groupby('category')
            for category, group in grouped:
                # Общая сумма по категории
                total_sum = group['total'].sum()

                # Записываем название категории и общую сумму в верхней строке с отступом
                worksheet.cell(row=start_row, column=start_col, value=f"{category}")
                worksheet.cell(row=start_row, column=start_col + 4, value=f"{total_sum:.2f}")

                # Переходим на следующую строку для записи детализации по категории
                start_row += 2

                # Записываем заголовки для колонок и делаем их жирными
                headers = ["Наименование", "Цена", "Количество", "Сумма", "Дата"]
                for i, header in enumerate(headers):
                    cell = worksheet.cell(row=start_row, column=start_col + i, value=header)
                    cell.font = bold_font  # Устанавливаем жирный шрифт

                # Записываем детализацию по каждой категории
                start_row += 1
                for _, row in group.iterrows():
                    worksheet.cell(row=start_row, column=start_col, value=row['expense'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 1, value=row['price'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 2, value=row['quantity'] or '---')
                    worksheet.cell(row=start_row, column=start_col + 3, value=row['total'])

                    # Форматируем дату без времени
                    date_value = datetime.strptime(row['date'], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
                    worksheet.cell(row=start_row, column=start_col + 4, value=date_value)

                    start_row += 1

                # После записи одной категории, начинаем следующую категорию с отступом
                start_row = 1  # Возвращаемся к началу строки
                start_col += col_offset  # Двигаемся на несколько столбцов вправо для следующей категории

        return file_name

    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return None
    except Exception as e:
        logging.error(f"Ошибка при создании Excel-отчета: {e}")
        return None

# Создание круговой диаграммы с отображением процентов и общей суммы по категории
async def create_pie_chart(data, user_id):
    # Здесь создается изображение с круговой диаграммой
    categories = [item[0] for item in data]
    totals = [item[1] for item in data]

    # Функция для отображения процентов и суммы
    def func(pct, allvals):
        absolute = int(pct/100.*sum(allvals))  # Рассчитываем абсолютное значение
        return f"{pct:.1f}%\n({absolute} руб.)"  # Отображаем процент и сумму

    plt.figure(figsize=(10, 6))
    plt.pie(totals, labels=categories, autopct=lambda pct: func(pct, totals), startangle=140)
    plt.title(f'Expenses by Category for user {user_id}')
    chart_file_name = f'expenses_pie_chart_{user_id}.png'
    plt.savefig(chart_file_name)
    plt.close()

    return chart_file_name

# Получение периода в формате дат
async def get_period_dates(period: str) -> tuple[str, str]:
    today = datetime.now()
    if period == "День":
        # Начало дня (полночь) и текущее время
        start_date: str = today.strftime("%Y-%m-%d 00:00:00")
        end_date: str = today.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "Неделя":
        start_date: str = (today - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        end_date: str = today.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "Месяц":
        start_date: str = (today - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        end_date: str = today.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "Квартал":
        start_date: str = (today - timedelta(days=90)).strftime("%Y-%m-%d 00:00:00")
        end_date: str = today.strftime("%Y-%m-%d %H:%M:%S")
    elif period == "Год":
        start_date: str = (today - timedelta(days=365)).strftime("%Y-%m-%d 00:00:00")
        end_date: str = today.strftime("%Y-%m-%d %H:%M:%S")
    else:
        start_date = end_date = None
    return start_date, end_date

# Функция для получения всех расходов пользователя
async def get_all_expenses(user_id):
    try:
        async with DatabaseConnection() as cursor:
            await cursor.execute('''
                SELECT c.name, e.name, e.price, e.quantity, e.total, e.date
                FROM expenses e
                JOIN categories c ON e.category_id = c.id
                WHERE e.user_id = ?
            ''', (user_id,))
            return await cursor.fetchall()
    except aiosqlite.Error as e:
        logging.error(f"Ошибка при работе с базой данных: {e}")
        return []

# Функция для обработки выбора периода
async def handle_period_report(message, period, category=None):
    # Получаем даты начала и конца периода
    start_date, end_date = await get_period_dates(period)

    # Проверяем, выбрал ли пользователь категорию
    if category:
        # Получаем данные расходов по выбранной категории за указанный период
        data = await get_expenses_by_category(start_date, end_date, category, message)
    else:
        # Получаем данные по всем категориям для пользователя
        data = await get_expenses(start_date, end_date, message.from_user.id)

    if data:
        # Формируем текстовый отчет
        report = await format_expense_report(data)

        # Генерация круговой диаграммы
        chart_file_name = await create_pie_chart(data, message.from_user.id)

        # Генерация Excel-отчета
        file_name = await generate_excel_report(start_date, end_date, message.from_user.id)

        # Отправляем отчет пользователю
        await message.reply(report, reply_markup=main_keyboard)
        await message.reply_document(file_name, reply_markup=main_keyboard)
        await message.reply_photo(chart_file_name, reply_markup=main_keyboard)
    else:
        # Если данных нет, отправляем соответствующее сообщение
        await message.reply("Нет данных за выбранный период.", reply_markup=main_keyboard)

    # Сбрасываем состояние пользователя после обработки
    await reset_user_state(message.from_user.id)

# Обработка команды "Отчет по категории"
async def handle_report_category(message):
    user_id = message.from_user.id
    categories = await get_category_names(user_id)
    if categories:
        category_keyboard_markup = ReplyKeyboardMarkup(
            [[KeyboardButton(category)] for category in categories] + [[KeyboardButton("Назад")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.reply("Выберите категорию:", reply_markup=category_keyboard_markup)
        await set_user_state(user_id, "choose_category")
    else:
        await message.reply("Категорий пока нет.", reply_markup=main_keyboard)

# Функция для обработки выбора категории
async def handle_choose_category(message, text):
    user_id = message.from_user.id
    if text == "Назад":
        await message.reply("Возвращаемся в главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    category_names = await get_category_names(user_id)

    if text in category_names:
        await set_user_state(user_id, "choose_period", text)
        await message.reply(f"Вы выбрали категорию: {text}. Теперь выберите период:", reply_markup=period_keyboard)
    else:
        await message.reply("Неверная категория. Попробуйте снова.", reply_markup= await category_keyboard(user_id))

# Функция для обработки выбора периода
async def handle_choose_period(message, period, data):
    user_id = message.from_user.id
    if period == "Назад":
        # Возвращаемся к выбору категории
        await message.reply("Возвращаемся к выбору категории.", reply_markup= await category_keyboard(user_id))
        await set_user_state(user_id, "choose_category")
        return

    if data is None:
        await message.reply("Произошла ошибка. Попробуйте снова.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    category = data.get("category")
    if period in ["День", "Неделя", "Месяц", "Квартал", "Год"]:
        start_date, end_date = await get_period_dates(period)

        if category:
            expenses = await get_expenses_by_category(start_date, end_date, category, message)
            if expenses == "Категория не найдена":
                await message.reply(f"Категория '{category}' не найдена. Попробуйте снова.", reply_markup=main_keyboard)
                await reset_user_state(user_id)
                return
        else:
            expenses = await get_expenses(start_date, end_date, user_id)

        if expenses and expenses != "Категория не найдена":
            report = f"Траты по категории '{category}' с {start_date} по {end_date}:\n"
            total = 0
            for name, expense in expenses:
                report += f"{name}: {expense:.2f}\n"
                total += expense
            report += f"\nОбщая сумма по категории '{category}': {total:.2f}"
            await message.reply(report, reply_markup=main_keyboard)
        else:
            await message.reply(f"Нет данных по категории '{category}' за выбранный период.", reply_markup=main_keyboard)

        await reset_user_state(user_id)
    else:
        await message.reply("Неверный период. Попробуйте снова.", reply_markup=period_keyboard)

    # Сбрасываем состояние после завершения обработки
    await reset_user_state(message.from_user.id)

# Функция для обработки ручного ввода дат для категории
async def handle_manual_dates(message, text, data):
    user_id = message.from_user.id
    if data is None:
        await message.reply("Произошла ошибка. Попробуйте снова.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
        return

    try:
        category = data.get("category")
        start_date, end_date = text.split()
        data = await get_expenses_by_category(start_date, end_date, category, message)
        if data:
            report = f"Траты по категории '{category}' с {start_date} по {end_date}:\n"
            total = 0
            for name, expense in data:
                report += f"{name}: {expense:.2f}\n"
                total += expense
            report += f"\nОбщая сумма по категории '{category}': {total:.2f}"
            await message.reply(report, reply_markup=main_keyboard)
        else:
            await message.reply(f"Нет данных по категории '{category}' за выбранный период.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
    except ValueError:
        await message.reply("Неверный формат дат. Используйте: YYYY-MM-DD YYYY-MM-DD", reply_markup=main_keyboard)

    # Сбрасываем состояние после завершения обработки
    await reset_user_state(user_id)

# Обработка команды "Мои категории" с выводом id: name
async def handle_my_categories(message):
    user_id = message.from_user.id
    categories = await get_categories(user_id)
    if categories:
        await message.reply("\n".join(categories), reply_markup=category_submenu)
    else:
        await message.reply("Категорий пока нет.", reply_markup=category_submenu)

    await reset_user_state(user_id)

async def handle_report(message):
    user_id = message.from_user.id
    await message.reply("Выберите период:", reply_markup=period_keyboard)
    await set_user_state(user_id, "choose_period", {})

async def handle_add_category(message, category_name):
    user_id = message.from_user.id
    category_name = category_name.strip()
    if category_name:
        await add_category(category_name, user_id)
        await message.reply(f"Категория '{category_name}' добавлена.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
    else:
        await message.reply("Название категории не должно быть пустым.", reply_markup=main_keyboard)
    # Сбрасываем состояние после завершения обработки
    await reset_user_state(message.from_user.id)

async def handle_expense_entry(message, text):
    parts = text.split(',')
    user_id = message.from_user.id
    if len(parts) == 4:  # Пример: Продукты, Сосиски, 400, 2
        try:
            category, name, price, quantity = parts
            price = float(price.strip())
            quantity = float(quantity.strip())
            total = price * quantity
            category_id = await get_category_id(category.strip(), user_id)
            if category_id is None:
                await message.reply(f"Категория '{category.strip()}' не найдена. Добавьте категорию сначала.")
            else:
                await log_expense(user_id, category_id, name.strip(), price, quantity, total)
                await message.reply("Запись добавлена.", reply_markup=main_keyboard)
        except ValueError:
            await message.reply("Неверный формат данных. Используйте: Категория, Наименование, Цена, Количество",
                          reply_markup=main_keyboard)

    elif len(parts) == 2:  # Пример: Такси, 2500
        try:
            category, total = parts
            total = float(total.strip())
            category_id = await get_category_id(category.strip(), user_id)
            if category_id is None:
                await message.reply(f"Категория '{category.strip()}' не найдена. Добавьте категорию сначала.")
            else:
                await log_expense(user_id, category_id, None, None, None, total)
                await message.reply("Запись добавлена.", reply_markup=main_keyboard)
        except ValueError:
            message.reply("Неверный формат данных. Используйте: Категория, Сумма", reply_markup=main_keyboard)

    else:
        await message.reply(
            "Неверный формат сообщения. Используйте: Категория, Наименование, Цена, Количество или Категория, Сумма.",
            reply_markup=main_keyboard)

async def handle_all_expenses(message):
    user_id = message.from_user.id
    expenses = await get_all_expenses(user_id)
    if expenses:
        report = ""
        for category, name, price, quantity, total, date in expenses:
            report += f"Категория: {category}, Наименование: {name or '---'}, Цена: {price or '---'}, Количество: {quantity or '---'}, Сумма: {total}, Дата: {date}\n"
        await message.reply(report, reply_markup=main_keyboard)
    else:
        await message.reply("Нет записей о тратах.", reply_markup=main_keyboard)

# Обработка текстовых сообщений
@app.on_message(filters.text)
async def handle_message(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    logging.info(f"Получено сообщение от пользователя {user_id}: {text}")

    state, data = await get_user_state(user_id)

    logging.info(f"Состояние пользователя: {state}, Данные: {data}")

    logging.info(f"Полученное сообщение: {text}")
    logging.info(f"Состояние пользователя: {state}")
    logging.info(f"Данные пользователя: {data}")

    if text == "Категории":
        await message.reply("Выберите действие:", reply_markup=category_submenu)
    elif text == "Мои категории":
        categories = await get_categories(user_id)
        if categories:
            await message.reply("\n".join(categories), reply_markup=category_submenu)
        else:
            await message.reply("Категорий пока нет.", reply_markup=category_submenu)
    elif text == "Добавить категорию":
        await message.reply("Введите название категории:", reply_markup=ForceReply())
        await set_user_state(user_id, "adding_category")
    elif text == "Удалить категорию":
        await message.reply("Введите id категории, которую вы хотите удалить:", reply_markup=ForceReply())
        await set_user_state(user_id, "delete_category")
    elif text == "Назад":
        await message.reply("Возвращаемся в главное меню.", reply_markup=main_keyboard)
        await reset_user_state(user_id)
    elif text == "/start":
        # Приветственное сообщение
        await message.reply(f"Привет! Я помогу тебе следить за твоими расходами. Добавляй траты по шаблону.")

        # Отправляем и закрепляем шаблонное сообщение
        await send_pinned_template_message(client, message)
    elif state == "adding_category":
        await add_category(text, user_id)
        await message.reply(f"Категория '{text}' добавлена.", reply_markup=category_submenu)
        await reset_user_state(user_id)
    elif state == "delete_category":
        try:
            category_id = int(text)
            await handle_delete_category(message, category_id)
        except ValueError:
            await message.reply("Неверный формат id. Введите корректный id категории.", reply_markup=category_submenu)
    elif state == "confirm_delete":
        category_id = data.get("category_id")
        if text == "Да":
            await delete_expenses_for_category(category_id, user_id)
            await delete_category(category_id, user_id)
            await message.reply("Категория и все связанные с ней траты удалены.", reply_markup=category_submenu)
        elif text == "Нет":
            await message.reply("Удаление отменено.", reply_markup=category_submenu)
        await reset_user_state(user_id)
    elif text == "Отчет":
        await handle_report(message)
    elif text == "Отчет по категории":
        await handle_report_category(message)
    elif text == "Все траты":  # Обрабатываем команду "Все траты"
        await handle_all_expenses(message)
    elif state == "choose_category":
        await handle_choose_category(message, text)
    elif state == "choose_period":
        await handle_period_report(message, text, data)  # Передаем данные о категории, если они есть
    elif state == "adding_category":
        await handle_add_category(message, text)
    else:
        await handle_expense_entry(message, text)

if __name__ == "__main__":
    app.run()  # Запуск бота через run()






