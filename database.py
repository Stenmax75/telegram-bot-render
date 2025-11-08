import aiomysql
import logging
import ssl
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
from config_1 import DB_HOST, DB_NAME, DB_PASS, DB_USER, DB_PORT

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    pass


class NotFoundError(DatabaseError):
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
                autocommit=False,
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
        """Выполнение запроса без возврата результата (использует DictCursor)."""
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                # Не делаем commit здесь: вызывающий код должен управлять транзакцией.
                # Для одиночных не критичных запросов можно вызвать conn.commit() при необходимости.

    async def _fetch(self, query: str, *params) -> List[Dict[str, Any]]:
        """Возвращает все строки как список словарей."""
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(r) for r in rows] if rows else []

    async def _fetchrow(self, query: str, *params) -> Optional[Dict[str, Any]]:
        """Возвращает одну строку как словарь или None."""
        if not self.pool:
            return None
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    @asynccontextmanager
    async def transaction(self):
        """
        Контекстный менеджер для транзакций.
        Пример:
            async with db.transaction() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(...)
        """
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
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

        # channels: добавляем UNIQUE(owner_id) чтобы один владелец — один канал (если такое семантически нужно)
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
                UNIQUE KEY uq_channels_owner (owner_id)
            ) ENGINE=InnoDB;
            """
        )

        # subscriptions
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

    async def add_user(self, user_id: int, username: str):
        """Добавление/обновление пользователя."""
        # Это одиночный запрос — можно выполнить без явной транзакции
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """INSERT INTO users (user_id, username)
                       VALUES (%s, %s)
                       ON DUPLICATE KEY UPDATE username = %s""",
                    (user_id, username, username)
                )
                await conn.commit()

    async def get_user_channel_info(self, owner_id: int) -> Optional[Dict[str, Any]]:
        """Получить информацию о канале по ID владельца."""
        return await self._fetchrow(
            "SELECT channel_id, `link`, title, subscribers_needed FROM channels WHERE owner_id = %s",
            owner_id
        )

    async def get_channel_info_by_owner_id(self, owner_id: int) -> Optional[Dict[str, Any]]:
        """То же, что get_user_channel_info (запрос с другим именем для читабельности)."""
        return await self.get_user_channel_info(owner_id)

    async def add_channel(self, owner_id: int, channel_id: int, link: str, title: str):
        """Добавляет канал. Предполагается, что пользователь уже создан (add_user будет вызван при необходимости)."""
        # Обновляем/добавляем пользователя-запись (placeholder)
        await self.add_user(owner_id, "channel_owner_placeholder")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """INSERT IGNORE INTO channels
                       (channel_id, owner_id, `link`, title, subscribers_needed)
                       VALUES (%s, %s, %s, %s, 0)""",
                    (channel_id, owner_id, link, title)
                )
                await conn.commit()

    async def get_target_channel(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Возвращает один канал-цель (для подписки пользователем user_id),
        который не принадлежит user_id и на который этот пользователь ещё не подписывался (активно).
        """
        query = """
            SELECT c.channel_id, c.link, c.title
            FROM channels c
            LEFT JOIN subscriptions s ON c.channel_id = s.subscribed_channel_id
                AND s.subscriber_user_id = %s AND s.is_active = TRUE
            WHERE c.owner_id != %s
              AND s.id IS NULL
            ORDER BY c.subscribers_needed ASC, c.queue_join_time ASC
            LIMIT 1
        """
        return await self._fetchrow(query, user_id, user_id)

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

    # ----------------- Транзакционные методы -----------------

    async def register_subscription_and_create_debt(
        self,
        subscriber_user_id: int,
        subscribed_channel_id: int,
        subscriber_channel_id: int
    ) -> int:
        """
        Атомарно:
          - проверяет, не существует ли уже активная аналогичная подписка (чтобы избежать дубликатов),
          - увеличивает subscribers_needed у subscriber_channel_id,
          - вставляет запись в subscriptions.
        Возвращает новое значение subscribers_needed.
        """
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")

        async with self.transaction() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    # Блокируем соответствующую строку канала, чтобы избежать гонки при счётчике
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s FOR UPDATE",
                        (subscriber_channel_id,)
                    )
                    ch = await cur.fetchone()
                    if not ch:
                        raise NotFoundError("Channel to receive subscribers not found")

                    # Проверяем, есть ли уже активная подписка от этого пользователя на этот канал в контексте этой "долговой" записи
                    await cur.execute(
                        """SELECT id FROM subscriptions
                           WHERE subscriber_user_id = %s AND subscribed_channel_id = %s
                                 AND channel_that_owes_id = %s AND is_active = TRUE
                           FOR UPDATE""",
                        (subscriber_user_id, subscribed_channel_id, subscriber_channel_id)
                    )
                    existing = await cur.fetchone()
                    if existing:
                        # Если уже существует активная подписка — просто возвращаем текущее значение
                        return int(ch["subscribers_needed"])

                    # Увеличиваем счётчик и обновляем время очереди
                    await cur.execute(
                        """UPDATE channels
                           SET subscribers_needed = subscribers_needed + 1,
                               queue_join_time = NOW()
                           WHERE channel_id = %s""",
                        (subscriber_channel_id,)
                    )

                    # Вставляем запись подписки
                    await cur.execute(
                        """INSERT INTO subscriptions
                           (subscriber_user_id, subscribed_channel_id, channel_that_owes_id, is_active)
                           VALUES (%s, %s, %s, TRUE)""",
                        (subscriber_user_id, subscribed_channel_id, subscriber_channel_id)
                    )

                    # Получаем новое значение subscribers_needed
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
                        (subscriber_channel_id,)
                    )
                    new_row = await cur.fetchone()
                    return int(new_row["subscribers_needed"])
                except Exception:
                    logger.exception("Ошибка в register_subscription_and_create_debt")
                    raise

    async def fulfill_debt(self, subscriber_user_id: int, subscribed_channel_id: int, channel_that_owes_id: int) -> int:
        """
        Атомарно:
          - находит активную запись подписки, соответствующую (subscribed_channel_id, channel_that_owes_id),
            помечает её как неактивную (is_active = FALSE, finished_at = NOW())
          - уменьшает subscribers_needed у channel_that_owes_id на 1 (до 0 минимум)
        Возвращает новое значение subscribers_needed.
        """
        if not self.pool:
            raise DatabaseError("DB pool is not initialized")

        async with self.transaction() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    # Ищем подходящую активную подписку (блокируем её строку)
                    await cur.execute(
                        """SELECT id FROM subscriptions
                           WHERE subscribed_channel_id = %s
                             AND channel_that_owes_id = %s
                             AND is_active = TRUE
                           ORDER BY id ASC
                           LIMIT 1 FOR UPDATE""",
                        (subscribed_channel_id, channel_that_owes_id)
                    )
                    sub = await cur.fetchone()
                    if not sub:
                        raise NotFoundError("Активная подписка для погашения долга не найдена")

                    sub_id = sub["id"]

                    # Деактивируем подписку
                    await cur.execute(
                        "UPDATE subscriptions SET is_active = FALSE, finished_at = NOW() WHERE id = %s",
                        (sub_id,)
                    )

                    # Уменьшаем счётчик у канала-должника (защита от отрицательных значений)
                    await cur.execute(
                        """UPDATE channels
                           SET subscribers_needed = GREATEST(subscribers_needed - 1, 0)
                           WHERE channel_id = %s""",
                        (channel_that_owes_id,)
                    )

                    # Получаем обновлённый баланс
                    await cur.execute(
                        "SELECT subscribers_needed FROM channels WHERE channel_id = %s",
                        (channel_that_owes_id,)
                    )
                    row = await cur.fetchone()
                    return int(row["subscribers_needed"])
                except Exception:
                    logger.exception("Ошибка в fulfill_debt")
                    raise


# Экземпляр базы
db = Database()
