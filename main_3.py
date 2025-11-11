import asyncio
import logging
import re
import os
import contextlib
from typing import Union, Optional, Any
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from datetime import timedelta

# REDIS / STORAGE
from aiogram.fsm.storage.redis import RedisStorage, Redis
# from aiogram.fsm.storage.memory import MemoryStorage # Не нужен, если используем Redis/Fallback

# Конфиг и БД (убедитесь, что REQUIRED_CHANNEL_ID в config_1 - int)
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
# Предполагается, что database.py предоставляет db и NotFoundError
from database import db, NotFoundError

# --- КОНФИГУРАЦИЯ И ЗАПУСК ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CONSTANTS
BOT_SUPPORT_CHANNEL_USERNAME = '@life_in_stile' # Переименовано для ясности
BOT_SUPPORT_CHANNEL_URL = f"https://t.me/{BOT_SUPPORT_CHANNEL_USERNAME.lstrip('@')}"
UNSUB_CHECK_INTERVAL = timedelta(minutes=30) # Установлено как константа для удобства

# REDIS STORAGE
REDIS_DSN = os.getenv("REDIS_DSN")
if REDIS_DSN:
    try:
        redis_instance = Redis.from_url(REDIS_DSN)
        storage = RedisStorage(redis=redis_instance)
        logger.info("FSM Storage: RedisStorage используется.")
    except Exception:
        logger.exception("Ошибка при подключении к Redis. Falling back to MemoryStorage.")
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
else:
    logger.warning("REDIS_DSN не задан. Используется MemoryStorage.")
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = MemoryStorage()

# Инициализация
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# FSM States
class ChannelForm(StatesGroup):
    waiting_for_channel_link = State()

# --- HELPERS ---

async def is_member(bot: Bot, user_id: int, channel_id: Union[int, str]) -> bool:
    """Проверяем членство, включая CREATOR и ADMINISTRATOR."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        # CREATOR - владелец, ADMINISTRATOR - админ, MEMBER - подписчик.
        # LEFT и KICKED — не член.
        return member.status in (
            ChatMemberStatus.MEMBER, 
            ChatMemberStatus.ADMINISTRATOR, 
            ChatMemberStatus.CREATOR
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        # Ошибка доступа или неверный ID
        return False
    except Exception:
        logger.exception("Неожиданная ошибка в is_member")
        return False

def format_link_for_button(link: str) -> str:
    """
    Гарантирует, что ссылка имеет формат URL (http/https) для inline-кнопки.
    """
    link = link.strip()
    
    # 1. Если это @username, преобразуем в полную ссылку
    if link.startswith('@'):
        return f"https://t.me/{link.lstrip('@')}"
    
    # 2. Если это короткая ссылка t.me/username, добавляем https://
    if link.lower().startswith('t.me/'):
        return f"https://{link}"
    
    # 3. Если ссылка не начинается с http/https, делаем ее ссылкой на t.me
    if not link.lower().startswith(('http://', 'https://')):
        # Избегаем двойного t.me/ в ссылке
        link = link.replace('t.me/', '').replace('https://t.me/', '')
        link = link.lstrip('/')
        return f"https://t.me/{link}"
    
    # 4. Иначе возвращаем ссылку как есть
    return link


async def safe_edit_or_reply(update_obj: Union[types.CallbackQuery, types.Message], text: str, reply_markup: Optional[Any] = None):
    """Безопасно редактирует сообщение колбэка или отвечает на сообщение."""
    is_callback = isinstance(update_obj, types.CallbackQuery)
    message_obj = update_obj.message if is_callback else update_obj
    
    if not message_obj:
        logger.error("safe_edit_or_reply вызван без message_obj.")
        return

    try:
        if is_callback:
            # Пытаемся редактировать, если текст и/или разметка отличаются
            await message_obj.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            # Если это обычное сообщение, просто отвечаем
            await message_obj.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        # Игнорируем ошибку, если текст/разметка не изменились
        if "message is not modified" in str(e):
            pass
        else:
            logger.warning(f"Ошибка редактирования: {e}. Отправка нового сообщения (Fallback).")
            # Отправка нового сообщения как запасной вариант, если редактирование
