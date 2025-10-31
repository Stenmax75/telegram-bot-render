import logging
import asyncio
import uvicorn # <-- НОВЫЙ ИМПОРТ
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Update
from contextlib import asynccontextmanager

# Импорт конфигурации и логики бота
from config_1 import (
    BOT_TOKEN, 
    WEB_SERVER_HOST, 
    WEB_SERVER_PORT, 
    BASE_WEBHOOK_URL,
    WEBHOOK_SECRET
)
from main_3 import dp, bot # Импортируем диспетчер и бота из основного файла
from database import db # Импортируем объект базы данных
from main_3 import check_for_unsubs # Импортируем фоновую задачу

# --- Настройка логирования ---
logger = logging.getLogger(__name__)

# --- Основные настройки Webhook ---
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

# -------------------------- Lifespan Context Manager --------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управляет событиями запуска (startup) и остановки (shutdown) сервера.
    Используется вместо устаревших @app.on_event
    """
    logger.info("--- [STARTUP] Запуск сервера FastAPI ---")

    # 1. Подключение к БД (Теперь MySQL)
    if await db.connect():
        logger.info("База данных MySQL подключена.")
    else:
        logger.critical("🚨 КРИТИЧЕСКАЯ ОШИБКА: Не удалось подключиться к MySQL. Бот не будет работать!")

    # 2. Установка Webhook
    try:
        await bot.set_webhook(
            url=WEBHOOK_URL, 
            secret_token=WEBHOOK_SECRET, 
            drop_pending_updates=True
        )
        logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")
    except TelegramBadRequest as e:
        logger.error(f"❌ Ошибка установки Webhook: {e}")

    # 3. Запуск фоновой задачи проверки отписок
    asyncio.create_task(check_for_unsubs(bot, db))
    logger.info("Запущена фоновая задача check_for_unsubs.")
    
    # --------------------------
    yield # Сервер начинает принимать запросы
    # --------------------------

    # --- SHUTDOWN LOGIC ---
    logger.info("--- [SHUTDOWN] Остановка сервера FastAPI ---")
    await db.close()

# Инициализация FastAPI приложения с lifespan
app = FastAPI(lifespan=lifespan) # <-- ИСПОЛЬЗУЕМ LIFESPAN

# -------------------------- Webhook Handler --------------------------

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request, update: dict):
    """Основной обработчик входящих обновлений от Telegram."""
    # Проверка секретного ключа для защиты Webhook
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    # Передача обновления диспетчеру
    try:
        await dp.feed_update(bot, Update(**update))
    except Exception as e:
        logger.error(f"Ошибка обработки обновления: {e}")

    return {"ok": True}

# -------------------------- Точка входа для самозапуска --------------------------

if __name__ == "__main__":
    # Запуск Uvicorn напрямую через Python-скрипт
    logger.info(">>> Запуск Uvicorn в режиме самозапуска <<<")
    uvicorn.run(
        "webhook_handler:app",  # Модуль и объект приложения
        host=WEB_SERVER_HOST,   # '0.0.0.0'
        port=WEB_SERVER_PORT,   # '8080'
        log_level="info",
        reload=False,           
        app_dir="/home/container"
    )