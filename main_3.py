import asyncio
import logging
import re
import os
import contextlib
from typing import Union, Optional, Any, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from datetime import timedelta

# REDIS / STORAGE
from aiogram.fsm.storage.redis import RedisStorage, Redis

# Конфиг и БД
# ************************************************************
# Убедитесь, что эти файлы существуют и содержат необходимые переменные/классы
from config_1 import BOT_TOKEN, REQUIRED_CHANNEL_ID
from database import db, NotFoundError, Database
# ************************************************************

# --- КОНФИГУРАЦИЯ И ЗАПУСК ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# CONSTANTS
BOT_SUPPORT_CHANNEL_USERNAME = '@life_in_stile'
BOT_SUPPORT_CHANNEL_URL = f"https://t.me/{BOT_SUPPORT_CHANNEL_USERNAME.lstrip('@')}"
CHECK_INTERVAL_SECONDS = 300  # 5 минут

# Инициализация Redis для FSM
REDIS_DSN = os.getenv("REDIS_DSN")

if REDIS_DSN:
    redis_storage = Redis.from_url(REDIS_DSN)
else:
    redis_storage = Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=os.getenv("REDIS_PORT", 6379)
    )

storage = RedisStorage(redis_storage)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# --- FSM STATES ---
class ChannelRegistration(StatesGroup):
    waiting_for_channel = State()
    confirm_registration_without_admin = State() # New state for handling non-admin channels


# --- KEYBOARDS ---
def get_main_menu_keyboard(has_channels: bool) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Получить Подписчика", callback_data="start_get_sub")
    builder.button(text="📋 Мои Каналы", callback_data="show_my_channels")
    builder.button(text="ℹ️ Поддержка", url=BOT_SUPPORT_CHANNEL_URL)
    builder.adjust(2)
    return builder.as_markup()


def get_back_button() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в Меню", callback_data="main_menu")
    return builder.as_markup()


def get_channel_control_keyboard(channels) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        # Display debt only if channel can receive subs
        debt_info = f" ({channel['subscribers_needed']}👤)" if channel.get('can_receive_subs', False) else " (только просмотр)"
        button_text = f"⚙️ {channel['title']}{debt_info}"
        builder.button(text=button_text, callback_data=f"manage_channel_{channel['channel_id']}")

    if any(c.get('can_receive_subs', False) for c in channels): # Only show delete if there's at least one managed channel
        builder.button(text="❌ Удалить Канал", callback_data="delete_channel_start")

    builder.button(text="➕ Зарегистрировать Новый", callback_data="register_new_channel_start")
    builder.button(text="⬅️ Назад в Меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_sub_keyboard(channel_link: str, channel_id: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Подписаться", url=channel_link)
    builder.button(text="✅ Проверить подписку", callback_data=f"check_sub_{channel_id}")
    builder.button(text="➡️ Пропустить", callback_data="skip_sub")
    builder.adjust(1, 2)
    return builder.as_markup()


def get_required_sub_keyboard(link: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подписаться на @life_in_stile", url=link)
    builder.button(text="Проверить подписку", callback_data="check_required_sub")
    builder.adjust(1)
    return builder.as_markup()

def get_confirm_no_admin_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Продолжить без админских прав", callback_data="register_no_admin")
    builder.button(text="↩️ Вернуться в главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


# --- ФУНКЦИЯ КЛАВИАТУРЫ (ASYNC) ---
async def get_ask_mutual_sub_keyboard(from_user_id: int,
                                      from_channel_id: int,
                                      target_channel_id: int) -> types.InlineKeyboardMarkup:
    """
    Клавиатура для владельца target_channel_id:
    кнопка «Подписаться в ответ» с deeplinkом вида
    start=mutual_<from_user_id>_<from_channel_id>_<target_channel_id>
    """
    builder = InlineKeyboardBuilder()
    # Payload для обратной подписки
    payload = f"mutual_{from_user_id}_{from_channel_id}_{target_channel_id}"

    bot_user = await bot.get_me()

    # URL СФОРМИРОВАН КОРРЕКТНО для deep-link
    builder.button(text="🔁 Подписаться в ответ",
                   url=f"https://t.me/{bot_user.username}?start={payload}")
    return builder.as_markup()


# --- CHECK / VALIDATION HELPERS ---
async def is_member_of_required_channel(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        logger.exception("Ошибка при проверке подписки на обязательный канал")
        return False


async def is_bot_admin_in_channel(bot: Bot, chat_id: Union[int, str]) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
            return False
        # Проверка прав на приглашение (can_invite_users)
        if member.status == ChatMemberStatus.ADMINISTRATOR and not member.can_invite_users:
            return False
        return True
    except TelegramBadRequest as e:
        logger.warning(f"Ошибка проверки прав бота в канале {chat_id}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка проверки прав бота: {e}")
        return False


async def get_channel_details_and_admin_status(bot: Bot, input_text: str) -> Optional[Dict[str, Any]]:
    channel_identifier = None
    match = re.search(
        r'(?:t\.me/|@|t\.me/joinchat/|telegram\.me/joinchat/|t\.me/\+)?([a-zA-Z0-9_]+)$',
        input_text
    )

    if match:
        channel_identifier = match.group(1)
        if not channel_identifier.startswith('@') and not channel_identifier.isdigit():
            channel_identifier = '@' + channel_identifier
    elif input_text.startswith('-100') and input_text[1:].isdigit():
        channel_identifier = int(input_text)
    elif input_text.startswith('@'):
        channel_identifier = input_text
    else:
        return None

    try:
        chat = await bot.get_chat(chat_id=channel_identifier)
        if chat.type not in ['channel', 'supergroup']:
            return None

        is_admin = await is_bot_admin_in_channel(bot, chat.id)

        if chat.username:
            link = f"https://t.me/{chat.username.lstrip('@')}"
        else:
            # For private channels without a username, the link is usually t.me/c/CHANNEL_ID/MESSAGE_ID
            # This link might not always be directly joinable. Provide general channel ID link.
            link = f"https://t.me/c/{str(chat.id).replace('-100', '')}" # This is a direct link to messages inside the channel
            # A better approach for joinable link would be to create an invite link if bot has rights.
            # For now, let's stick to this as it's what was present.

        return {
            'channel_id': chat.id,
            'link': link,
            'username': chat.username,
            'title': chat.title,
            'is_bot_admin': is_admin, # Add admin status
            'chat_type': chat.type # Might be useful
        }

    except TelegramBadRequest as e:
        logger.error(f"TelegramBadRequest in get_channel_details_and_admin_status (Input: {input_text}): {e}")
        return None
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка в get_channel_details_and_admin_status: {e}")
        return None


# --- HANDLERS ---

# --- УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ /start И DEEPLINK ---
@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext, command: CommandObject):
    await db.add_user(message.from_user.id, message.from_user.username)
    user_id = message.from_user.id

    # 1. Сначала проверяем, является ли это deep-link для ответной подписки
    if command.args and command.args.startswith('mutual_'):
        await state.clear()

        # Парсим payload: mutual_<from_user_id>_<from_channel_id>_<target_channel_id>
        try:
            parts = command.args.split('_')
            if len(parts) != 4:
                raise ValueError("Неверное количество аргументов в deeplink.")

            initial_subscriber_user_id = int(parts[1])
            channel_to_subscribe_id = int(parts[2])
            owner_channel_id_to_fulfill = int(parts[3])

        except (IndexError, ValueError) as e:
            logger.error(f"Ошибка парсинга deeplink: {e} | Payload: {command.args}")
            await message.answer("❌ Ошибка в ссылке для ответной подписки. Свяжитесь с поддержкой.",
                                 reply_markup=get_back_button())
            return

        # Проверка, что текущий пользователь (user_id) является владельцем owner_channel_id_to_fulfill
        owner_channel_info = await db.get_channel_info_by_channel_id(owner_channel_id_to_fulfill)
        if not owner_channel_info or owner_channel_info.get('owner_id') != user_id:
            await message.answer("❌ Эта ссылка предназначена только для владельца канала. Операция отменена.",
                                 reply_markup=get_back_button())
            return

        # Получаем информацию о канале, на который нужно подписаться (channel_to_subscribe_id)
        channel_to_subscribe_info = await db.get_channel_info_by_channel_id(channel_to_subscribe_id)
        if not channel_to_subscribe_info:
            await message.answer("❌ Канал, на который нужно подписаться, не найден.",
                                 reply_markup=get_back_button())
            return

        # Отправляем пользователю задание на подписку
        await message.answer(
            f"➡️ **Ответное задание:**\n"
            f"Для выполнения ответной подписки, подпишитесь на канал **{channel_to_subscribe_info['title']}**.\n\n"
            f"После проверки ваш канал **{owner_channel_info['title']}** погасит свой долг.",
            reply_markup=get_sub_keyboard(channel_to_subscribe_info['link'], channel_to_subscribe_info['channel_id'])
        )

        # Сохраняем данные для проверки подписки в FSM
        await state.update_data(
            target_channel_id=channel_to_subscribe_info['channel_id'],
            channel_to_receive_sub_id=owner_channel_id_to_fulfill,
            is_mutual_sub=True,
            can_user_channel_receive_subs=True # A mutual sub implies the channel can receive subs
        )
        return

    # 2. Если это обычный /start (deep-link отсутствует)
    await state.clear()
    await message.answer("...") # Removing ReplyKeyboardRemove for cleaner UX

    if not await is_member_of_required_channel(bot, user_id):
        await message.answer(
            f"👋 Добро пожаловать!\n\nДля начала работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).",
            reply_markup=get_required_sub_keyboard(BOT_SUPPORT_CHANNEL_URL)
        )
        return

    user_channels = await db.get_user_channels_info(user_id)
    has_channels = len(user_channels) > 0
    await message.answer(
        "✅ Вы подписаны на наш канал. Выберите действие:",
        reply_markup=get_main_menu_keyboard(has_channels)
    )


@dp.callback_query(F.data == "check_required_sub")
async def process_check_required_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await is_member_of_required_channel(bot, user_id):
        user_channels = await db.get_user_channels_info(user_id)
        has_channels = len(user_channels) > 0
        await callback.message.edit_text(
            "✅ Вы успешно подписались на наш канал!\n\n"
            "Теперь вы можете обмениваться подписками. Выберите действие:",
            reply_markup=get_main_menu_keyboard(has_channels)
        )
        await callback.answer()
    else:
        await callback.answer("❌ Подписка не найдена. Пожалуйста, подпишитесь.", show_alert=True)


@dp.callback_query(F.data == "main_menu")
async def process_main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    user_channels = await db.get_user_channels_info(callback.from_user.id)
    has_channels = len(user_channels) > 0
    await callback.message.edit_text(
        "👋 Выберите действие в меню:",
        reply_markup=get_main_menu_keyboard(has_channels)
    )


@dp.callback_query(F.data == "show_my_channels")
async def show_my_channels(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    await callback.answer()

    if not await is_member_of_required_channel(bot, user_id):
        await callback.message.edit_text(
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).",
            reply_markup=get_required_sub_keyboard(BOT_SUPPORT_CHANNEL_URL)
        )
        return

    channels = await db.get_user_channels_info(user_id) # This should fetch all user channels
    count = len(channels)
    text = f"🗂️ **Ваши зарегистрированные каналы ({count})**:\n"
    if count == 0:
        text += "У вас пока нет зарегистрированных каналов. Нажмите '➕ Зарегистрировать Новый' ниже."
    else:
        text += "Выберите канал для управления или действия:"
    keyboard = get_channel_control_keyboard(channels)
    await callback.message.edit_text(text, reply_markup=keyboard)


@dp.callback_query(F.data.in_(["register_new_channel_start", "delete_channel_start"]))
async def start_registering_channel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id

    if not await is_member_of_required_channel(bot, user_id):
        await callback.message.edit_text(
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).",
            reply_markup=get_required_sub_keyboard(BOT_SUPPORT_CHANNEL_URL)
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "Отправьте мне ссылку (@username), ID канала (например, -100...) или публичную ссылку-приглашение на ваш **публичный** канал.\n\n"
        "**Важно:** Для полноценного участия в системе обмена подписками (чтобы ваш канал мог получать подписчиков) "
        "бот должен быть **администратором** в этом канале **с правом на приглашение пользователей**.\n"
        "Это позволяет боту автоматически проверять подписки/отписки и управлять очередью вашего канала.",
        reply_markup=get_back_button()
    )
    await state.set_state(ChannelRegistration.waiting_for_channel)
    await callback.answer()


@dp.message(ChannelRegistration.waiting_for_channel)
async def process_channel_id_or_username(message: types.Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    input_text = message.text
    channel_details = await get_channel_details_and_admin_status(bot, input_text)

    if channel_details is None:
        await message.answer(
            "❌ Некорректный формат адреса, канал не найден. Пожалуйста, убедитесь, что вы отправляете "
            "правильную ссылку (@username, ID канала или ссылку-приглашение).",
            reply_markup=get_back_button()
        )
        return

    channel_id = channel_details['channel_id']
    channel_title = channel_details['title']
    channel_link = channel_details['link']
    is_bot_admin = channel_details['is_bot_admin']

    if await db.is_channel_registered_by_other(channel_id, user_id): # Assuming this checks globally, not just for user's own 'can_receive_subs' channels
        await state.clear() # Clear state after handling the input
        await message.answer(
            "❌ Этот канал уже зарегистрирован другим пользователем. Если это ошибка, свяжитесь с поддержкой.",
            reply_markup=get_main_menu_keyboard(True) # Assuming user has other channels
        )
        return

    existing_channel = await db.get_channel_info_by_channel_id(channel_id) # This checks if the channel is registered by *this* user
    if existing_channel:
        await state.clear() # Clear state after handling the input
        await message.answer(
            f"⚠️ Канал **{channel_title}** уже был вами зарегистрирован.\n"
            "Нажмите '📋 Мои Каналы' для управления.",
            reply_markup=get_main_menu_keyboard(True)
        )
        return

    if not is_bot_admin:
        # If bot is not admin, ask user how to proceed
        await state.update_data(temp_channel_details=channel_details) # Store for next state
        await state.set_state(ChannelRegistration.confirm_registration_without_admin)
        await message.answer(
            f"⚠️ **Внимание!** Бот не является администратором в канале **{channel_title}** "
            f"или не имеет права на приглашение пользователей.\n\n"
            f"**Почему это важно?**\n"
            f"Для полноценного участия в системе обмена подписками (чтобы ваш канал мог получать подписчиков) "
            f"бот должен быть администратором в вашем канале **с правом на приглашение пользователей**.\n"
            f"Это позволяет боту:\n"
            f"  - Автоматически проверять подписки и отписки.\n"
            f"  - Уведомлять вас о новых подписчиках и отписавшихся.\n"
            f"  - Корректно управлять очередью вашего канала на получение подписчиков.\n\n"
            f"Вы можете продолжить регистрацию, но без админских прав ваш канал **не сможет получать подписчиков** "
            f"и бот **не сможет отслеживать ваши подписки и отписки** для этого канала.\n\n"
            f"Что вы хотите сделать?",
            reply_markup=get_confirm_no_admin_keyboard()
        )
        return

    # If bot is admin, proceed with full registration
    # DB assumption: add_channel now takes can_receive_subs parameter
    if await db.add_channel(user_id, channel_id, channel_link, channel_title, can_receive_subs=True):
        await state.clear()
        await message.answer(
            f"✅ Канал **{channel_title}** успешно добавлен в систему!\n"
            "Бот является администратором, ваш канал может получать подписчиков и его долг будет автоматически управляться.\n"
            "Нажмите '➕ Получить Подписчика', чтобы начать работу.",
            reply_markup=get_main_menu_keyboard(True)
        )
    else:
        await state.clear()
        await message.answer(
            "❌ Произошла ошибка при регистрации канала в базе данных. Попробуйте позже.",
            reply_markup=get_back_button()
        )


@dp.callback_query(ChannelRegistration.confirm_registration_without_admin, F.data == "register_no_admin")
async def confirm_register_no_admin(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    state_data = await state.get_data()
    channel_details = state_data.get('temp_channel_details')

    if not channel_details:
        await callback.message.edit_text("❌ Ошибка: Данные канала не найдены.", reply_markup=get_back_button())
        await state.clear()
        await callback.answer()
        return

    channel_id = channel_details['channel_id']
    channel_title = channel_details['title']
    channel_link = channel_details['link']

    # DB assumption: add_channel now takes can_receive_subs parameter
    if await db.add_channel(user_id, channel_id, channel_link, channel_title, can_receive_subs=False):
        await state.clear()
        await callback.message.edit_text(
            f"✅ Канал **{channel_title}** зарегистрирован.\n"
            f"**Важно:** Поскольку бот не является администратором, ваш канал **не будет получать подписчиков** "
            f"и бот не сможет автоматически проверять подписки/отписки для этого канала. "
            f"Вы сможете использовать его для самостоятельного поиска каналов для подписки, но без ответного обмена.\n\n"
            "Нажмите '➕ Получить Подписчика', чтобы найти каналы для подписки.",
            reply_markup=get_main_menu_keyboard(True)
        )
    else:
        await state.clear()
        await callback.message.edit_text(
            "❌ Произошла ошибка при регистрации канала в базе данных. Попробуйте позже.",
            reply_markup=get_back_button()
        )
    await callback.answer()


@dp.callback_query(F.data == "start_get_sub")
async def start_get_sub(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    await callback.answer()

    if not await is_member_of_required_channel(bot, user_id):
        await callback.message.edit_text(
            f"Для работы необходимо подписаться на наш основной канал: [Канал поддержки]({BOT_SUPPORT_CHANNEL_URL}).",
            reply_markup=get_required_sub_keyboard(BOT_SUPPORT_CHANNEL_URL)
        )
        return

    # DB assumption: get_user_channels_info can filter by can_receive_subs
    user_channels_for_receiving_subs = await db.get_user_channels_info(user_id, can_receive_subs=True)
    channel_to_receive_sub = None
    if user_channels_for_receiving_subs:
        # DB assumption: get_user_channel_with_highest_debt only returns channels that can receive subs
        channel_to_receive_sub = await db.get_user_channel_with_highest_debt(user_id)

    state_data = await state.get_data()
    excluded_ids = state_data.get('excluded_channel_ids', [])
    target_channel = await db.get_target_channel(user_id, excluded_ids)

    if not target_channel:
        await callback.message.edit_text(
            "🎉 Поздравляем! Вы подписались на все доступные каналы.\n"
            "Подождите, пока в системе появятся новые каналы.",
            reply_markup=get_main_menu_keyboard(True)
        )
        await state.clear()
        return

    # Determine if the user has *any* channel that can receive subs
    can_user_channel_receive_subs = bool(channel_to_receive_sub)

    await state.update_data(
        target_channel_id=target_channel['channel_id'],
        # Store the ID only if a suitable channel was found, otherwise None
        channel_to_receive_sub_id=channel_to_receive_sub['channel_id'] if channel_to_receive_sub else None,
        is_mutual_sub=False,
        can_user_channel_receive_subs=can_user_channel_receive_subs # Store this flag
    )

    message_text = f"➡️ **Ваше задание:**\n"
    message_text += f"Подпишитесь на канал **{target_channel['title']}** (долг: {target_channel['subscribers_needed']}👤).\n\n"

    if can_user_channel_receive_subs:
        current_channel_debt = channel_to_receive_sub['subscribers_needed']
        message_text += f"После проверки подписки, ваш канал **{channel_to_receive_sub['title']}** получит +1 в очередь на подписку (долг: {current_channel_debt}👤)."
    else:
        message_text += (
            f"**Внимание!** Вы можете подписаться на этот канал, но поскольку у вас нет зарегистрированного "
            f"канала с админскими правами (или ваш канал не был настроен для получения подписчиков), "
            f"ваш канал **не получит ответного подписчика**.\n"
            f"Чтобы получать подписчиков в ответ, пожалуйста, зарегистрируйте свой канал и "
            f"сделайте бота администратором в нем с правом на приглашение пользователей." # Clearer instruction
        )

    await callback.message.edit_text(
        message_text,
        reply_markup=get_sub_keyboard(target_channel['link'], target_channel['channel_id'])
    )


@dp.callback_query(F.data.startswith("check_sub_"))
async def check_subscription(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        # Извлекаем ID канала, на который пользователь должен был подписаться
        target_channel_id = int(callback.data.split('_')[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    state_data = await state.get_data()
    # Проверка, что нажата кнопка для текущей задачи
    if state_data.get('target_channel_id') != target_channel_id:
        await callback.answer("Ошибка: Проверьте, что вы нажали кнопку для текущей задачи.", show_alert=True)
        # Перенаправляем в меню для сброса состояния
        await callback.message.edit_text(
            "⚠️ Ваша текущая задача устарела. Нажмите '➕ Получить Подписчика' снова.",
            reply_markup=get_main_menu_keyboard(True)
        )
        await state.clear()
        return

    await callback.answer("Проверяю подписку...", show_alert=False)
    is_subscribed = False
    try:
        member = await bot.get_chat_member(target_channel_id, user_id)
        is_subscribed = member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        logger.exception(f"Ошибка при проверке подписки пользователя {user_id} на канал {target_channel_id}")
        is_subscribed = False

    if not is_subscribed:
        await callback.message.edit_text(
            "❌ Подписка не найдена. Пожалуйста, подпишитесь и повторите проверку.",
            reply_markup=callback.message.reply_markup
        )
        return

    # --- ЛОГИКА ОБРАБОТКИ УСПЕШНОЙ ПОДПИСКИ ---
    channel_that_owes_id = state_data.get('channel_to_receive_sub_id') # Can be None
    is_mutual_sub = state_data.get('is_mutual_sub', False)
    can_user_channel_receive_subs = state_data.get('can_user_channel_receive_subs', False)

    # ПЫТАЕМСЯ ПОЛУЧИТЬ ИНФОРМАЦИЮ О КАНАЛАХ
    target_ch = await db.get_channel_info_by_channel_id(target_channel_id) # Канал, на который подписались
    my_ch = None
    if channel_that_owes_id: # Only attempt to get my_ch if there's an ID
        my_ch = await db.get_channel_info_by_channel_id(channel_that_owes_id) # Канал, который получает или гасит долг

    # ************************************************************
    # ИСПРАВЛЕНИЕ КРИТИЧЕСКОЙ ОШИБКИ: Проверка на отсутствие канала
    # ************************************************************
    if not target_ch:
        await state.clear()
        await callback.message.edit_text(
            f"❌ **Ошибка!** Целевой канал (ID: `{target_channel_id}`) не найден в системе. "
            f"Возможно, он был удален.\nНажмите '📋 Мои Каналы' для обновления статуса.",
            reply_markup=get_main_menu_keyboard(True)
        )
        await callback.answer("❌ Канал удален или не найден.", show_alert=True)
        return
    # ************************************************************
    # КОНЕЦ ИСПРАВЛЕНИЯ
    # ************************************************************

    success_text = ""
    new_debt = 0 # Initialize to avoid UnboundLocalError

    try:
        if is_mutual_sub:
            # 1. ОТВЕТНАЯ ПОДПИСКА (ПОГАШЕНИЕ ДОЛГА)
            if not my_ch: # my_ch must exist for mutual sub
                raise NotFoundError("Канал, который должен погасить долг, не найден для взаимной подписки.")

            # db.fulfill_debt(channel_id) гасит 1 долг
            new_debt = await db.fulfill_debt(channel_that_owes_id)

            success_text = (
                f"✅ **Поздравляем!** Ответная подписка на канал **{target_ch['title']}** засчитана.\n\n"
                f"Ваш канал **{my_ch['title']}** (ID: {channel_that_owes_id}) погасил 1 единицу долга. "
                f"Текущий долг: **{new_debt}**👤."
            )

        elif can_user_channel_receive_subs and my_ch: # 2. СТАНДАРТНАЯ ПОДПИСКА (СОЗДАНИЕ ДОЛГА)
            # my_ch must exist here if can_user_channel_receive_subs is True
            new_debt = await db.register_subscription_and_create_debt(
                subscriber_user_id=user_id,
                subscribed_channel_id=target_channel_id,
                channel_that_owes_id=channel_that_owes_id
            )

            success_text = (
                f"✅ **Поздравляем!** Ваша подписка на канал **{target_ch['title']}** успешно засчитана.\n\n"
                f"Ваш канал **{my_ch['title']}** теперь в очереди на получение **+1** подписчика (Текущий долг: **{new_debt}**👤)."
            )

            # УВЕДОМЛЕНИЕ ВЛАДЕЛЬЦА ТОЛЬКО ПРИ СТАНДАРТНОЙ ПОДПИСКЕ (если бот админ в канале my_ch)
            owner_info = await db.get_channel_owner_info(target_channel_id)
            if owner_info:
                try:
                    # Владелец канала, на который подписались, получает уведомление
                    kb = await get_ask_mutual_sub_keyboard(
                        from_user_id=user_id,
                        from_channel_id=channel_that_owes_id,
                        target_channel_id=target_channel_id
                    )
                    await bot.send_message(
                        chat_id=owner_info['owner_id'],
                        text=(f"🔔 У вашего канала **{target_ch['title']}** новый подписчик по обмену!\n\n"
                              f"Пожалуйста, подпишитесь на канал **{my_ch['title']}** в ответ."),
                        reply_markup=kb
                    )
                except Exception:
                    logger.exception("Не удалось уведомить владельца")
        else: # User subscribed, but their channel cannot receive subs (bot is not admin in their channel for exchange)
            # DB assumption: register_manual_subscription method exists to log this subscription
            await db.register_manual_subscription(user_id, target_channel_id)

            success_text = (
                f"✅ **Поздравляем!** Вы успешно подписались на канал **{target_ch['title']}**.\n\n"
                f"**Внимание!** Поскольку у вас нет зарегистрированного канала с админскими правами, "
                f"владелец канала **{target_ch['title']}** не будет автоматически уведомлен о вашей подписке, "
                f"и ваш канал **не получит ответного подписчика** в рамках системы обмена.\n"
                f"Для участия в полноценном обмене, пожалуйста, зарегистрируйте свой канал, сделав бота администратором." # Clearer explanation
            )
            # No notification to owner of target_ch here, as we can't verify/track reciprocal action.

    except NotFoundError:
        await callback.message.edit_text(
            "❌ Ошибка! Ваш канал-получатель подписки не найден. Нажмите '📋 Мои Каналы' для проверки.",
            reply_markup=get_back_button()
        )
        return
    except Exception:
        logger.exception("Критическая ошибка при регистрации/погашении долга или ручной подписке")
        await callback.message.edit_text(
            "❌ Произошла критическая ошибка базы данных при регистрации подписки. Повторите попытку позже.",
            reply_markup=get_back_button()
        )
        return

    await state.clear()

    await callback.message.edit_text(
        success_text,
        reply_markup=get_main_menu_keyboard(True)
    )
    await callback.answer()


@dp.callback_query(F.data == "skip_sub")
async def skip_subscription(callback: types.CallbackQuery, state: FSMContext):
    state_data = await state.get_data()
    target_channel_id = state_data.get('target_channel_id')

    if target_channel_id is None:
        await callback.answer("Ошибка: Задача для пропуска не найдена.", show_alert=True)
        # Optionally navigate to main menu if state is truly broken
        await callback.message.edit_text(
            "⚠️ Ваша текущая задача устарела. Нажмите '➕ Получить Подписчика' снова.",
            reply_markup=get_main_menu_keyboard(True)
        )
        return

    excluded_ids = state_data.get('excluded_channel_ids', [])
    if target_channel_id not in excluded_ids:
        excluded_ids.append(target_channel_id)
        await state.update_data(excluded_channel_ids=excluded_ids)

    await callback.answer("Канал пропущен.", show_alert=False)
    # Удаляем старое сообщение и вызываем новую задачу
    await callback.message.delete()
    await start_get_sub(callback, state)


# --- ФОНОВАЯ ПРОВЕРКА ОТПИСОК (TASK) ---
async def check_for_unsubs(bot: Bot, db: Database):
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        logger.info("Запуск фоновой проверки подписок...")

        try:
            # DB assumption: get_active_subscriptions_to_check only returns subscriptions that created debt
            # (i.e., where channel_that_owes_id is NOT NULL and my_ch.can_receive_subs is TRUE at the time of check)
            subscriptions_to_check = await db.get_active_subscriptions_to_check(limit=50)
            if not subscriptions_to_check:
                logger.info("Нет активных подписок для проверки.")
                continue

            for sub in subscriptions_to_check:
                sub_id = sub['id']
                subscriber_user_id = sub['subscriber_user_id']
                subscribed_channel_id = sub['subscribed_channel_id']
                channel_that_owes_id = sub['channel_that_owes_id']
                owner_id_of_subscribed = sub['owner_id'] # Owner of the channel the user subscribed TO

                try:
                    is_subscribed = False
                    try:
                        member = await bot.get_chat_member(subscribed_channel_id, subscriber_user_id)
                        is_subscribed = member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
                    except TelegramBadRequest:
                        logger.warning(f"Ошибка при проверке: Канал {subscribed_channel_id} не найден. Считаем отпиской.")
                        is_subscribed = False
                    except Exception:
                        logger.exception(f"Ошибка при проверке подписки (sub_id: {sub_id})")
                        continue

                    if not is_subscribed:
                        logger.warning(f"Обнаружена отписка! user: {subscriber_user_id}, channel: {subscribed_channel_id}")

                        # db.fulfill_debt(channel_id) используется для погашения долга при отписке
                        try:
                            # This will reduce the 'subscribers_needed' for channel_that_owes_id
                            new_debt = await db.fulfill_debt(channel_that_owes_id)
                        except NotFoundError:
                             # Если канал, который должен был получить штраф, уже удален, просто пропускаем
                            logger.warning(f"Канал-должник {channel_that_owes_id} не найден при попытке штрафа. Пропуск.")
                            # If the channel that owes was deleted, then its debt implicitly goes away for the system.
                            # Mark the subscription as inactive / handled in DB for this unsubs event.
                            # DB assumption: a method to mark subscription as inactive exists.
                            await db.mark_subscription_inactive(sub_id)
                            continue

                        try:
                            # Уведомление подписчика
                            await bot.send_message(
                                chat_id=subscriber_user_id,
                                text=f"⚠️ **Внимание!** Обнаружена ваша отписка от канала **{subscribed_channel_id}** (ID).\n"
                                     f"В качестве штрафа, ваш канал (ID: **{channel_that_owes_id}**) потерял 1 место в очереди на получение подписчиков. "
                                     f"Текущий долг: **{new_debt}**👤."
                            )
                        except TelegramForbiddenError:
                            logger.warning(f"Не удалось уведомить подписчика об отписке. Чат заблокирован {subscriber_user_id}.")
                        except Exception:
                            logger.exception("Не удалось уведомить подписчика об отписке")

                        try:
                            # Уведомление владельца канала, от которого отписались
                            target_channel_info = await db.get_channel_info_by_channel_id(subscribed_channel_id)
                            target_channel_title = target_channel_info['title'] if target_channel_info else f"канал с ID {subscribed_channel_id}"
                            await bot.send_message(
                                chat_id=owner_id_of_subscribed,
                                text=f"🔔 **Внимание!** Один из подписчиков, которого вы получили по обмену "
                                     f"отписался от вашего канала **{target_channel_title}**.\n"
                                     f"Соответствующий 'долг' был аннулирован."
                            )
                        except TelegramForbiddenError:
                            logger.warning(f"Не удалось уведомить владельца канала об отписке. Чат заблокирован {owner_id_of_subscribed}.")
                        except Exception:
                            logger.exception("Не удалось уведомить владельца канала об отписке")

                        # Mark the subscription as inactive after processing the unsubscription
                        # DB assumption: a method to mark subscription as inactive exists.
                        await db.mark_subscription_inactive(sub_id)

                except Exception:
                    logger.exception(f"Ошибка при обработке строки отписок (sub_id: {sub_id})")

        except asyncio.CancelledError:
            logger.info("Фоновая задача check_for_unsubs отменена.")
            break
        except Exception:
            logger.exception("Фоновая задача завершилась с ошибкой.")


# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ---
# Обратите внимание: в aiogram 3.x, обработчик ошибок должен принимать event
@dp.errors()
async def errors_handler(exception: Exception, event: types.error_event.ErrorEvent):
    logger.exception(f"Критическая ошибка в обработке Update: {exception}", exc_info=exception)


# --- RUN ---
async def main():
    await db.connect()
    bg_task = None
    if db.pool:
        bg_task = asyncio.create_task(check_for_unsubs(bot, db))
        logger.info("Запущена фоновая задача check_for_unsubs.")

    logger.info("Бот запущен.")
    try:
        # Убедитесь, что вы используете dp.start_polling(bot) для локальной работы
        # или aiogram.methods.SetWebhook/run_webhook для сервера (как в ваших логах)
        await dp.start_polling(bot)
    finally:
        if bg_task:
            bg_task.cancel()
            with contextlib.suppress(Exception):
                await bg_task
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
