# Telegram Mini App: Демо-казино (Рулетка)

Полноценный пример Telegram Mini App с виртуальной европейской рулеткой.
Виртуальные монеты, никаких реальных денег.

## Стек

- **Бот**: Python 3.11+, aiogram 3.x
- **Бэкенд**: FastAPI + uvicorn
- **База данных**: SQLite
- **Фронтенд**: HTML5/CSS/JS (Telegram WebApp SDK)

## Структура проекта

```
telegram-casino/
├── bot/
│   ├── __init__.py
│   ├── config.py          # Конфигурация бота
│   └── bot.py             # Точка входа бота, обработка /start
├── server/
│   ├── __init__.py
│   ├── app.py             # FastAPI приложение и эндпоинты
│   ├── database.py        # Модели и работа с SQLite
│   └── public/
│       └── index.html     # Фронтенд Mini App
├── .env                   # Переменные окружения (токен и т.д.)
├── .env.example           # Пример переменных
├── requirements.txt       # Зависимости Python
├── Procfile               # Команда запуска для Render
├── render.yaml            # Blueprint для Render
└── README.md              # Инструкция по запуску
```

## 1. Подготовка

### 1.1 Создайте бота в BotFather

1. Напишите [@BotFather](https://t.me/BotFather) `/newbot`.
2. Задайте имя и username бота.
3. Скопируйте **HTTP API token**.
4. Вставьте токен в файл `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCDEF...your_token
WEBAPP_URL=https://your-server.com/   # смотрите раздел про ngrok ниже
PORT=8000
START_BALANCE=10000
CLAIM_COOLDOWN_SECONDS=3600
```

### 1.2 Подключите WebApp в BotFather

1. В BotFather отправьте `/mybots`.
2. Выберите своего бота → **Bot Settings** → **Menu Button** → **Configure menu button**.
3. Укажите название кнопки, например `🎰 Казино`, и URL вашего Mini App.
4. Или используйте `/setinline` + WebApp-кнопку, которую отправляет бот при `/start` (как в нашем коде).

## 2. Установка зависимостей

```bash
pip install -r requirements.txt
```

Рекомендуется делать это в виртуальном окружении:

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Локальный запуск (сервер)

```bash
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Сервер будет доступен по адресу `http://localhost:8000`.

## 4. Запуск Telegram-бота

В отдельном терминале:

```bash
python -m bot.bot
```

При команде `/start` бот пришлёт приветствие и кнопку **«Открыть Казино»**.

## 5. Тестирование через ngrok

Telegram Mini App требует HTTPS и должен быть доступен из интернета. Для локальных тестов используйте **ngrok**.

### 5.1 Установка ngrok

- Скачайте [ngrok](https://ngrok.com/download).
- Зарегистрируйтесь и получите authtoken.
- Выполните:

```bash
ngrok config add-authtoken YOUR_AUTHTOKEN
```

### 5.2 Проброс порта

```bash
ngrok http 8000
```

ngrok выдаст HTTPS URL вида:

```
https://a1b2c3d4.ngrok-free.app
```

### 5.3 Настройка Mini App

1. Скопируйте HTTPS URL ngrok.
2. Вставьте его в `.env` в переменную `WEBAPP_URL`:

```env
WEBAPP_URL=https://a1b2c3d4.ngrok-free.app
```

3. Перезапустите бота.
4. В BotFather укажите тот же URL как WebApp URL.
5. Откройте бота в Telegram, нажмите **«Открыть Казино»**.

> Важно: каждый раз при перезапуске ngrok URL меняется (в бесплатной версии). Обновляйте `WEBAPP_URL` и настройки BotFather.

## 6. Деплой на Render (бесплатный и стабильный вариант)

Самый надёжный способ — развернуть сервер на Render. Это бесплатно, даёт постоянный HTTPS URL и не требует запускать туннели на твоём компьютере.

### 6.1 Создай репозиторий на GitHub

1. Создай новый публичный репозиторий (например, `telegram-casino-demo`).
2. Загрузи туда файлы проекта:

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/ВАШ_НИК/telegram-casino-demo.git
git push -u origin main
```

> Файл `.env` уже добавлен в `.gitignore`, поэтому токен не попадёт в GitHub.

### 6.2 Подключи Render

1. Зайди на [render.com](https://render.com) и авторизуйся через GitHub.
2. Нажми **New +** → **Blueprint** → выбери свой репозиторий.
3. Render автоматически создаст Web Service из `render.yaml`.
4. В настройках сервиса добавь переменные окружения вручную:

| Переменная | Значение |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | твой токен от BotFather |
| `WEBAPP_URL` | скопировать после деплоя из строки вида `https://telegram-casino-demo-xxx.onrender.com/` |

5. Нажми **Apply** и дождись деплоя (обычно 2–5 минут).

### 6.3 Настрой Telegram

После деплоя:

1. Скопируй HTTPS URL сервиса из Render Dashboard.
2. Обнови `WEBAPP_URL` в переменных окружения Render и перезапусти сервис.
3. Обнови URL в BotFather:
   - `/mybots` → выбери бота → **Bot Settings** → **Menu Button** → **Configure menu button**
   - Укажи название `🎰 Казино` и URL из Render.
4. Перезапусти бота локально (или тоже задеплой его, например, на второй сервис Render или Railway).

### 6.4 Запуск бота

Бота можно запускать локально (он использует только токен и Telegram API):

```bash
cd telegram-casino
python -m bot.bot
```

Или задеплой на [Railway](https://railway.app) / Render как отдельный сервис с командой:

```bash
python -m bot.bot
```

## 7. Продакшен на VPS

Для полноценного продакшена размести сервер на VPS (Hetzner, AWS, DigitalOcean) с HTTPS-доменом.

```env
WEBAPP_URL=https://casino.example.com/
```

Запуск:

```bash
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Рекомендуется Nginx как reverse-proxy с SSL от Let's Encrypt.

## 8. API эндпоинты

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| GET   | `/` | Главная страница Mini App |
| GET   | `/api/user/profile` | Профиль и баланс |
| POST  | `/api/user/claim` | Получить бесплатные монеты |
| POST  | `/api/roulette/spin` | Сделать ставку в рулетку |
| GET   | `/api/user/history` | История ставок |

Все защищённые эндпоинты требуют заголовок `X-Telegram-Init-Data`, который автоматически отправляет Telegram WebApp SDK.

## 8. Безопасность

- `initData` валидируется на сервере через HMAC-SHA256 с токеном бота.
- Случайное число рулетки генерируется на сервере (`secrets.randbelow`).
- Баланс и история хранятся в SQLite, обновляются только через API.

## 9. Возможные доработки

- Добавить слоты, блэкджек, кости.
- Добавить лидерборд и рейтинг игроков.
- Перейти на PostgreSQL для большого числа пользователей.
- Добавить мультиязычность.
