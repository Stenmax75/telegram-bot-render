# database.py (Переписан для aiomysql с поддержкой SSL для TiDB Cloud)
import aiomysql
import logging
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
            self.pool = await aiomysql.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASS,
                db=DB_NAME,
                autocommit=True, 
                minsize=1,
                maxsize=10,
                # --- НОВОЕ: Настройки SSL для TiDB Cloud (Используем ca.pem) ---
                ssl={
                    # Указываем путь к файлу сертификата, который должен быть в корне проекта
                    'ca': 'ca.pem',
                    # Требуем проверку подлинности сервера для безопасности
                    'verify_identity': True, 
                }
                # -------------------------------------------------------------
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
        """Создание необходимых таблиц с InnoDB для поддержки внешних ключей и транзакций."""
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
        """Добавление/обновление пользователя. Используем ON DUPLICATE KEY UPDATE для MySQL."""
        await self._execute(
            """INSERT INTO users (user_id, username) 
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE username = %s""", 
            user_id, username, username
        )
    
    async def get_user_channel_info(self, owner_id: int):
        row = await self._fetchrow(
            "SELECT channel_id, link, title, subscribers_needed FROM channels WHERE owner_id = %s",
            owner_id
        )
        return row
    
    async def add_channel(self, owner_id: int, channel_id: int, link: str, title: str):
        await self.add_user(owner_id, "channel_owner_placeholder")
        
        await self._execute(
            """INSERT IGNORE INTO channels 
                (channel_id, owner_id, link, title, subscribers_needed) 
                VALUES (%s, %s, %s, %s, 0)""", 
            channel_id, owner_id, link, title
        )

    async def get_target_channel(self, user_id: int):
        """Получение канала для подписки."""
        row = await self._fetchrow(
            """
            SELECT c.channel_id, c.link, c.title
            FROM channels c
            LEFT JOIN subscriptions s ON c.channel_id = s.subscribed_channel_id AND s.subscriber_user_id = %s AND s.is_active = TRUE
            WHERE c.owner_id != %s 
              AND c.subscribers_needed > 0 
              AND s.subscriber_user_id IS NULL 
            ORDER BY c.subscribers_needed DESC 
            LIMIT 1
            """,
            user_id, user_id
        )
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
        """Регистрирует подписку и увеличивает счетчик (создание долга)."""
        async with self.pool.acquire() as conn:
            # MySQL транзакция
            async with conn.cursor() as cur:
                await cur.execute("START TRANSACTION")
                try:
                    # 1. Увеличение счетчика для Канала Б
                    await cur.execute(
                        """UPDATE channels 
                           SET subscribers_needed = subscribers_needed + 1 
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
                    # 1. Уменьшение счетчика для Канала Б
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
                    new_subs_needed = (await cur.fetchone())[0] # aiomysql fetchone возвращает кортеж
                    
                    await conn.commit()
                    return new_subs_needed
                except Exception as e:
                    await conn.rollback()
                    logger.error(f"MySQL Transaction Rollback: {e}")
                    raise

# Инициализация объекта Database (как Singleton)
db = Database()