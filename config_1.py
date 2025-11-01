# config_1.py (Оптимизирован для Render и TiDB)
import os
from dotenv import load_dotenv

# load_dotenv() - эта строка нужна только для локального запуска, на Render переменные уже доступны.
# Но оставляем, если вы хотите локально проверить код.
load_dotenv() 

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")
REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID")
WEBHOOK_SECRET = os.getenv("RENDER_EXTERNAL_URL", "default_secret_key_change_me") 

# Database (MySQL/TiDB)
# TiDB требует эти переменные, которые мы настроим на Render
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
DB_HOST = os.getenv("DB_HOST") 
# Порт считываем как целое число. Для TiDB это 4000.
DB_PORT = int(os.getenv("DB_PORT", 4000)) # Изменили порт по умолчанию на 4000

# Webhook (Server Configuration)
# УДАЛЕНЫ WEB_SERVER_HOST и WEB_SERVER_PORT - Render управляет ими через $PORT.
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL") 

# Проверка на наличие критически важных данных
if not (BOT_TOKEN and BASE_WEBHOOK_URL and DB_USER and DB_HOST and DB_NAME):
    # Добавили DB_PORT в условие

    raise ValueError("Одна или несколько критически важных переменных (BOT_TOKEN, BASE_WEBHOOK_URL, DB_USER, DB_HOST, DB_NAME, DB_PORT) не найдены.")
