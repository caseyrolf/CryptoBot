import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

DATA_FILE = os.environ.get("DATA_FILE", "crypto_data.json")


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @classmethod
    def from_raw(cls, raw: Any) -> "Direction":
        normalized = str(raw).strip().upper()
        if normalized in ("BUY", "LONG"):
            return cls.LONG
        if normalized in ("SELL", "SHORT"):
            return cls.SHORT
        raise ValueError(f"Unsupported direction: {raw}")


@dataclass
class Position:
    position_id: Optional[int]
    crypto: str
    side: Direction
    timestamp: Optional[int]
    entry: float
    margin: float
    lev: int
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    tp_timestamp: Optional[int] = None
    stop_timestamp: Optional[int] = None

    def liquidation_price(self) -> Optional[float]:
        if self.lev <= 0:
            return None

        # In this simulator's isolated-margin model, liquidation occurs after
        # an adverse move of 1 / leverage from entry.
        move = 1.0 / float(self.lev)
        if self.side == Direction.LONG:
            return self.entry * (1.0 - move)
        return self.entry * (1.0 + move)

    def __str__(self) -> str:
        liq_price = self.liquidation_price()
        liq_str = f"${liq_price:,.2f}" if liq_price is not None else "N/A"
        tp_str = f"${self.take_profit:,.2f}" if self.take_profit is not None else "N/A"
        stop_str = f"${self.stop_loss:,.2f}" if self.stop_loss is not None else "N/A"
        return (
            f"- **{self.side.value} {self.crypto}/USDT** (ID: `{self.position_id}`) @{self.lev}x | Margin: ${self.margin:.2f} | "
            f"Entry: ${self.entry:,.2f} | Liquidation: {liq_str} | TP: {tp_str} | Stop: {stop_str}"
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Position":
        try:
            side = Direction.from_raw(raw.get("side", Direction.LONG.value))
        except ValueError:
            side = Direction.LONG
        return cls(
            position_id=raw.get("position_id"),
            crypto=str(raw.get("crypto", "")),
            side=side,
            timestamp=raw.get("timestamp"),
            entry=float(raw.get("entry", 0.0)),
            margin=float(raw.get("margin", 0.0)),
            lev=int(raw.get("lev", 0)),
            take_profit=float(raw["take_profit"]) if raw.get("take_profit") is not None else None,
            stop_loss=float(raw["stop_loss"]) if raw.get("stop_loss") is not None else None,
            tp_timestamp=raw.get("tp_timestamp"),
            stop_timestamp=raw.get("stop_timestamp"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "crypto": self.crypto,
            "side": self.side,
            "timestamp": self.timestamp,
            "entry": self.entry,
            "margin": self.margin,
            "lev": self.lev,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "tp_timestamp": self.tp_timestamp,
            "stop_timestamp": self.stop_timestamp,
        }


@dataclass
class UserData:
    usd: float = 0.0
    positions: list[Position] = field(default_factory=list)
    orders: list[Position] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserData":
        positions = [Position.from_dict(pos) for pos in raw.get("positions", [])]
        orders = [Position.from_dict(order) for order in raw.get("orders", [])]
        return cls(
            usd=float(raw.get("usd", 0.0)),
            positions=positions,
            orders=orders,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "usd": self.usd,
            "positions": [pos.to_dict() for pos in self.positions],
            "orders": [order.to_dict() for order in self.orders],
        }


@dataclass
class AppData:
    users: dict[str, UserData] = field(default_factory=dict)
    next_id: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppData":
        users = {
            uid: UserData.from_dict(udata)
            for uid, udata in raw.get("users", {}).items()
        }
        return cls(
            users=users,
            next_id=int(raw.get("next_id", 0)),
        )

    def ensure_user(self, user_id: str) -> UserData:
        if user_id not in self.users:
            self.users[user_id] = UserData()
        return self.users[user_id]

    def next_position_id(self) -> int:
        pos_id = self.next_id
        self.next_id += 1
        return pos_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "users": {uid: user_data.to_dict() for uid, user_data in self.users.items()},
            "next_id": self.next_id,
        }


APP_DATA: AppData


def next_position_id() -> int:
    global APP_DATA
    return APP_DATA.next_position_id()


def get_users() -> dict[str, UserData]:
    global APP_DATA
    return APP_DATA.users


def ensure_user(user_id: str) -> UserData:
    global APP_DATA
    user_exists: bool = user_id in get_users()
    user_data: UserData = APP_DATA.ensure_user(user_id)
    if not user_exists:
        save_data()
    return user_data


def save_data() -> None:
    global APP_DATA
    with open(DATA_FILE, "w") as f:
        json.dump(APP_DATA.to_dict(), f)


def load_data() -> AppData:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
            return AppData.from_dict(raw)
    return AppData()


def init_store():
    global APP_DATA
    APP_DATA = load_data()

    changed = False
    now = int(time.time())
    for user_data in APP_DATA.users.values():
        for pos in user_data.positions:
            if pos.position_id is None:
                pos.position_id = APP_DATA.next_position_id()
                changed = True
            if pos.timestamp is None:
                pos.timestamp = now
                changed = True
            if pos.take_profit is not None and pos.tp_timestamp is None:
                pos.tp_timestamp = pos.timestamp
                changed = True
            if pos.stop_loss is not None and pos.stop_timestamp is None:
                pos.stop_timestamp = pos.timestamp
                changed = True
        for order in user_data.orders:
            if order.position_id is None:
                order.position_id = APP_DATA.next_position_id()
                changed = True
            if order.timestamp is None:
                order.timestamp = now
                changed = True

    if changed:
        save_data()
