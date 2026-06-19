"""FastAPI приложение: эндпоинты и валидация Telegram initData."""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.database import (
    get_db,
    get_or_create_user,
    init_db,
    secure_random_number,
    calculate_roulette_win,
    now_utc,
    User,
    GameHistory,
    CLAIM_COOLDOWN_SECONDS,
)

# Загружаем переменные окружения из .env в корне проекта
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Режим разработки: позволяет открывать Mini App локально без Telegram initData
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Вспомогательные функции для валидации Telegram initData
# ---------------------------------------------------------------------------

def parse_init_data(init_data: str) -> dict:
    """Парсит строку initData из Telegram в словарь."""
    result = {}
    for pair in init_data.split("&"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        result[key] = urllib.parse.unquote(value)
    return result


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Проверяет подпись Telegram initData.

    Возвращает данные пользователя (user), если подпись верна,
    иначе возвращает None.

    Алгоритм валидации официально описан в документации Telegram WebApp:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    # Отладочное логирование: записываем первые 200 символов initData,
    # чтобы понимать, что приходит от Telegram (не логируем полностью из соображений безопасности)
    print(f"[initData] received length={len(init_data)}, preview={init_data[:200]}")
    print(f"[initData] bot_token length={len(bot_token)}, preview={bot_token[:10]}...")

    data = parse_init_data(init_data)

    # Хеш, который прислал Telegram
    received_hash = data.pop("hash", None)
    if not received_hash:
        print("[initData] FAIL: missing hash")
        return None

    # Сортируем оставшиеся поля по ключу и собираем data_check_string
    data_check_arr = [f"{k}={v}" for k, v in sorted(data.items())]
    data_check_string = "\n".join(data_check_arr)

    # Создаём secret_key = HMAC_SHA256(token, "WebAppData")
    secret_key = hmac.new(
        key="WebAppData".encode(),
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    # Вычисляем hash от data_check_string
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Сравниваем хеши
    if not hmac.compare_digest(calculated_hash, received_hash):
        print(f"[initData] FAIL: hash mismatch. calculated={calculated_hash[:16]}... received={received_hash[:16]}...")
        return None

    # Проверяем актуальность (чтобы initData не была слишком старой)
    auth_date = data.get("auth_date")
    if auth_date:
        try:
            age = time.time() - int(auth_date)
            # initData старше 1 суток — не принимаем
            if age > 86400:
                print(f"[initData] FAIL: auth_date too old, age={age:.0f}s")
                return None
        except ValueError:
            print("[initData] FAIL: invalid auth_date")
            return None

    # Парсим JSON с данными пользователя
    user_json = data.get("user")
    if not user_json:
        print("[initData] FAIL: missing user field")
        return None
    try:
        user_data = json.loads(user_json)
        print(f"[initData] OK: user_id={user_data.get('id')}, first_name={user_data.get('first_name')}")
        return user_data
    except json.JSONDecodeError as e:
        print(f"[initData] FAIL: JSON decode error: {e}")
        return None




async def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Зависимость FastAPI: проверяет initData и возвращает пользователя из БД."""
    init_data = request.headers.get("X-Telegram-Init-Data")

    # Режим разработки: если заголовок пустой, используем фиктивного пользователя
    if not init_data and DEV_MODE:
        user = get_or_create_user(
            db,
            telegram_id=123456789,
            username="demo_user",
            first_name="Демо Игрок",
        )
        # Обновляем время последнего claim, чтобы не было конфликта часовых поясов
        return user

    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")

    user_data = validate_init_data(init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=403, detail="Invalid Telegram initData")

    telegram_id = user_data.get("id")
    if not telegram_id:
        raise HTTPException(status_code=403, detail="Invalid user data")

    return get_or_create_user(
        db,
        telegram_id=telegram_id,
        username=user_data.get("username"),
        first_name=user_data.get("first_name"),
    )


# ---------------------------------------------------------------------------
# Модели запросов
# ---------------------------------------------------------------------------

class SpinRequest(BaseModel):
    """Запрос на вращение рулетки."""
    bet_amount: int = Field(..., ge=1, description="Сумма ставки")
    bet_type: str = Field(..., description="Тип ставки: red, black, even, odd, number:N")


class ClaimRequest(BaseModel):
    """Запрос на получение бесплатных монет."""
    pass


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализируем БД при старте сервера."""
    init_db()
    yield


app = FastAPI(title="Telegram Mini App Casino", lifespan=lifespan)

# Разрешаем запросы с фронтенда (в проде можно ограничить домен)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Главная страница — отдаём статический HTML Mini App."""
    return FileResponse("server/public/index.html")


@app.get("/api/user/profile")
async def get_profile(user: User = Depends(require_user)):
    """Возвращает профиль и баланс пользователя."""
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "balance": user.balance,
    }


@app.post("/api/user/claim")
async def claim_coins(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Начисляет бесплатные монеты раз в час."""
    now = now_utc()

    # Проверяем кулдаун
    if user.last_claim_at:
        # last_claim_at может быть offset-naive (SQLite) — приводим к UTC
        last_claim = user.last_claim_at
        if last_claim.tzinfo is None:
            last_claim = last_claim.replace(tzinfo=timezone.utc)
        elapsed = (now - last_claim).total_seconds()
        remaining = CLAIM_COOLDOWN_SECONDS - elapsed
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Бесплатные монеты доступны позже",
                    "remaining_seconds": int(remaining),
                },
            )

    # Начисляем 5000 монет
    reward = 5000
    user.balance += reward
    user.last_claim_at = now
    db.commit()

    return {
        "success": True,
        "reward": reward,
        "balance": user.balance,
    }


@app.post("/api/roulette/spin")
async def spin_roulette(
    spin: SpinRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Обрабатывает ставку в рулетку."""
    # Проверяем, что ставка не больше баланса
    if spin.bet_amount > user.balance:
        raise HTTPException(status_code=400, detail="Недостаточно средств")

    if spin.bet_amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше 0")

    # Разрешаем только известные типы ставок
    allowed_types = {"red", "black", "even", "odd"}
    if spin.bet_type not in allowed_types and not spin.bet_type.startswith("number:"):
        raise HTTPException(status_code=400, detail="Некорректный тип ставки")

    # Списываем ставку с баланса
    user.balance -= spin.bet_amount

    # Генерируем результат на сервере (никогда не доверяем клиенту!)
    result_number = secure_random_number()

    # Рассчитываем выигрыш
    win_amount = calculate_roulette_win(spin.bet_type, spin.bet_amount, result_number)
    is_win = win_amount > 0

    # Зачисляем выигрыш
    if is_win:
        user.balance += win_amount

    # Сохраняем историю игры
    history = GameHistory(
        user_id=user.id,
        bet_amount=spin.bet_amount,
        bet_type=spin.bet_type,
        result_number=result_number,
        win_amount=win_amount,
        is_win=is_win,
    )
    db.add(history)
    db.commit()
    db.refresh(history)

    return {
        "success": True,
        "result_number": result_number,
        "is_win": is_win,
        "win_amount": win_amount,
        "balance": user.balance,
        "bet_type": spin.bet_type,
        "bet_amount": spin.bet_amount,
    }


@app.get("/api/user/history")
async def get_history(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Возвращает историю ставок пользователя (последние 20 записей)."""
    rows = (
        db.query(GameHistory)
        .filter(GameHistory.user_id == user.id)
        .order_by(GameHistory.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id": row.id,
            "game_type": row.game_type,
            "bet_amount": row.bet_amount,
            "bet_type": row.bet_type,
            "result_number": row.result_number,
            "win_amount": row.win_amount,
            "is_win": row.is_win,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
