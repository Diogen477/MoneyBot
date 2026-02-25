import aiosqlite
import asyncio
import logging


class DatabaseConnection:
    """Singleton-подключение к БД. Одно соединение на всё время работы бота."""
    _connection: aiosqlite.Connection | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_connection(cls) -> aiosqlite.Connection:
        async with cls._lock:
            if cls._connection is None:
                cls._connection = await aiosqlite.connect('expenses.db')
                await cls._connection.execute("PRAGMA journal_mode=WAL")
                await cls._connection.execute("PRAGMA foreign_keys=ON")
        return cls._connection

    @classmethod
    async def close(cls):
        async with cls._lock:
            if cls._connection is not None:
                await cls._connection.close()
                cls._connection = None

    async def __aenter__(self):
        self.conn = await self.get_connection()
        self.cursor = await self.conn.cursor()
        return self.cursor

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_value is None:
            await self.conn.commit()
        else:
            try:
                await self.conn.rollback()
            except Exception:
                pass


async def init_db():
    async with DatabaseConnection() as cursor:
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(user_id, name)
            )
        ''')

        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                name TEXT,
                price REAL,
                quantity REAL,
                total REAL NOT NULL,
                date TEXT NOT NULL,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        ''')

        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                data TEXT
            )
        ''')

        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                currency TEXT DEFAULT 'KZT',
                last_category_id INTEGER
            )
        ''')

        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS recurring_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                name TEXT,
                amount REAL NOT NULL,
                day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 28),
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        ''')

        # Индексы
        await cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)')
        await cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id)')
        await cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id)')
        await cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_templates_user_day ON recurring_templates(user_id, day_of_month)')

        # ── Миграция: объединение дублей категорий, отличающихся только регистром ──
        await cursor.execute('''
            SELECT c1.id AS dup_id, c2.id AS keep_id
            FROM categories c1
            JOIN categories c2
              ON c1.user_id = c2.user_id
             AND LOWER(c1.name) = LOWER(c2.name)
             AND c1.id > c2.id
        ''')
        duplicates = await cursor.fetchall()
        for dup_id, keep_id in duplicates:
            await cursor.execute(
                'UPDATE expenses SET category_id = ? WHERE category_id = ?',
                (keep_id, dup_id))
            await cursor.execute(
                'UPDATE recurring_templates SET category_id = ? WHERE category_id = ?',
                (keep_id, dup_id))
            await cursor.execute(
                'DELETE FROM categories WHERE id = ?', (dup_id,))
        if duplicates:
            logging.info(f"Миграция: объединено {len(duplicates)} дублей категорий (регистр)")

        # Пересоздаём уникальный индекс с COLLATE NOCASE (если ещё нет)
        try:
            await cursor.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_user_name_nocase '
                'ON categories(user_id, name COLLATE NOCASE)')
        except Exception:
            pass  # Индекс может конфликтовать со старым UNIQUE — не критично

        # ── Миграция: нормализация названий трат (первая буква заглавная) ──
        await cursor.execute('''
            UPDATE expenses
            SET name = UPPER(SUBSTR(name, 1, 1)) || SUBSTR(name, 2)
            WHERE name IS NOT NULL
              AND name != ''
              AND SUBSTR(name, 1, 1) != UPPER(SUBSTR(name, 1, 1))
        ''')


async def _standalone_init():
    await init_db()
    await DatabaseConnection.close()

if __name__ == "__main__":
    asyncio.run(_standalone_init())
