import logging
import asyncio
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher
# !!! ДОБАВЛЕН ИМПОРТ TelegramRetryAfter !!!
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Update
from contextlib import asynccontextmanager 

# Импорт конфигурации и логики бота
from config_1 import (
    BOT_TOKEN, 
    BASE_WEBHOOK_URL,
    WEBHOOK_SECRET
)
from main_3 import dp, bot
from database import db
from main_3 import check_for_unsubs

# --- Настройка логирования ---
logger = logging.getLogger(__name__)

# --- Основные настройки Webhook ---
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
# BASE_WEBHOOK_URL будет установлен Render как $RENDER_EXTERNAL_URL
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}" 

# -------------------------- Lifespan Context Manager (Startup/Shutdown) --------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управляет событиями запуска (startup) и остановки (shutdown) сервера.
    Включает логику повторных попыток для установки Webhook.
    """
    logger.info("--- [STARTUP] Запуск сервера FastAPI на Render ---")

    # 1. Подключение к БД 
    if await db.connect():
        logger.info("База данных MySQL подключена.")
    else:
        # Если БД критически не подключена, мы все равно позволяем серверу запуститься,
        # но фоновая задача и операции БД будут зависеть от проверки db.pool.
        logger.critical("🚨 КРИТИЧЕСКАЯ ОШИБКА: Не удалось подключиться к MySQL!")
    
    # 2. Установка Webhook с обработкой Flood Control
    webhook_setup_success = False
    max_retries = 5 # Увеличим число попыток
    delay = 1       # Начальная задержка
    
    for attempt in range(max_retries):
        # Ожидание перед каждой попыткой (включая первую)
        await asyncio.sleep(delay)
        
        try:
            await bot.set_webhook(
                url=WEBHOOK_URL, 
                secret_token=WEBHOOK_SECRET, 
                drop_pending_updates=True
            )
            logger.info(f"✅ Webhook установлен (Попытка {attempt + 1}/{max_retries}): {WEBHOOK_URL}")
            webhook_setup_success = True
            break # Успех, выходим из цикла
            
        except TelegramRetryAfter as e:
            # Если Telegram говорит "Flood control", ждем столько, сколько он просит (+запас)
            delay = e.retry_after + 2 
            logger.warning(f"⚠️ Flood Control: Webhook не установлен. Повторная попытка через {delay} с.")
            
        except TelegramBadRequest as e:
            # Сюда попадут ошибки типа Bad Request (неправильный URL или токен).
            logger.error(f"❌ Непоправимая ошибка установки Webhook: {e}")
            break # Непоправимая ошибка, нет смысла повторять
            
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка при установке Webhook: {e}")
            break
            
    if not webhook_setup_success:
        logger.critical("🚨 КРИТИЧЕСКАЯ ОШИБКА: Не удалось установить Webhook после всех попыток.")
        # Если Webhook не установлен, мы можем позволить worker'у завершиться с ошибкой,
        # чтобы Render попытался снова.
        # pass 

    # 3. Запуск фоновой задачи проверки отписок
    # Запускаем только если пул БД был успешно создан (db.pool != None)
    if db.pool:
        asyncio.create_task(check_for_unsubs(bot, db))
        logger.info("Запущена фоновая задача check_for_unsubs.")
        
    yield # Сервер начинает принимать запросы

    # --- SHUTDOWN LOGIC ---
    logger.info("--- [SHUTDOWN] Остановка сервера FastAPI ---")
    await db.close()

# Инициализация FastAPI приложения с lifespan
app = FastAPI(lifespan=lifespan) 

# -------------------------- Webhook Handler --------------------------

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request, update: dict):
    """Основной обработчик входящих обновлений от Telegram."""
    
    # Проверка секретного токена
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    # Передача обновления диспетчеру
    try:
        # Убедитесь, что 'update' является словарем (dict)
        await dp.feed_update(bot, Update(**update))
    except Exception as e:
        logger.error(f"Ошибка обработки обновления: {e}")

    return {"ok": True}
