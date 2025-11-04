import asyncio
import logging
import re
import os # <-- НОВЫЙ ИМПОРТ: для чтения переменных окружения
from typing import Union
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command

# НОВЫЕ ИМПОРТЫ ДЛЯ REDIS
from aiogram.fsm.storage.redis import RedisStorage, Redis
from aiogram.fsm.storage.memory import MemoryStorage # На случай, если Redis недоступен

# Импорт конфигурации и базы данных
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
from database import db 

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------- КОНСТАНТЫ --------------------------

# !!! ЗАМЕНИТЕ ЭТО НА РЕАЛЬНЫЙ USERNAME ВАШЕГО КАНАЛА !!!
REQUIRED_CHANNEL_USERNAME = '@life_in_stile'
REQUIRED_CHANNEL_URL = f"https://t.me/{REQUIRED_CHANNEL_USERNAME.lstrip('@')}"

# -------------------------- ИНИЦИАЛИЗАЦИЯ (ИЗМЕНЕН) --------------------------

# Получаем DSN для Redis из переменной окружения (REDIS_DSN должен быть настроен на Render)
REDIS_DSN = os.getenv("REDIS_DSN")

if REDIS_DSN:
    # 1. Пытаемся использовать RedisStorage для надежного хранения FSM
    try:
        # Убедитесь, что aiogram[redis] установлен в requirements.txt
        redis_instance = Redis.from_url(REDIS_DSN)
        storage = RedisStorage(redis=redis_instance)
        logger.info("FSM Storage: Используется RedisStorage (надежное хранилище).")
    except Exception as e:
        # 2. Если Redis недоступен/ошибка, используем MemoryStorage как резерв
        logger.error(f"Ошибка подключения к Redis: {e}. Используется MemoryStorage (не рекомендуется для Render).")
        storage = MemoryStorage()
else:
    # 3. Если переменная окружения REDIS_DSN не найдена, используем MemoryStorage
    logger.warning("Переменная REDIS_DSN не найдена. Используется MemoryStorage (не рекомендуется для Render).")
    storage = MemoryStorage()


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
# Передаем настроенное хранилище в Диспетчер
dp = Dispatcher(storage=storage)

# --- FSM States (Машина конечных состояний) ---
class ChannelForm(StatesGroup):
    waiting_for_channel_link = State()
# -------------------------- КОНЕЦ ИЗМЕНЕНИЙ ИНИЦИАЛИЗАЦИИ --------------------------


# -------------------------- Хелперы и проверки --------------------------
# ... (Остальной код без изменений)
async def is_member(user_id: int, channel_id: int) -> bool:
# ... (и так далее, до конца файла)

# -------------------------- ХЕНДЛЕРЫ ЛОГИКИ БОТА --------------------------
# ... (Все хендлеры, включая /start, FSM, и логику обмена - без изменений)
# ...

# -------------------------- ФОНОВАЯ ЗАДАЧА --------------------------
# ... (check_for_unsubs - без изменений)
# ...

# -------------------------- ЗАПУСК БОТА --------------------------

async def main():
    # Устанавливаем соединение с БД
    await db.connect() 
    
    # Запускаем фоновую задачу только если БД успешно подключена
    if db.pool:
        # Запуск фоновой задачи проверки отписок
        dp.startup.register(lambda: asyncio.create_task(check_for_unsubs(bot, db)))
    
    logger.info("Бот запущен. Ожидание обновлений...")
    try:
        # dp уже содержит RedisStorage в качестве хранилища
        await dp.start_polling(bot)
    finally:
        # Корректное закрытие соединений
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    # Если вы не используете отдельный файл для запуска, этот код остаётся
    asyncio.run(main())
