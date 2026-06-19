"""Запуск Telegram-бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

import urllib.parse

from bot.config import TELEGRAM_BOT_TOKEN, WEBAPP_URL

# Включаем логирование, чтобы видеть ошибки и запросы
logging.basicConfig(level=logging.INFO)

# Создаём бота и диспетчер
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """Обработка команды /start.

    При первом запуске бот отправляет приветствие и кнопку для открытия Mini App.
    initData Telegram передаётся автоматически при открытии WebApp — отдельно
    передавать ничего не нужно. Если initData не приходит (например, из-за
    ограничений WebView), user_id и first_name также передаются через query-параметры URL.
    """
    user = message.from_user
    # Формируем URL Mini App с fallback-параметрами пользователя
    webapp_url = f"{WEBAPP_URL}&user_id={user.id}&first_name={urllib.parse.quote(user.first_name or 'Игрок')}"

    # Формируем кнопку "Открыть Казино" с ссылкой на Mini App
    web_app_button = InlineKeyboardButton(
        text="Открыть Казино",
        web_app=WebAppInfo(url=webapp_url),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[web_app_button]])

    welcome_text = (
        "🎰 Добро пожаловать в Демо-Казино!\n\n"
        "У вас на счету 10 000 виртуальных монет.\n"
        "Нажмите кнопку ниже, чтобы открыть игру в Telegram."
    )

    await message.answer(welcome_text, reply_markup=keyboard)


async def main() -> None:
    """Точка входа бота."""
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
