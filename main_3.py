# main_3.py

import asyncio
import logging
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command

# Импорт конфигурации и базы данных
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
from database import db

# Настройка логирования для вывода ошибок в консоль
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------- КОНСТАНТЫ --------------------------

# !!! ЗАМЕНИТЕ ЭТО НА РЕАЛЬНЫЙ USERNAME ВАШЕГО КАНАЛА !!!
REQUIRED_CHANNEL_USERNAME = '@life_in_stile'
REQUIRED_CHANNEL_URL = f"https://t.me/{REQUIRED_CHANNEL_USERNAME.lstrip('@')}"

# -------------------------- ИНИЦИАЛИЗАЦИЯ --------------------------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# --- FSM States (Машина конечных состояний) ---
class ChannelForm(StatesGroup):
    waiting_for_channel_link = State()

# -------------------------- Хелперы и проверки --------------------------

async def is_member(user_id: int, channel_id: int) -> bool:
    """Реальная проверка подписки пользователя на канал."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except TelegramBadRequest as e:
        logger.error(f"Ошибка проверки членства ({channel_id}, {user_id}): {e}")
        return False
    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке членства: {e}")
        return False

# -------------------------- КЛАВИАТУРЫ --------------------------

def get_main_keyboard(is_registered: bool = False):
    """Возвращает основную клавиатуру."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Хочу Подписку (Обмен)", callback_data="start_exchange")
    if is_registered:
        builder.button(text="📊 Мой Канал (Баланс)", callback_data="my_channel_stats")
    # Добавление кнопки регистрации, если канал НЕ зарегистрирован
    else:
        builder.button(text="➕ Зарегистрировать канал", callback_data="register_channel")

    builder.adjust(1)
    return builder.as_markup()

def get_join_main_channel_keyboard():
    """Кнопка для вступления в основной канал."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Подписаться на {REQUIRED_CHANNEL_USERNAME}", url=REQUIRED_CHANNEL_URL)
    builder.button(text="Проверить подписку", callback_data="check_required_sub")
    builder.adjust(1)
    return builder.as_markup()

def get_subscription_keyboard(channel_link: str, channel_id: int):
    """Кнопки для подписки на целевой канал."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подписаться на канал", url=channel_link)
    builder.button(text="Подписка оформлена", callback_data=f"sub_done:{channel_id}")
    builder.adjust(1)
    return builder.as_markup()

# -------------------------- ХЕНДЛЕРЫ ЛОГИКИ БОТА --------------------------

# --- /start и Проверка обязательной подписки ---

@dp.message(Command("start"))
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"

    # !!! ИСПРАВЛЕНИЕ: Очистка FSM при /start, если пользователь застрял !!!
    await state.clear()
    
    # Добавляем/обновляем пользователя
    await db.add_user(user_id, username)

    # 1. Проверяем обязательную подписку
    if not await is_member(user_id, REQUIRED_CHANNEL_ID):
        await message.answer(
            "👋 Добро пожаловать!\n\n"
            "Для начала работы с ботом, пожалуйста, подпишитесь на наш основной канал:",
            reply_markup=get_join_main_channel_keyboard()
        )
        return

    # 2. Если подписан, показываем основную клавиатуру
    channel_info = await db.get_user_channel_info(user_id)
    await message.answer(
        "✅ Вы подписаны на наш канал. Выберите действие:",
        reply_markup=get_main_keyboard(is_registered=channel_info is not None)
    )

@dp.callback_query(F.data == "check_required_sub")
async def process_check_required_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    # 1. Проверяем подписку на обязательный канал
    if await is_member(user_id, REQUIRED_CHANNEL_ID):
        # 2. Проверяем, зарегистрирован ли пользователь
        channel_info = await db.get_user_channel_info(user_id)
        
        await callback.message.edit_text(
            "✅ Вы успешно подписались на наш канал!\n\n"
            "Теперь вы можете обмениваться подписками.",
            reply_markup=get_main_keyboard(is_registered=channel_info is not None)
        )
    else:
        await callback.answer("❌ Подписка не найдена. Пожалуйста, подпишитесь.")
    
    await callback.answer()

# --- FSM: Регистрация канала ---

@dp.callback_query(F.data == "register_channel")
async def register_channel_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    # Проверка, что пользователь не имеет зарегистрированного канала
    if await db.get_user_channel_info(user_id):
        await callback.answer("У вас уже есть зарегистрированный канал.", show_alert=True)
        return
        
    await state.set_state(ChannelForm.waiting_for_channel_link)
    await callback.message.edit_text(
        "📝 **Регистрация канала**\n\n"
        "Отправьте мне публичную ссылку на ваш Telegram-канал (например, `@channel_name` или `https://t.me/channel_name`).\n\n"
        "**Важно:** Бот должен быть **администратором** вашего канала с правом **приглашения пользователей** для проверки подписок.",
        reply_markup=None
    )
    await callback.answer()

@dp.message(ChannelForm.waiting_for_channel_link)
async def process_channel_link(message: types.Message, state: FSMContext):
    link = message.text.strip()
    user_id = message.from_user.id
    
    # Извлечение username из ссылки
    match = re.search(r"@(\w+)|t\.me/(\w+)", link, re.IGNORECASE)
    if not match:
        await message.answer("❌ Некорректный формат ссылки. Используйте `@username` или `https://t.me/username`.")
        # Остаемся в состоянии
        return
    
    channel_username = '@' + (match.group(1) or match.group(2))
    
    try:
        # 1. Проверка, что бот является администратором
        chat_member = await bot.get_chat_member(chat_id=channel_username, user_id=bot.id)
        if chat_member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.answer(
                f"❌ Бот не является администратором в канале {channel_username}. "
                "Пожалуйста, сделайте бота администратором и повторите."
            )
            return # Остаемся в состоянии

        # 2. Получаем полную информацию о канале
        chat = await bot.get_chat(chat_id=channel_username)
        channel_id = chat.id
        
        # 3. Проверка, что владелец сообщения (пользователь) является владельцем канала
        owner_member = await bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        if owner_member.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            await message.answer("❌ Вы не являетесь владельцем или администратором этого канала.")
            return # Остаемся в состоянии

        # 4. Сохранение в БД
        await db.add_channel(user_id, channel_id, channel_username, chat.title)
        
        # 5. Успех: Отправляем ответ и ВЫХОДИМ ИЗ СОСТОЯНИЯ
        await state.clear()
        
        # 6. Уведомление об успешной регистрации
        await message.answer(
            f"✅ Канал <b>{chat.title}</b> ({channel_username}) успешно зарегистрирован!\n\n"
            "Сейчас мы найдем вам первый канал для взаимной подписки...",
            reply_markup=None # Убираем клавиатуру FSM
        )

        # 7. !!! АВТОМАТИЧЕСКИЙ ЗАПУСК ОБМЕНА !!!
        # Генерируем фиктивный CallbackQuery для вызова логики обмена
        await start_exchange_process(
            types.CallbackQuery(
                id="dummy_id", 
                from_user=message.from_user, 
                message=message, 
                data="start_exchange"
            )
        )


    # --- УЛУЧШЕННАЯ ОБРАБОТКА ОШИБОК ---
    except TelegramBadRequest as e:
        logger.error(f"TelegramBadRequest в process_channel_link (link: {link}): {e}")
        await message.answer(
            "❌ Не удалось найти канал по этой ссылке, или нет прав. Убедитесь, что канал **публичный** "
            "и бот является его администратором с правом приглашения."
        )
        # Остаемся в состоянии для повтора
    
    except Exception as e:
        logger.error(f"Неизвестная ошибка в process_channel_link (link: {link}): {e}")
        await message.answer("Произошла неизвестная ошибка при регистрации канала. Попробуйте снова.")
        # Остаемся в состоянии для повтора
        
# --- Логика обмена ---

@dp.callback_query(F.data == "start_exchange")
# !!! ИСПРАВЛЕНИЕ: Добавлен необязательный аргумент callback для возможности вызова из FSM !!!
async def start_exchange_process(callback: types.CallbackQuery): 
    # Если callback пришел из FSM, то message.message - это None, используем message.message.answer()
    # Если callback пришел с кнопки, используем callback.message.edit_text()
    message_to_edit = callback.message if isinstance(callback, types.CallbackQuery) else callback
    user_id = callback.from_user.id
    
    # 1. Проверяем, зарегистрирован ли его канал
    user_channel_info = await db.get_user_channel_info(user_id)
    if not user_channel_info:
        # !!! ИСПРАВЛЕНИЕ: Используем message_to_edit для ответа/редактирования !!!
        if isinstance(callback, types.CallbackQuery):
            await message_to_edit.edit_text(
                "⚠️ **Ваш канал не зарегистрирован.**\n\n"
                "Чтобы начать обмен, сначала зарегистрируйте свой канал:",
                reply_markup=InlineKeyboardBuilder().button(text="➕ Зарегистрировать канал", callback_data="register_channel").as_markup()
            )
            await callback.answer()
        else:
            await message_to_edit.answer(
                "⚠️ **Ваш канал не зарегистрирован.**\n\n"
                "Чтобы начать обмен, сначала зарегистрируйте свой канал:",
                reply_markup=InlineKeyboardBuilder().button(text="➕ Зарегистрировать канал", callback_data="register_channel").as_markup()
            )
        return
        
    # 2. Ищем целевой канал (Channel B)
    target_channel_info = await db.get_target_channel(user_id)
    
    if target_channel_info:
        target_channel_id, target_channel_link, target_channel_title = target_channel_info
        
        # !!! ИСПРАВЛЕНИЕ: Используем message_to_edit для редактирования !!!
        await message_to_edit.edit_text(
            f"✨ **Обмен Подписками**\n\n"
            f"**1. Подпишитесь на этот канал:**\n"
            f"Канал: **{target_channel_title}**\n"
            f"Ссылка: `{target_channel_link}`\n\n"
            "Это **обязательное** условие для получения подписчика взамен. После подписки нажмите 'Подписка оформлена'.",
            reply_markup=get_subscription_keyboard(target_channel_link, target_channel_id)
        )
        
    else:
        # !!! ИСПРАВЛЕНИЕ: Используем message_to_edit для редактирования !!!
        await message_to_edit.edit_text(
            "😴 **Нет доступных каналов для обмена.**\n\n"
            "Все долги на данный момент закрыты. Ваш канал остается в очереди (баланс: 0). "
            "Попробуйте позже или пригласите друзей!",
            reply_markup=get_main_keyboard(is_registered=True)
        )
    
    if isinstance(callback, types.CallbackQuery):
        await callback.answer()

@dp.callback_query(F.data.startswith("sub_done:"))
async def process_subscription_done(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    subscribed_channel_id = int(callback.data.split(":")[1])
    
    # 1. Проверяем, действительно ли пользователь подписался на Channel B
    if not await is_member(user_id, subscribed_channel_id):
        await callback.answer("❌ Мы не видим вашей подписки на целевой канал. Попробуйте еще раз.")
        return

    # 2. Находим свой канал (Channel A), который должен получить подписку взамен
    user_channel_info = await db.get_user_channel_info(user_id)
    if not user_channel_info:
        await callback.answer("Ошибка: Ваш канал не найден. Начните с /start.")
        return

    subscriber_channel_id = user_channel_info[0] # ID канала A

    # 3. Регистрируем подписку и создаем "долг" для Channel A (Транзакция в database.py)
    try:
        await db.register_subscription_and_create_debt(
            subscriber_user_id=user_id, 
            subscribed_channel_id=subscribed_channel_id, 
            subscriber_channel_id=subscriber_channel_id # Канал A
        )
        
        # 4. Уведомление об успехе
        await callback.message.edit_text(
            "🎉 **Подписка засчитана!**\n\n"
            f"Ваш канал **{user_channel_info[2]}** добавлен в очередь. "
            "Вы получите уведомление, как только на него подпишется другой пользователь.",
            reply_markup=get_main_keyboard(is_registered=True)
        )
        await callback.answer("Подписка успешно засчитана!")
    except Exception as e:
        logger.error(f"Ошибка транзакции при sub_done: {e}")
        await callback.answer("❌ Ошибка при регистрации транзакции. Попробуйте снова.")


# --- Статистика канала ---

@dp.callback_query(F.data == "my_channel_stats")
async def show_my_channel_stats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    channel_info = await db.get_user_channel_info(user_id)
    if not channel_info:
        await callback.answer("Ваш канал не зарегистрирован.", show_alert=True)
        return

    channel_id, link, title, subs_needed = channel_info

    text = (
        f"📊 **Статистика вашего канала**\n\n"
        f"Название: **{title}**\n"
        f"Ссылка: `{link}`\n"
        f"Баланс долга: **{subs_needed}**\n\n"
        f"**Долг {subs_needed}** означает, что столько подписчиков должен получить ваш канал."
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_main_keyboard(is_registered=True)
    )
    await callback.answer()

# -------------------------- ФОНОВАЯ ЗАДАЧА --------------------------

async def check_for_unsubs(bot_instance: Bot, db_instance: db):
    """Фоновая задача, которая периодически проверяет активные подписки."""
    while True:
        # Пауза между проверками (30 минут)
        await asyncio.sleep(30 * 60)
        logger.info("Запуск фоновой проверки отписок...")
        
        # Получаем активные подписки
        rows = await db_instance._fetch(
            """
            SELECT
                s.id,
                s.subscriber_user_id,
                s.subscribed_channel_id,
                c.owner_id,
                s.channel_that_owes_id
            FROM subscriptions s
            JOIN channels c ON s.subscribed_channel_id = c.channel_id
            WHERE s.is_active = TRUE
            LIMIT 50
            """
        )

        for row in rows:
            sub_id = row[0]
            subscriber_id = row[1]
            subscribed_channel_id = row[2]
            owner_id_of_subscribed = row[3]
            channel_that_owes_id = row[4]

            # Проверка отписки
            if not await is_member(subscriber_id, subscribed_channel_id):
                logger.warning(f"Обнаружена отписка: sub_id={sub_id}, user={subscriber_id}")

                # 1. Уведомление владельца канала, от которого отписались 
                try:
                    await bot_instance.send_message(
                        chat_id=owner_id_of_subscribed,
                        text=f"⚠️ **ВНИМАНИЕ! ОТПИСКА!**\n\n"
                             f"Пользователь с ID **{subscriber_id}** **отписался** от вашего канала..."
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление владельцу {owner_id_of_subscribed}: {e}")
                
                # 2. Уведомление владельца канала-должника
                owes_info = await db_instance.get_channel_owner_info(channel_that_owes_id)
                owner_of_owes_id = owes_info[0] if owes_info else None
                
                if owner_of_owes_id:
                    try:
                        await bot_instance.send_message(
                            chat_id=owner_of_owes_id,
                            text=f"❌ **АННУЛИРОВАНИЕ ДОЛГА!**\n\n"
                                 f"Ваш **баланс** (долг) за этот обмен был **аннулирован** и будет **уменьшен на 1**."
                        )
                    except Exception as e:
                        logger.error(f"Не удалось отправить уведомление должнику {owner_of_owes_id}: {e}")
                        
                    # 3. Уменьшение счетчика долга для Канала А
                    await db_instance._execute(
                        """UPDATE channels 
                           SET subscribers_needed = subscribers_needed - 1 
                           WHERE channel_id = %s""",
                        channel_that_owes_id
                    )

                # 4. Установка статуса подписки как неактивной
                await db_instance._execute(
                    "UPDATE subscriptions SET is_active = FALSE WHERE id = %s",
                    sub_id
                )
