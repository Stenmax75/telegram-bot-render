import aiomysql
import logging
import ssl
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
# Импортируем, как в вашем исходном коде
from config_1 import DB_HOST, DB_NAME, DB_PASS, DB_USER, DB_PORT

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Базовый класс для ошибок базы данных."""
    pass


class NotFoundError(DatabaseError):
    """Исключение: объект не найден в базе данных."""
    pass


class Database:
    """Асинхронная обёртка над aiomysql pool с транзакционной логикой и helper-методами."""

    def __init__(self):
        self.pool: Optional[aiomysql.Pool] = None

    async def connect(self) -> bool:
        """Создаёт пул подключений к MySQL (aiomysql) и инициализирует таблицы."""
        if not (DB_USER and DB_PASS and DB_NAME and DB_HOST):
            logger.error("❌ Отсутствуют учетные данные для MySQL. Подключение невозможно.")
            return False

        try:
            # Настройка SSL-контекста
            ssl_context = ssl.create_default_context(cafile="ca.pem")
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED

            # Важно: autocommit=False чтобы транзакции работали ожидаемо
            self.pool = await aiomysql.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASS,
                db=DB_NAME,
                autocommit=False, # Управление коммитами через transaction()
                minsize=1,
                maxsize=10,
                ssl=ssl_context
            )
            logger.info("База данных: Пул подключений к MySQL успешно создан.")
            await self._create_tables()
            return True
        except Exception:
            logger.exception("❌ КРИТИЧЕСКАЯ ОШИБКА подключения к MySQL")
            self.pool = None
            return False

    async def close(self):
        """Закрывает пул подключений."""
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            logger.info("База данных: Пул подключений закрыт.")

    # ----------------- Вспомогательные методы -----------------

    async def _execute(self, query: str, *params) -> None:
        """Выполнение запроса без возврата результата. Выполняет commit."""
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                await conn.commit()

    async def _fetch(self, query: str, *params) -> List[Dict[str, Any]]:
        """Возвращает все строки как список словарей."""
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                # Возвращаем список словарей (DictCursor уже возвращает dict-подобные объекты)
                return rows if rows else []

    async def _fetchrow(self, query: str, *params) -> Optional[Dict[str, Any]]:
        """Возвращает одну строку как словарь или None."""
        if not self.pool:
            return None
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                # Возвращаем словарь
                return row if row else None

    @asynccontextmanager
    async def transaction(self):
        """
        Контекстный менеджер для транзакций.
        Используется для атомарных операций, требующих нескольких запросов.
        """
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")
        async with self.pool.acquire() as conn:
            try:
                # Убеждаемся, что conn.begin() не вызывается, если aiomysql настроен на auto-begin
                yield conn
                await conn.commit()
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    logger.exception("Не удалось сделать rollback транзакции")
                raise

    # ----------------- Создание таблиц и индексов -----------------

    async def _create_tables(self):
        """Создаёт таблицы и индексы, если их ещё нет."""
        if not self.pool:
            return

        # users
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255)
            ) ENGINE=InnoDB;
            """
        )

        # channels: УДАЛЕН UNIQUE KEY uq_channels_owner, чтобы позволить несколько каналов на одного владельца
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id BIGINT PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                `link` VARCHAR(255) NOT NULL,
                title VARCHAR(255),
                subscribers_needed INT DEFAULT 0,
                queue_join_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
                INDEX idx_channels_owner (owner_id) -- Добавлен обычный индекс для поиска по владельцу
            ) ENGINE=InnoDB;
            """
        )

        # subscriptions (остается без изменений)
        await self._execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subscriber_user_id BIGINT NOT NULL,
                subscribed_channel_id BIGINT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                channel_that_owes_id BIGINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME NULL,
                FOREIGN KEY (subscriber_user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (subscribed_channel_id) REFERENCES channels(channel_id) ON DELETE RESTRICT,
                FOREIGN KEY (channel_that_owes_id) REFERENCES channels(channel_id) ON DELETE RESTRICT,
                INDEX idx_sub_active (subscriber_user_id, subscribed_channel_id, is_active),
                INDEX idx_subscribed_channel (subscribed_channel_id),
                INDEX idx_channel_owes (channel_that_owes_id)
            ) ENGINE=InnoDB;
            """
        )

        logger.info("База данных: Все таблицы MySQL успешно проверены/созданы.")

    # ----------------- CRUD / Domain methods -----------------

    async def add_user(self, user_id: int, username: Optional[str] = None):
        """Добавление/обновление пользователя."""
        await self._execute(
            """INSERT INTO users (user_id, username)
               VALUES (%s, %s)
               ON DUPLICATE KEY UPDATE username = %s""",
            user_id, username, username
        )

    async def get_user_channels_info(self, owner_id: int) -> List[Dict[str, Any]]:
        """Получить информацию обо всех каналах по ID владельца."""
        return await self._fetch(
            "SELECT channel_id, `link`, title, subscribers_needed FROM channels WHERE owner_id = %s",
            owner_id
        )
    
    async def get_channel_info_by_channel_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Получить информацию об ОДНОМ канале по его ID."""
        return await self._fetchrow(
            "SELECT channel_id, owner_id, `link`, title, subscribers_needed FROM channels WHERE channel_id = %s",
            channel_id
        )

    async def get_user_channel_with_highest_debt(self, owner_id: int) -> Optional[Dict[str, Any]]:
        """
        Возвращает канал пользователя, который нуждается в наибольшем количестве подписчиков.
        Это канал, который получит 'долг' при подписке пользователя.
        """
        return await self._fetchrow(
            """
            SELECT channel_id, `link`, title, subscribers_needed
            FROM channels
            WHERE owner_id = %s
            ORDER BY subscribers_needed DESC, queue_join_time ASC
            LIMIT 1
            """,
            owner_id
        )

    async def is_channel_registered_by_other(self, channel_id: int, user_id: int) -> bool:
        """
        Проверяет, зарегистрирован ли канал с данным channel_id и принадлежит ли он 
        другому пользователю (owner_id != user_id).
        """
        # ИСПРАВЛЕНО: УДАЛЕН НЕРАЗРЫВНЫЙ ПРОБЕЛ ИЗ `owner_id`
        query = """
            SELECT owner_id
            FROM channels
            WHERE channel_id = %s AND owner_id != %s
        """
        result = await self._fetchrow(query, channel_id, user_id)
        return result is not None

    async def add_channel(self, owner_id: int, channel_id: int, link: str, title: str) -> bool:
        """
        Добавляет канал и владельца. Возвращает True, если добавлено, False если уже существует
        канал с таким channel_id (т.к. channel_id - PRIMARY KEY).
        """
        # 1. Сначала убеждаемся, что владелец существует (для FOREIGN KEY)
        await self.add_user(owner_id, title)
        
        try:
            # 2. Используем INSERT ... ON DUPLICATE KEY UPDATE для обработки возможного дубликата по channel_id
            await self._execute(
                """INSERT INTO channels
                    (channel_id, owner_id, `link`, title, subscribers_needed)
                    VALUES (%s, %s, %s, %s, 0)
                    ON DUPLICATE KEY UPDATE owner_id = channel_id  #owner_id_было раньше
                """,
                # Убеждаемся, что переменные передаются в правильном порядке
                channel_id, owner_id, link, title
            )
            # Если канал уже был, это вернет True, но не добавит новую строку.
            # Поскольку мы проверяем канал_id, это предотвратит двойную регистрацию одного и того же канала.
            return True
        except Exception:
            logger.exception("Ошибка при добавлении канала")
            return False

    # ИЗМЕНЕННАЯ ФУНКЦИЯ ДЛЯ ПОСЛЕДОВАТЕЛЬНОГО ПРЕДЛОЖЕНИЯ КАНАЛОВ
    async def get_target_channel(self, user_id: int, excluded_channel_ids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
        """
        Находит канал для обмена, исключая собственные, уже подписанные 
        и временно исключенные (excluded_channel_ids).
        """
        # ... (логика остается прежней)
        if excluded_channel_ids is None:
            excluded_channel_ids = []

        # Получаем список всех каналов, принадлежащих пользователю, чтобы исключить их все
        owner_channels = await self.get_user_channels_info(user_id)
        owner_channel_ids = [c['channel_id'] for c in owner_channels]

        # Добавляем все каналы пользователя в список исключений, если они еще не там
        for ch_id in owner_channel_ids:
            if ch_id not in excluded_channel_ids:
                excluded_channel_ids.append(ch_id)


        sql_base = """
        SELECT c.channel_id, c.link, c.title, c.subscribers_needed
        FROM channels c
        LEFT JOIN subscriptions s
            ON s.subscribed_channel_id = c.channel_id
            AND s.subscriber_user_id = %s
            AND s.is_active = TRUE
        WHERE 
            s.id IS NULL -- Исключаем каналы, на которые уже активно подписан
        """
        
        params = [user_id]
        
        # ЛОГИКА ИСКЛЮЧЕНИЯ: Добавляем условие NOT IN для всех исключенных (включая свои)
        if excluded_channel_ids:
            # Создаем строку плейсхолдеров (%s) для NOT IN
            placeholders = ', '.join(['%s'] * len(excluded_channel_ids))
            sql_base += f" AND c.channel_id NOT IN ({placeholders})"
            params.extend(excluded_channel_ids)
            
        sql_base += """
        ORDER BY c.subscribers_needed ASC, c.queue_join_time ASC
        LIMIT 1
        """
        
        return await self._fetchrow(sql_base, *params)


    async def get_channel_owner_info(self, channel_id: int) -> Optional[Dict[str, Any]]:
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

    async def get_channel_to_verify(self) -> Optional[Dict[str, Any]]:
        # ... (логика остается прежней)
        query = """
            SELECT c.channel_id, c.owner_id
            FROM channels c
            WHERE c.subscribers_needed > 0
            ORDER BY c.queue_join_time ASC 
            LIMIT 1
        """
        return await self._fetchrow(query)


    # ----------------- Транзакционные методы -----------------

    async def register_subscription_and_create_debt(
        self,
        subscriber_user_id: int,
        subscribed_channel_id: int,
        channel_that_owes_id: int # Переименовано для ясности: это канал пользователя, который создает долг
    ) -> int:
        """
        Атомарно:
          - проверяет, не существует ли уже активная аналогичная подписка,
          - увеличивает subscribers_needed у channel_that_owes_id (Канал A),
          - вставляет запись в subscriptions.
        Возвращает новое значение subscribers_needed.
        """
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")

        async with self.transaction() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    # 1. Блокируем строку канала-должника (Канал A)
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s FOR UPDATE",
                        (channel_that_owes_id,)
                    )
                    ch = await cur.fetchone()
                    if not ch:
                        raise NotFoundError(f"Channel to receive subscribers not found: {channel_that_owes_id}")

                    # 2. Проверяем, есть ли уже активная подписка (Пользователь A -> Канал B)
                    await cur.execute(
                        """SELECT id FROM subscriptions
                            WHERE subscriber_user_id = %s AND subscribed_channel_id = %s
                              AND is_active = TRUE
                            FOR UPDATE""",
                        (subscriber_user_id, subscribed_channel_id)
                    )
                    existing = await cur.fetchone()
                    if existing:
                        # Если уже существует активная подписка — просто возвращаем текущее значение
                        return int(ch["subscribers_needed"])

                    # 3. Увеличиваем счётчик у канала-должника (Канал A) и обновляем время очереди
                    await cur.execute(
                        """UPDATE channels
                            SET subscribers_needed = subscribers_needed + 1,
                                queue_join_time = NOW()
                            WHERE channel_id = %s""",
                        (channel_that_owes_id,)
                    )

                    # 4. Вставляем запись подписки (channel_that_owes_id = Канал A)
                    await cur.execute(
                        """INSERT INTO subscriptions
                            (subscriber_user_id, subscribed_channel_id, channel_that_owes_id, is_active)
                            VALUES (%s, %s, %s, TRUE)""",
                        (subscriber_user_id, subscribed_channel_id, channel_that_owes_id)
                    )

                    # 5. Получаем новое значение subscribers_needed
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
                        (channel_that_owes_id,)
                    )
                    new_row = await cur.fetchone()
                    return int(new_row["subscribers_needed"])
                except Exception:
                    logger.exception("Ошибка в register_subscription_and_create_debt")
                    raise

    async def fulfill_debt(self, channel_that_owes_id: int) -> int:
        # ... (логика остается прежней)
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")

        async with self.transaction() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    # 1. Находим самую старую активную запись, которая создала ДОЛГ для Канала A (FOR UPDATE)
                    await cur.execute(
                        """SELECT id, subscribed_channel_id, subscriber_user_id FROM subscriptions
                            WHERE channel_that_owes_id = %s
                              AND is_active = TRUE
                            ORDER BY created_at ASC
                            LIMIT 1 FOR UPDATE""",
                        (channel_that_owes_id,)
                    )
                    sub = await cur.fetchone()
                    
                    if not sub:
                        # Если долг уже погашен (или ошибка логики), просто уменьшаем счётчик
                        logger.warning(f"Не найдена активная подписка для погашения долга. channel_that_owes_id={channel_that_owes_id}. Только уменьшаем счётчик.")
                        pass # Продолжаем к уменьшению счётчика

                    else:
                        sub_id = sub["id"]
                        # 2. Деактивируем подписку
                        await cur.execute(
                            "UPDATE subscriptions SET is_active = FALSE, finished_at = NOW() WHERE id = %s",
                            (sub_id,)
                        )
                    
                    # 3. Уменьшаем счётчик у канала-должника (Канал A)
                    await cur.execute(
                        """UPDATE channels
                            SET subscribers_needed = GREATEST(subscribers_needed - 1, 0)
                            WHERE channel_id = %s""",
                        (channel_that_owes_id,)
                    )

                    # 4. Получаем обновлённый баланс
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
                        (channel_that_owes_id,)
                    )
                    row = await cur.fetchone()
                    return int(row["subscribers_needed"])
                except Exception:
                    logger.exception("Ошибка в fulfill_debt")
                    raise

    async def get_active_subscriptions_to_check(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Получает список активных подписок для периодической проверки на отписку. 
        Включает owner_id для удобства уведомления владельца канала.
        """
        query = """
            SELECT 
                s.id, s.subscriber_user_id, s.subscribed_channel_id, s.channel_that_owes_id, c.owner_id
            FROM subscriptions s
            JOIN channels c ON s.subscribed_channel_id = c.channel_id
            WHERE s.is_active = TRUE 
            ORDER BY s.created_at ASC
            LIMIT %s
        """
        return await self._fetch(query, limit)

    async def get_channel_debt(self, channel_id: int) -> int:
        """Получает текущий долг канала (сколько нужно получить подписчиков)."""
        row = await self._fetchrow(
            "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
            channel_id
        )
        return int(row["subscribers_needed"]) if row else 0

# Экземпляр базы
db = Database()

