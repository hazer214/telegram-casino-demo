"""FastAPI приложение: эндпоинты и валидация Telegram initData."""
import hashlib
import hmac
import json
import os
import secrets
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
    lucky_jet,
)

# ---------------------------------------------------------------------------
# Хранилище активных раундов Lucky Jet (in-memory для демо)
# ---------------------------------------------------------------------------

import uuid
from datetime import datetime, timezone, timedelta

lucky_rounds: dict[str, dict] = {}


def cleanup_old_lucky_rounds():
    """Удаляет раунды старше 5 минут."""
    now = datetime.now(timezone.utc)
    expired = [
        rid for rid, r in lucky_rounds.items()
        if now - r["created_at"] > timedelta(minutes=5)
    ]
    for rid in expired:
        lucky_rounds.pop(rid, None)


# ---------------------------------------------------------------------------
# Загружаем переменные окружения из .env в корне проекта
# ---------------------------------------------------------------------------

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
        print("[initData] missing hash — trying fallback for demo mode")
        # Fallback для демо-режима: если initData без hash, но есть user,
        # доверяем этому (некоторые WebView не передают hash)
        user_json = data.get("user")
        if user_json:
            try:
                user_data = json.loads(user_json)
                if user_data.get("id"):
                    print(f"[initData] FALLBACK OK: user_id={user_data.get('id')}, first_name={user_data.get('first_name')}")
                    return user_data
            except json.JSONDecodeError:
                pass
        print("[initData] FAIL: missing hash and invalid user")
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
    """Зависимость FastAPI: проверяет initData и возвращает пользователя из БД.

    Если initData отсутствует/невалиден, пробуем fallback через query-параметры
    user_id и first_name, которые бот добавляет в URL Mini App. Это обходное
    решение для случаев, когда Telegram WebView не передаёт initData.
    """
    init_data = request.headers.get("X-Telegram-Init-Data")

    # Режим разработки: если заголовок пустой, используем фиктивного пользователя
    if not init_data and DEV_MODE:
        user = get_or_create_user(
            db,
            telegram_id=123456789,
            username="demo_user",
            first_name="Демо Игрок",
        )
        return user

    user_data = None
    if init_data:
        user_data = validate_init_data(init_data, BOT_TOKEN)

    # Fallback: если initData невалиден, берём user_id из query-параметров URL
    if not user_data:
        user_id = request.query_params.get("user_id")
        first_name = request.query_params.get("first_name") or "Игрок"
        if user_id and user_id.isdigit():
            print(f"[require_user] Fallback from query params: user_id={user_id}, first_name={first_name}")
            return get_or_create_user(
                db,
                telegram_id=int(user_id),
                username=None,
                first_name=first_name,
            )

    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")

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


class LuckyRequest(BaseModel):
    """Запрос на игру в Lucky Jet."""
    bet_amount: int = Field(..., ge=1, description="Сумма ставки")
    target_multiplier: float = Field(..., ge=1.01, description="Целевой коэффициент для выхода")


class LuckyJetStartRequest(BaseModel):
    """Запрос на старт раунда Lucky Jet."""
    bet_amount: int = Field(..., ge=1, description="Сумма ставки")


class LuckyJetCashoutRequest(BaseModel):
    """Запрос на вывод из раунда Lucky Jet."""
    round_id: str = Field(..., description="Идентификатор раунда")
    target_multiplier: float = Field(..., ge=1.0, description="Множитель, на котором игрок вышел")


class SlotsSpinRequest(BaseModel):
    """Запрос на вращение слотов."""
    bet_amount: int = Field(..., ge=1, description="Сумма ставки")


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


@app.post("/api/lucky/spin")
async def spin_lucky(
    req: LuckyRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Обрабатывает раунд Lucky Jet.

    Игрок делает ставку и указывает целевой коэффициент.
    Сервер генерирует случайный коэффициент краша.
    Если target_multiplier <= crash_multiplier — игрок выигрывает
    bet_amount * target_multiplier.
    Иначе ставка сгорает.
    """
    if req.bet_amount > user.balance:
        raise HTTPException(status_code=400, detail="Недостаточно средств")

    if req.bet_amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше 0")

    if req.target_multiplier < 1.01:
        raise HTTPException(status_code=400, detail="Коэффициент должен быть >= 1.01")

    # Списываем ставку
    user.balance -= req.bet_amount

    # Генерируем коэффициент краша на сервере
    crash_multiplier = lucky_jet.generate_multiplier()

    # Определяем результат
    is_win = req.target_multiplier <= crash_multiplier
    win_amount = 0
    final_balance_change = 0

    if is_win:
        win_amount = int(req.bet_amount * req.target_multiplier)
        final_balance_change = win_amount
        user.balance += win_amount

    # Сохраняем историю
    history = GameHistory(
        user_id=user.id,
        game_type="lucky_jet",
        bet_amount=req.bet_amount,
        bet_type=f"lucky:x{req.target_multiplier:.2f}",
        crash_multiplier=f"x{crash_multiplier:.2f}",
        win_amount=win_amount,
        is_win=is_win,
    )
    db.add(history)
    db.commit()

    return {
        "success": True,
        "crash_multiplier": round(crash_multiplier, 2),
        "target_multiplier": round(req.target_multiplier, 2),
        "is_win": is_win,
        "win_amount": win_amount,
        "balance": user.balance,
    }


@app.post("/api/lucky/start")
async def lucky_start(
    req: LuckyJetStartRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Стартует раунд Lucky Jet.

    Списывает ставку с баланса, генерирует случайный коэффициент краша
    и возвращает его фронтенду для честной анимации.
    """
    if req.bet_amount > user.balance:
        raise HTTPException(status_code=400, detail="Недостаточно средств")

    if req.bet_amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше 0")

    # Списываем ставку
    user.balance -= req.bet_amount
    db.commit()

    # Генерируем коэффициент краша
    crash_multiplier = lucky_jet.generate_multiplier()

    # Создаём раунд
    round_id = str(uuid.uuid4())
    cleanup_old_lucky_rounds()
    lucky_rounds[round_id] = {
        "user_id": user.id,
        "bet_amount": req.bet_amount,
        "crash_multiplier": crash_multiplier,
        "created_at": datetime.now(timezone.utc),
        "cashed_out": False,
    }

    return {
        "success": True,
        "round_id": round_id,
        "crash_multiplier": round(crash_multiplier, 2),
        "bet_amount": req.bet_amount,
        "balance": user.balance,
    }


@app.post("/api/lucky/cashout")
async def lucky_cashout(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Завершает раунд Lucky Jet.

    Принимает { round_id, target_multiplier } от фронтенда.
    Если target_multiplier <= crash_multiplier — игрок выигрывает.
    Иначе (включая target_multiplier=0) — проигрывает.
    """
    data = await request.json()
    round_id = data.get("round_id")
    target_multiplier = float(data.get("target_multiplier", 0))

    if not round_id or round_id not in lucky_rounds:
        raise HTTPException(status_code=400, detail="Раунд не найден или уже завершён")

    round_data = lucky_rounds[round_id]

    # Защита: раунд принадлежит текущему пользователю
    if round_data["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Раунд не принадлежит вам")

    if round_data["cashed_out"]:
        raise HTTPException(status_code=400, detail="Раунд уже завершён")

    crash_multiplier = round_data["crash_multiplier"]
    bet_amount = round_data["bet_amount"]

    # Помечаем раунд завершённым
    round_data["cashed_out"] = True

    is_win = target_multiplier >= 1.01 and target_multiplier <= crash_multiplier
    win_amount = 0

    if is_win:
        win_amount = int(bet_amount * target_multiplier)
        user.balance += win_amount

    # Сохраняем историю
    history = GameHistory(
        user_id=user.id,
        game_type="lucky_jet",
        bet_amount=bet_amount,
        bet_type=f"lucky:x{target_multiplier:.2f}",
        crash_multiplier=f"x{crash_multiplier:.2f}",
        win_amount=win_amount,
        is_win=is_win,
    )
    db.add(history)
    db.commit()

    # Удаляем раунд из памяти
    lucky_rounds.pop(round_id, None)

    return {
        "success": True,
        "is_win": is_win,
        "win_amount": win_amount,
        "crash_multiplier": round(crash_multiplier, 2),
        "target_multiplier": round(target_multiplier, 2),
        "balance": user.balance,
    }


class SlotsSpinRequest(BaseModel):
    """Запрос на вращение слотов."""
    bet_amount: int = Field(..., ge=1, description="Сумма ставки")


@app.post("/api/slots/spin")
async def spin_slots(
    req: SlotsSpinRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Обрабатывает вращение слотов."""
    if req.bet_amount > user.balance:
        raise HTTPException(status_code=400, detail="Недостаточно средств")

    if req.bet_amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше 0")

    # Списываем ставку
    user.balance -= req.bet_amount

    # Генерируем результат на сервере
    slot_icons = ['🍒', '🍋', '🔔', '💎', '7️⃣', '🍀', '👑']
    weights = [30, 25, 20, 12, 3, 7, 3]  # Веса выпадения каждого символа
    total = sum(weights)

    def roll():
        r = secrets.randbelow(total)
        cumulative = 0
        for icon, w in zip(slot_icons, weights):
            cumulative += w
            if r < cumulative:
                return icon
        return slot_icons[-1]

    results = [roll(), roll(), roll()]

    # Рассчитываем выигрыш
    all_same = results[0] == results[1] == results[2]
    two_same = results[0] == results[1] or results[1] == results[2] or results[0] == results[2]

    win_multiplier = 0
    if all_same:
        win_multiplier = 10
    elif two_same:
        win_multiplier = 2

    win_amount = req.bet_amount * win_multiplier
    is_win = win_amount > 0

    if is_win:
        user.balance += win_amount

    # Сохраняем историю
    history = GameHistory(
        user_id=user.id,
        game_type="slots",
        bet_amount=req.bet_amount,
        bet_type=f"slots:{','.join(results)}",
        win_amount=win_amount,
        is_win=is_win,
    )
    db.add(history)
    db.commit()

    return {
        "success": True,
        "results": results,
        "is_win": is_win,
        "win_amount": win_amount,
        "balance": user.balance,
    }


@app.get("/api/user/history")
async def get_history(
    game_type: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Возвращает историю ставок пользователя (последние 20 записей).

    Если указан game_type — фильтрует по типу игры:
    roulette, lucky_jet, slots.
    """
    query = db.query(GameHistory).filter(GameHistory.user_id == user.id)
    if game_type:
        query = query.filter(GameHistory.game_type == game_type)
    rows = query.order_by(GameHistory.created_at.desc()).limit(20).all()
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
