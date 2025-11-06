import aiomysql
import logging
import ssl 
from config_1 import DB_HOST, DB_NAME, DB_PASS, DB_USER, DB_PORT

logger = logging.getLogger(__name__)

class Database:
    """Класс для работы с асинхронной базой данных MySQL через aiomysql."""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Создает пул подключений к MySQL и инициализирует таблицы."""
        if not (DB_USER and DB_PASS and DB_NAME and DB_HOST):
            logger.error("❌ Отсутствуют учетные данные для MySQL. Подключение невозможно.")
            return False
            
        try:
            # Создание SSL-контекста
            ssl_context = ssl.create_default_context(
                cafile='ca.pem'
            )
            ssl_context.check_hostname = True 
            ssl_context.verify_mode = ssl.CERT_REQUIRED

            self.pool = await aiomysql.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASS,
                db=DB_NAME,
                autocommit=True, 
                minsize=1,
                maxsize=10,
                ssl=ssl_context
            )
            logger.info("База данных: Пул подключений к MySQL успешно создан.")
            await self._create_tables()
            return True
        except Exception as e:
            logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА подключения к MySQL: {e}")
            self.pool = None 
            return False

    async def close(self):
        """Закрывает пул подключений."""
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            logger.info("База данных: Пул подключений закрыт.")

    # --- Вспомогательные методы для простых операций ---

    async def _execute(self, query: str, *args):
        """Выполнение запроса без возврата данных (с autocommit)."""
        if not self.pool: return
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)

    async def _fetch(self, query: str, *args):
        """Получение всех строк."""
        if not self.pool: return []
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return await cur.fetchall()

    async def _fetchrow(self, query: str, *args):
        """Получение одной строки."""
        if not self.pool: return None
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return await cur.fetchone()

    async def _create_tables(self):
        """Создание необходимых таблиц."""
        if not self.pool: return
        
        await self._execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255)
            ) ENGINE=InnoDB;
        """)
        
        await self._execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id BIGINT PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                link VARCHAR(255) NOT NULL,
                title VARCHAR(255),
                subscribers_needed INT DEFAULT 0,
                FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE
            ) ENGINE=InnoDB;
        """)
        
        # ДОБАВЛЕНИЕ КОЛОНКИ queue_join_time (Если её еще нет)
        await self._execute("""
            ALTER TABLE channels 
            ADD COLUMN IF NOT EXISTS queue_join_time DATETIME DEFAULT CURRENT_TIMESTAMP;
        """)

        await self._execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subscriber_user_id BIGINT NOT NULL,
                subscribed_channel_id BIGINT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                channel_that_owes_id BIGINT NOT NULL,
                FOREIGN KEY (subscriber_user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (subscribed_channel_id) REFERENCES channels(channel_id) ON DELETE RESTRICT,
                FOREIGN KEY (channel_that_owes_id) REFERENCES channels(channel_id) ON DELETE RESTRICT
            ) ENGINE=InnoDB;
        """)
        logger.info("База данных: Все таблицы MySQL успешно проверены/созданы.")

    # ------------------- Методы для работы с данными -------------------

    async def add_user(self, user_id: int, username: str):
        """Добавление/обновление пользователя."""
        await self._execute(
            """INSERT INTO users (user_id, username) 
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE username = %s""", 
            user_id, username, username
        )
    
    async def get_user_channel_info(self, owner_id: int):
        """Получить ID, ссылку, название и долг канала по ID владельца."""
        row = await self._fetchrow(
            "SELECT channel_id, link, title, subscribers_needed FROM channels WHERE owner_id = %s",
            owner_id
        )
        return row
    
    # --- НОВАЯ ФУНКЦИЯ ДЛЯ ВЗАИМНОЙ ПОДПИСКИ ---
    async def get_channel_info_by_owner_id(self, owner_id: int):
        """Возвращает информацию о канале (ID, link, title) по ID владельца."""
        # Используем ту же логику, что и в get_user_channel_info, но возвращаем нужные поля
        row = await self._fetchrow(
            "SELECT channel_id, link, title, subscribers_needed FROM channels WHERE owner_id = %s",
            owner_id
        )
        return row
    # --- КОНЕЦ НОВОЙ ФУНКЦИИ ---
    
    async def add_channel(self, owner_id: int, channel_id: int, link: str, title: str):
        await self.add_user(owner_id, "channel_owner_placeholder")
        
        await self._execute(
            """INSERT IGNORE INTO channels 
                (channel_id, owner_id, link, title, subscribers_needed) 
                VALUES (%s, %s, %s, %s, 0)""", 
            channel_id, owner_id, link, title
        )

    async def get_target_channel(self, user_id: int):
        """
        Получение канала для подписки с приоритетом по долгу и времени ожидания.
        ВРЕМЕННОЕ ИЗМЕНЕНИЕ: subscribers_needed >= 0 для тестирования.
        """
        query = """
            SELECT c.channel_id, c.link, c.title
            FROM channels c
            LEFT JOIN subscriptions s ON c.channel_id = s.subscribed_channel_id AND s.subscriber_user_id = %s AND s.is_active = TRUE
            WHERE c.owner_id != %s
              AND c.subscribers_needed >= 0 
              AND s.subscriber_user_id IS NULL
            ORDER BY c.subscribers_needed ASC, c.queue_join_time ASC, RAND()
            LIMIT 1
        """
        row = await self._fetchrow(query, user_id, user_id)
        return row

    async def get_channel_owner_info(self, channel_id: int):
        """Получить информацию о владельце канала."""
        row = await self._fetchrow(
            """
            SELECT c.owner_id, c.title, c.link, u.username
            FROM channels c
            JOIN users u ON c.owner_id = u.user_id
            WHERE c.channel_id = %s
            """,
            channel_id
        )
        return row
        
    # --- Транзакционные методы (для атомарности) ---

    async def register_subscription_and_create_debt(
        self, 
        subscriber_user_id: int, 
        subscribed_channel_id: int, 
        subscriber_channel_id: int
    ):
        """Регистрирует подписку и увеличивает счетчик (создание долга) и обновляет время очереди."""
        async with self.pool.acquire() as conn:
            # MySQL транзакция
            async with conn.cursor() as cur:
                await cur.execute("START TRANSACTION")
                try:
                    # 1. Увеличение счетчика и обновление queue_join_time
                    await cur.execute(
                        """UPDATE channels 
                           SET subscribers_needed = subscribers_needed + 1,
                               queue_join_time = NOW()
                           WHERE channel_id = %s""",
                        (subscriber_channel_id,)
                    )
                    
                    # 2. Регистрация подписки 
                    await cur.execute(
                        """INSERT INTO subscriptions 
                           (subscriber_user_id, subscribed_channel_id, channel_that_owes_id) 
                           VALUES (%s, %s, %s)""",
                        (subscriber_user_id, subscribed_channel_id, subscriber_channel_id)
                    )
                    await conn.commit()
                except Exception as e:
                    await conn.rollback()
                    logger.error(f"MySQL Transaction Rollback: {e}")
                    raise

    async def fulfill_debt(self, subscriber_user_id: int, subscribed_channel_id: int, channel_that_owes_id: int):
        """Регистрирует подписку, уменьшает счетчик (погашение долга) и возвращает новое значение долга."""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("START TRANSACTION")
                try:
                    # 1. Уменьшение счетчика для Канала, который получает подписку (Канал, которому должны)
                    await cur.execute(
                        """UPDATE channels 
                           SET subscribers_needed = subscribers_needed - 1 
                           WHERE channel_id = %s""",
                        (channel_that_owes_id,)
                    )
                    
                    # 2. Регистрация подписки 
                    await cur.execute(
                        """INSERT INTO subscriptions 
                           (subscriber_user_id, subscribed_channel_id, channel_that_owes_id) 
                           VALUES (%s, %s, %s)""",
                        (subscriber_user_id, subscribed_channel_id, channel_that_owes_id)
                    )
                    
                    # 3. Получаем новое значение долга для Канала Б
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
                        (channel_that_owes_id,)
                    )
                    new_subs_needed = (await cur.fetchone())[0]
                    
                    await conn.commit()
                    return new_subs_needed
                except Exception as e:
                    await conn.rollback()
                    logger.error(f"MySQL Transaction Rollback: {e}")
                    raise

db = Database()
