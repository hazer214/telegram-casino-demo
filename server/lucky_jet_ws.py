"""WebSocket-игра Lucky Jet (синхронный multiplayer crash game)."""
import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from server.database import (
    GameHistory,
    User,
    get_or_create_user,
    init_db,
)

# ---------------------------------------------------------------------------
# Конфигурация игрового цикла
# ---------------------------------------------------------------------------

BETTING_PHASE_SECONDS = 7          # фаза приёма ставок
CRASH_TICK_INTERVAL_MS = 50       # частота обновления икса
POST_CRASH_PAUSE_SECONDS = 3       # пауза между раундами
MIN_CRASH_MULTIPLIER = 1.01        # минимальный икс краша
MAX_CRASH_MULTIPLIER = 100.0       # максимальный икс краша


# ---------------------------------------------------------------------------
# Модель ставки
# ---------------------------------------------------------------------------

@dataclass
class LuckyBet:
    user_id: int
    telegram_id: int
    first_name: str
    amount: int
    cashed_out: bool = False
    cashout_multiplier: float = 0.0
    win_amount: int = 0


# ---------------------------------------------------------------------------
# Состояние раунда
# ---------------------------------------------------------------------------

@dataclass
class LuckyRound:
    id: str
    state: str = "betting"          # betting | flying | crashed | finished
    crash_multiplier: float = 1.0
    current_multiplier: float = 1.0
    bets: Dict[int, LuckyBet] = field(default_factory=dict)  # key: telegram_id
    started_at: Optional[datetime] = None
    crashed_at: Optional[datetime] = None
    tick_task: Optional[asyncio.Task] = None

    def to_dict(self, include_bets: bool = False) -> dict:
        data = {
            "round_id": self.id,
            "state": self.state,
            "current_multiplier": round(self.current_multiplier, 2),
            "crash_multiplier": round(self.crash_multiplier, 2) if self.state in ("crashed", "finished") else None,
        }
        if include_bets:
            data["bets"] = [
                {
                    "telegram_id": b.telegram_id,
                    "first_name": b.first_name,
                    "amount": b.amount,
                    "cashed_out": b.cashed_out,
                    "cashout_multiplier": round(b.cashout_multiplier, 2) if b.cashed_out else None,
                    "win_amount": b.win_amount,
                }
                for b in self.bets.values()
            ]
        return data


# ---------------------------------------------------------------------------
# Генератор коэффициента краша
# ---------------------------------------------------------------------------

def generate_crash_multiplier() -> float:
    """Криптографически безопасный генератор икса краша.

    Геометрическое распределение:
    - ~45% раундов краш до x1.50
    - ~30% между x1.50 и x3.00
    - ~20% между x3.00 и x10.00
    - ~5% выше x10.00
    """
    r = secrets.randbelow(1000) / 1000.0
    if r < 0.45:
        return 1.0 + secrets.randbelow(500) / 1000.0
    elif r < 0.75:
        return 1.5 + secrets.randbelow(1500) / 1000.0
    elif r < 0.95:
        return 3.0 + secrets.randbelow(7000) / 1000.0
    else:
        return 10.0 + secrets.randbelow(40000) / 1000.0


# ---------------------------------------------------------------------------
# Менеджер игры
# ---------------------------------------------------------------------------

class LuckyJetManager:
    """Управляет общим игровым циклом Lucky Jet и списком WebSocket-клиентов."""

    def __init__(self):
        self.round: LuckyRound = self._create_new_round()
        self.clients: List = []
        self.game_loop_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Создание раунда
    # -----------------------------------------------------------------------

    def _create_new_round(self) -> LuckyRound:
        return LuckyRound(
            id=secrets.token_hex(8),
            state="betting",
            crash_multiplier=generate_crash_multiplier(),
        )

    # -----------------------------------------------------------------------
    # Подключение / отключение клиентов
    # -----------------------------------------------------------------------

    async def connect(self, websocket):
        await self.lock.acquire()
        try:
            self.clients.append(websocket)
            await self._send_state(websocket)
        finally:
            self.lock.release()

    async def disconnect(self, websocket):
        await self.lock.acquire()
        try:
            if websocket in self.clients:
                self.clients.remove(websocket)
        finally:
            self.lock.release()

    # -----------------------------------------------------------------------
    # Ставки
    # -----------------------------------------------------------------------

    async def place_bet(self, telegram_id: int, first_name: str, amount: int, db: Session):
        async with self.lock:
            if self.round.state != "betting":
                raise ValueError("Приём ставок завершён, дождитесь следующего раунда")

            if telegram_id in self.round.bets:
                raise ValueError("Вы уже сделали ставку в этом раунде")

            user = db.query(User).filter(User.telegram_id == telegram_id).first()
            if not user:
                raise ValueError("Пользователь не найден")

            if user.balance < amount:
                raise ValueError("Недостаточно монет")

            # Списываем ставку с баланса
            user.balance -= amount
            db.commit()

            bet = LuckyBet(
                user_id=user.id,
                telegram_id=telegram_id,
                first_name=first_name,
                amount=amount,
            )
            self.round.bets[telegram_id] = bet

            await self._broadcast({
                "type": "bet_placed",
                "round_id": self.round.id,
                "telegram_id": telegram_id,
                "first_name": first_name,
                "amount": amount,
            })

    # -----------------------------------------------------------------------
    # Забрать выигрыш
    # -----------------------------------------------------------------------

    async def cashout(self, telegram_id: int, db: Session) -> dict:
        async with self.lock:
            if self.round.state != "flying":
                raise ValueError("Раунд не в фазе полёта")

            bet = self.round.bets.get(telegram_id)
            if not bet:
                raise ValueError("У вас нет ставки в этом раунде")

            if bet.cashed_out:
                raise ValueError("Вы уже забрали выигрыш")

            multiplier = self.round.current_multiplier
            win_amount = int(bet.amount * multiplier)

            user = db.query(User).filter(User.telegram_id == telegram_id).first()
            if not user:
                raise ValueError("Пользователь не найден")

            user.balance += win_amount
            db.commit()

            bet.cashed_out = True
            bet.cashout_multiplier = multiplier
            bet.win_amount = win_amount

            # Записываем в историю
            history = GameHistory(
                user_id=bet.user_id,
                game_type="lucky_jet",
                bet_amount=bet.amount,
                bet_type=f"lucky:x{multiplier:.2f}",
                crash_multiplier=f"x{self.round.crash_multiplier:.2f}",
                win_amount=win_amount,
                is_win=True,
            )
            db.add(history)
            db.commit()

            await self._broadcast({
                "type": "cashout",
                "round_id": self.round.id,
                "telegram_id": telegram_id,
                "first_name": bet.first_name,
                "multiplier": round(multiplier, 2),
                "win_amount": win_amount,
            })

            return {
                "success": True,
                "multiplier": round(multiplier, 2),
                "win_amount": win_amount,
                "balance": user.balance,
            }

    # -----------------------------------------------------------------------
    # Игровой цикл
    # -----------------------------------------------------------------------

    async def start_game_loop(self):
        self.game_loop_task = asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        while True:
            # --- Фаза 1: приём ставок ---
            self.round = self._create_new_round()
            self.round.state = "betting"
            await self._broadcast({
                "type": "round_start",
                "round_id": self.round.id,
                "state": "betting",
                "countdown_seconds": BETTING_PHASE_SECONDS,
            })
            await asyncio.sleep(BETTING_PHASE_SECONDS)

            # Закрываем приём ставок
            async with self.lock:
                self.round.state = "flying"
                self.round.started_at = datetime.now(timezone.utc)

                # Всем, кто не поставил, возвращаем деньги (на всякий случай)
                # На самом деле деньги уже списаны при ставке.

            await self._broadcast({
                "type": "flight_start",
                "round_id": self.round.id,
                "state": "flying",
                "crash_multiplier": round(self.round.crash_multiplier, 2),
            })

            # --- Фаза 2: полёт ---
            await self._flight_loop()

            # --- Фаза 3: краш ---
            async with self.lock:
                self.round.state = "crashed"
                self.round.crashed_at = datetime.now(timezone.utc)

                # Записываем проигрыши тех, кто не забрал
                for bet in self.round.bets.values():
                    if not bet.cashed_out:
                        history = GameHistory(
                            user_id=bet.user_id,
                            game_type="lucky_jet",
                            bet_amount=bet.amount,
                            bet_type=f"lucky:crashed_x{self.round.crash_multiplier:.2f}",
                            crash_multiplier=f"x{self.round.crash_multiplier:.2f}",
                            win_amount=0,
                            is_win=False,
                        )
                        db = SessionLocal()
                        try:
                            db.add(history)
                            db.commit()
                        finally:
                            db.close()

            await self._broadcast({
                "type": "crash",
                "round_id": self.round.id,
                "state": "crashed",
                "crash_multiplier": round(self.round.crash_multiplier, 2),
            })

            # --- Пауза ---
            await asyncio.sleep(POST_CRASH_PAUSE_SECONDS)

            async with self.lock:
                self.round.state = "finished"

    async def _flight_loop(self):
        """Цикл полёта: увеличиваем икс каждые 50 мс."""
        start_time = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(CRASH_TICK_INTERVAL_MS / 1000.0)
            elapsed = asyncio.get_event_loop().time() - start_time

            # Экспонентный рост: multiplier = e^(0.06 * elapsed) ~ x1.50 за 7 сек
            multiplier = max(1.0, pow(2.71828, 0.06 * elapsed))

            async with self.lock:
                self.round.current_multiplier = multiplier
                crashed = multiplier >= self.round.crash_multiplier

            await self._broadcast({
                "type": "tick",
                "round_id": self.round.id,
                "multiplier": round(multiplier, 2),
            })

            if crashed:
                break

    # -----------------------------------------------------------------------
    # Рассылка сообщений
    # -----------------------------------------------------------------------

    async def _send_state(self, websocket):
        """Отправляет текущее состояние новому подключившемуся клиенту."""
        async with self.lock:
            payload = {
                "type": "state",
                "round": self.round.to_dict(include_bets=True),
            }
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _broadcast(self, message: dict):
        """Рассылает сообщение всем подключённым клиентам."""
        text = json.dumps(message)
        dead_clients = []
        for ws in self.clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead_clients.append(ws)

        # Удаляем отвалившихся клиентов
        for ws in dead_clients:
            if ws in self.clients:
                self.clients.remove(ws)


# ---------------------------------------------------------------------------
# Глобальный менеджер (синглтон)
# ---------------------------------------------------------------------------

from server.database import SessionLocal

lucky_manager = LuckyJetManager()
