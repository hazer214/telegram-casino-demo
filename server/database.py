"""Модели базы данных и вспомогательные функции."""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

# Загружаем .env из корня проекта
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

START_BALANCE = int(os.getenv("START_BALANCE", "10000"))
CLAIM_COOLDOWN_SECONDS = int(os.getenv("CLAIM_COOLDOWN_SECONDS", "3600"))

# Путь к файлу базы данных в корне проекта
DB_PATH = Path(__file__).resolve().parent.parent / "casino.db"

# Создаём движок SQLAlchemy для SQLite
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class User(Base):  # type: ignore[valid-type, misc]
    """Таблица пользователей Telegram."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    balance = Column(Integer, default=START_BALANCE, nullable=False)
    last_claim_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class GameHistory(Base):  # type: ignore[valid-type, misc]
    """История игр (ставок) пользователя."""

    __tablename__ = "game_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    game_type = Column(String, default="roulette", nullable=False)
    bet_amount = Column(Integer, nullable=False)
    bet_type = Column(String, nullable=False)  # например: "red", "black", "even", "odd", "number:7", "lucky:x1.50"
    result_number = Column(Integer, nullable=True)  # для рулетки
    crash_multiplier = Column(String, nullable=True)  # для Lucky Jet (например "x1.50")
    win_amount = Column(Integer, nullable=False)  # 0 если проигрыш
    is_win = Column(Boolean, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Генератор случайного коэффициента для Lucky Jet
# ---------------------------------------------------------------------------

class LuckyJetDistribution:
    """Генератор случайного коэффициента краша для Lucky Jet.

    Используем геометрическое распределение: в ~45% раундов краш происходит
    до x1.5, в ~30% между x1.5 и x3.0, в ~20% между x3.0 и x10.0,
    и в ~5% выше x10.0. RTP игрока составляет ~97% (небольшое преимущество казино).
    """

    @staticmethod
    def generate_multiplier() -> float:
        r = secrets.randbelow(1000) / 1000.0  # 0.000 - 0.999
        if r < 0.45:
            # 45%: x1.00 - x1.50
            return 1.0 + secrets.randbelow(500) / 1000.0
        elif r < 0.75:
            # 30%: x1.50 - x3.00
            return 1.5 + secrets.randbelow(1500) / 1000.0
        elif r < 0.95:
            # 20%: x3.00 - x10.00
            return 3.0 + secrets.randbelow(7000) / 1000.0
        else:
            # 5%: x10.00 - x50.00
            return 10.0 + secrets.randbelow(40000) / 1000.0


lucky_jet = LuckyJetDistribution()


def get_db():
    """Генератор сессий для работы с БД."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_user(db, telegram_id: int, username: str | None, first_name: str | None):
    """Получает пользователя из БД или создаёт нового со стартовым балансом."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            balance=START_BALANCE,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def secure_random_number() -> int:
    """Генерирует случайное число для рулетки (0-36) криптографически безопасно."""
    return secrets.randbelow(37)


def now_utc() -> datetime:
    """Текущее время в UTC (offset-aware)."""
    return datetime.now(timezone.utc)


# Красные и чёрные числа европейской рулетки
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}


def is_red(number: int) -> bool:
    return number in RED_NUMBERS


def is_black(number: int) -> bool:
    return number in BLACK_NUMBERS


def is_even(number: int) -> bool:
    return number != 0 and number % 2 == 0


def is_odd(number: int) -> bool:
    return number != 0 and number % 2 == 1


def calculate_roulette_win(bet_type: str, bet_amount: int, result: int) -> int:
    """Рассчитывает выигрыш по ставке.

    Возвращает сумму выигрыша (0 если проигрыш).

    Правила:
    - red/black, even/odd — коэффициент 1:1 (выигрыш = ставка + ставка)
    - конкретное число — коэффициент 35:1 (выигрыш = ставка * 35 + ставка)
    """
    if bet_type == "red" and is_red(result):
        return bet_amount * 2
    if bet_type == "black" and is_black(result):
        return bet_amount * 2
    if bet_type == "even" and is_even(result):
        return bet_amount * 2
    if bet_type == "odd" and is_odd(result):
        return bet_amount * 2
    if bet_type.startswith("number:"):
        try:
            chosen_number = int(bet_type.split(":")[1])
            if chosen_number == result:
                return bet_amount * 36
        except (ValueError, IndexError):
            pass
    return 0
