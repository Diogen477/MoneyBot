# MoneyBot — Telegram-бот для учёта расходов

Персональный Telegram-бот для ведения учёта трат с поддержкой категорий, отчётов, повторяющихся платежей и свободного формата ввода.

## Возможности

- **Свободный ввод трат** — пиши как удобно: `Продукты яблоки 100`, `такси 500`, `Еда 1200р`
- **Точный формат** — `Продукты, Яблоки, 100, 2` (категория, название, цена, количество)
- **Умная категоризация** — бот запоминает последнюю категорию и предлагает её для следующей траты
- **Отмена** — inline-кнопка «↩ Отменить» после каждой записи
- **Отчёты** — текст + Excel + круговая диаграмма за любой период
- **Отчёт по категории** — детализация трат внутри категории
- **Шаблоны** — повторяющиеся траты (аренда, подписки) с ежедневными напоминаниями
- **Ежемесячное сравнение** — автоматический отчёт 1-го числа: сравнение с прошлым месяцем
- **Мультивалютность** — KZT, RUB, USD, EUR, UAH, GBP, UZS, KGS

## Стек

- Python 3.12+
- [Pyrogram](https://docs.pyrogram.org/) — Telegram MTProto API
- SQLite (aiosqlite) — хранение данных
- APScheduler — планировщик задач
- matplotlib / pandas / openpyxl — отчёты

## Установка

### 1. Клонирование

```bash
git clone https://github.com/your-username/MoneyBot.git
cd MoneyBot
```

### 2. Виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Настройка переменных окружения

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
nano .env
```

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=6737168098:AAH...
```

- **API_ID** и **API_HASH** — получить на [my.telegram.org](https://my.telegram.org/)
- **BOT_TOKEN** — получить у [@BotFather](https://t.me/BotFather)

### 4. Инициализация базы данных

```bash
source venv/bin/activate
python setup_db.py
```

### 5. Запуск

```bash
python bot.py
```

## Деплой на VPS (systemd)

Создайте файл сервиса:

```bash
sudo nano /etc/systemd/system/moneybot.service
```

```ini
[Unit]
Description=Telegram MoneyBot
After=network.target

[Service]
User=vladislav
Group=vladislav
WorkingDirectory=/home/vladislav/MoneyBot
ExecStart=/home/vladislav/MoneyBot/venv/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable moneybot
sudo systemctl start moneybot
```

Управление:

```bash
sudo systemctl status moneybot    # статус
sudo systemctl restart moneybot   # перезапуск
journalctl -u moneybot -f         # логи
```

## Обновление на VPS

```bash
cd /home/vladislav/MoneyBot
git pull origin develop
source venv/bin/activate
pip install -r requirements.txt
python setup_db.py
sudo systemctl restart moneybot
```

## Структура проекта

```
MoneyBot/
├── bot.py              # Основной код бота
├── setup_db.py         # Инициализация БД и класс подключения
├── requirements.txt    # Зависимости
├── .env.example        # Пример переменных окружения
├── .gitignore          # Исключения из Git
└── README.md           # Документация
```

## Структура базы данных

| Таблица | Назначение |
|---------|-----------|
| `categories` | Категории расходов (привязаны к user_id) |
| `expenses` | Записи о тратах |
| `user_states` | Состояние FSM для диалогов |
| `user_settings` | Валюта и последняя категория |
| `recurring_templates` | Шаблоны повторяющихся трат |

## Лицензия

MIT
