import aiosqlite
import asyncio

class DatabaseConnection:
    async def __aenter__(self):
        self.conn = await aiosqlite.connect('expenses.db')  # Асинхронное подключение к базе данных
        self.cursor = await self.conn.cursor()  # Получаем курсор для выполнения запросов
        return self.cursor

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_value is None:
            await self.conn.commit()  # Асинхронный коммит изменений
        await self.conn.close()  # Асинхронное закрытие подключения

async def init_db():
    async with DatabaseConnection() as cursor:
        # Таблица для категорий, связываем категории с пользователями
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(user_id, name)
            )
        ''')

        # Таблица для расходов
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

        # Таблица для хранения состояния пользователей
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                data TEXT
            )
        ''')

if __name__ == "__main__":
    asyncio.run(init_db())
