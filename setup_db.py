import aiosqlite
import asyncio
import logging


class DatabaseConnection:
    """Singleton-подключение к БД. Одно соединение на всё время работы бота."""
    _connection: aiosqlite.Connection | None = None

    @classmethod
    async def get_connection(cls) -> aiosqlite.Connection:
        if cls._connection is None:
            cls._connection = await aiosqlite.connect('expenses.db')
            await cls._connection.execute("PRAGMA journal_mode=WAL")
            await cls._connection.execute("PRAGMA foreign_keys=ON")
        return cls._connection

    @classmethod
    async def close(cls):
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


async def _standalone_init():
    await init_db()
    await DatabaseConnection.close()

if __name__ == "__main__":
    asyncio.run(_standalone_init())
