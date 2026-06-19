"""Конфигурация Telegram-бота."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "")
START_BALANCE: int = int(os.getenv("START_BALANCE", "10000"))

# Проверяем, что токен указан
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
