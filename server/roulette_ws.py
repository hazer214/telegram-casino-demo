"""WebSocket-игра Live Roulette (мультиплеер)."""
import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import WebSocket
from sqlalchemy.orm import Session

from database import (
    User,
    GameHistory,
    secure_random_number,
    calculate_roulette_win,
    is_red,
    is_black,
    SessionLocal,
)


BETTING_PHASE_SECONDS = 15
SPIN_SECONDS = 6
RESULT_DISPLAY_SECONDS = 4

# Угол одного сектора на европейском колесе (37 чисел)
SECTOR_ANGLE = 2 * 3.141592653589793 / 37

# Порядок чисел на европейском колесе рулетки (против часовой стрелки)
WHEEL_ORDER = [
    0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36,
    11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9,
    22, 18, 29, 7, 28, 12, 35, 3, 26
]


def number_to_sector_index(number: int) -> int:
    """Возвращает индекс сектора (0-36) для числа на колесе."""
    return WHEEL_ORDER.index(number)


def number_color(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if is_red(number) else "black"


@dataclass
class RouletteBet:
    user_id: int
    telegram_id: int
    first_name: str
    bet_type: str
    amount: int


@dataclass
class RouletteRound:
    id: str
    state: str = "betting"  # betting | spinning | result | finished
    winning_number: Optional[int] = None
    bets: Dict[int, List[RouletteBet]] = field(default_factory=dict)  # key: telegram_id
    winning_sector: Optional[int] = None

    def to_dict(self, include_bets: bool = False) -> dict:
        data = {
            "round_id": self.id,
            "state": self.state,
            "winning_number": self.winning_number,
            "winning_sector": self.winning_sector,
        }
        if include_bets:
            flat_bets = []
            for telegram_id, bets in self.bets.items():
                for b in bets:
                    flat_bets.append({
                        "telegram_id": telegram_id,
                        "first_name": b.first_name,
                        "bet_type": b.bet_type,
                        "amount": b.amount,
                    })
            data["bets"] = flat_bets
        return data


class RouletteManager:
    """Управляет общим игровым циклом Live Roulette и WebSocket-клиентами."""

    def __init__(self):
        self.round: RouletteRound = self._create_new_round()
        self.clients: List[WebSocket] = []
        self.game_loop_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

    def _create_new_round(self) -> RouletteRound:
        return RouletteRound(id=secrets.token_hex(8))

    async def connect(self, websocket):
        async with self.lock:
            self.clients.append(websocket)
            round_state = self.round.to_dict(include_bets=True)
        try:
            await websocket.send_text(json.dumps({
                "type": "state",
                "round": round_state,
            }))
        except Exception:
            pass

    async def disconnect(self, websocket):
        async with self.lock:
            if websocket in self.clients:
                self.clients.remove(websocket)

    async def place_bet(self, telegram_id: int, first_name: str, bet_type: str, amount: int, db: Session):
        async with self.lock:
            if self.round.state != "betting":
                raise ValueError("Приём ставок завершён, дождитесь следующего раунда")

            user = db.query(User).filter(User.telegram_id == telegram_id).first()
            if not user:
                raise ValueError("Пользователь не найден")

            if user.balance < amount:
                raise ValueError("Недостаточно монет")

            # Списываем ставку
            user.balance -= amount
            db.commit()

            user_bets = self.round.bets.get(telegram_id, [])
            user_bets.append(RouletteBet(
                user_id=user.id,
                telegram_id=telegram_id,
                first_name=first_name,
                bet_type=bet_type,
                amount=amount,
            ))
            self.round.bets[telegram_id] = user_bets

            await self._broadcast({
                "type": "bet_placed",
                "round_id": self.round.id,
                "telegram_id": telegram_id,
                "first_name": first_name,
                "bet_type": bet_type,
                "amount": amount,
                "balance": user.balance,
            })

    async def start_game_loop(self):
        self.game_loop_task = asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        while True:
            db = SessionLocal()
            try:
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

                # --- Закрываем приём ставок ---
                async with self.lock:
                    self.round.state = "spinning"
                    winning_number = secure_random_number()
                    self.round.winning_number = winning_number
                    self.round.winning_sector = number_to_sector_index(winning_number)

                await self._broadcast({
                    "type": "spin_start",
                    "round_id": self.round.id,
                    "state": "spinning",
                    "winning_number": winning_number,
                    "winning_sector": self.round.winning_sector,
                    "duration_seconds": SPIN_SECONDS,
                })

                # Ждём, пока фронтенд покрутит колесо
                await asyncio.sleep(SPIN_SECONDS)

                # --- Расчёт выигрышей ---
                async with self.lock:
                    self.round.state = "result"
                    win_summary = []
                    for telegram_id, bets in self.round.bets.items():
                        user = db.query(User).filter(User.telegram_id == telegram_id).first()
                        if not user:
                            continue
                        total_win = 0
                        for bet in bets:
                            win_amount = calculate_roulette_win(bet.bet_type, bet.amount, winning_number)
                            is_win = win_amount > 0
                            history = GameHistory(
                                user_id=bet.user_id,
                                game_type="roulette",
                                bet_amount=bet.amount,
                                bet_type=bet.bet_type,
                                result_number=winning_number,
                                win_amount=win_amount,
                                is_win=is_win,
                            )
                            db.add(history)
                            if is_win:
                                total_win += win_amount

                        if total_win > 0:
                            user.balance += total_win
                            db.commit()
                            win_summary.append({
                                "telegram_id": telegram_id,
                                "first_name": user.first_name or "Игрок",
                                "win_amount": total_win,
                                "balance": user.balance,
                            })
                        else:
                            db.commit()

                    db.commit()

                await self._broadcast({
                    "type": "result",
                    "round_id": self.round.id,
                    "state": "result",
                    "winning_number": winning_number,
                    "winning_sector": self.round.winning_sector,
                    "color": number_color(winning_number),
                    "winners": win_summary,
                })

                await asyncio.sleep(RESULT_DISPLAY_SECONDS)

                # --- Переход к следующему раунду ---
                await self._broadcast({
                    "type": "round_finish",
                    "round_id": self.round.id,
                    "state": "finished",
                })
            finally:
                db.close()

    async def _broadcast(self, message: dict):
        text = json.dumps(message)
        dead_clients = []
        async with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                await client.send_text(text)
            except Exception:
                dead_clients.append(client)
        if dead_clients:
            async with self.lock:
                for client in dead_clients:
                    if client in self.clients:
                        self.clients.remove(client)


roulette_manager = RouletteManager()
