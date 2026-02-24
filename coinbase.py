import time
from datetime import datetime, timezone
from typing import Optional

import requests

from store import Direction, Position

SPOT_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"
CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_CANDLES_PER_REQUEST = 300


def get_price(crypto: str) -> float:
    """Fetch spot price from Coinbase API. crypto should be e.g. 'BTC', 'SOL'."""
    pair = f"{crypto.upper()}-USD"
    url = SPOT_URL.format(pair=pair)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        raise ValueError(
            f"Could not fetch price for {crypto.upper()}/USD - possibly unsupported pair. Error: {str(e)}"
        )


def should_liquidate(position: Position) -> bool:
    """
    Check if liquidation threshold was crossed since position.timestamp.
    LONG: liquidate if low <= liquidation price.
    SHORT: liquidate if high >= liquidation price.
    """
    if position.timestamp is None:
        return False

    liq_price = position.liquidation_price()
    now = int(time.time())
    trigger_on_or_above = position.side == Direction.SHORT
    return _was_trigger_hit(
        crypto=position.crypto,
        price=liq_price,
        trigger_on_or_above=trigger_on_or_above,
        start_ts=position.timestamp,
        stop_ts=now,
    )


def should_fill_limit_order(order: Position) -> Optional[int]:
    """
    Check if a limit order threshold was crossed since order.timestamp.
    Returns the first candle timestamp where the threshold was hit, or None.
    LONG limit: fill if low <= entry.
    SHORT limit: fill if high >= entry.
    """
    if order.timestamp is None:
        return None

    limit_price = order.entry
    now = int(time.time())
    trigger_on_or_above = order.side == Direction.SHORT
    try:
        return _first_trigger_hit_timestamp(
            crypto=order.crypto,
            price=limit_price,
            trigger_on_or_above=trigger_on_or_above,
            start_ts=order.timestamp,
            stop_ts=now,
        )
    except ValueError:
        return None


def _was_trigger_hit(
    crypto: str,
    price: float,
    trigger_on_or_above: bool,
    start_ts: int,
    stop_ts: int,
) -> bool:
    try:
        if start_ts >= stop_ts:
            current = get_price(crypto)
            if trigger_on_or_above:
                return current >= price
            return current <= price

        low, high = _price_extremes_since(crypto, start_ts, stop_ts)
        if low is None or high is None:
            current = get_price(crypto)
            if trigger_on_or_above:
                return current >= price
            return current <= price
    except ValueError:
        # Fail-safe: if Coinbase APIs are unavailable, do not trigger.
        return False

    if trigger_on_or_above:
        return high >= price
    return low <= price


def _first_trigger_hit_timestamp(
    crypto: str,
    price: float,
    trigger_on_or_above: bool,
    start_ts: int,
    stop_ts: int,
) -> Optional[int]:
    if start_ts >= stop_ts:
        current = get_price(crypto)
        if trigger_on_or_above:
            return stop_ts if current >= price else None
        return stop_ts if current <= price else None

    product = f"{crypto.upper()}-USD"
    granularity = _choose_granularity(start_ts, stop_ts)
    chunk_seconds = granularity * MAX_CANDLES_PER_REQUEST

    saw_candles = False
    cursor = start_ts
    while cursor < stop_ts:
        chunk_end = min(stop_ts, cursor + chunk_seconds)
        params = {
            "start": _to_iso8601(cursor),
            "end": _to_iso8601(chunk_end),
            "granularity": granularity,
        }
        try:
            resp = requests.get(CANDLES_URL.format(product=product), params=params, timeout=10)
            resp.raise_for_status()
            candles = resp.json()
        except Exception as e:
            raise ValueError(
                f"Could not fetch candle data for {crypto.upper()}/USD. Error: {str(e)}"
            )

        if isinstance(candles, list) and candles:
            saw_candles = True
            ordered = sorted(
                (
                    candle
                    for candle in candles
                    if isinstance(candle, list) and len(candle) >= 3
                ),
                key=lambda candle: int(candle[0]),
            )
            for candle in ordered:
                candle_ts = int(candle[0])
                if candle_ts < start_ts or candle_ts > stop_ts:
                    continue
                low = float(candle[1])
                high = float(candle[2])
                if trigger_on_or_above and high >= price:
                    return candle_ts
                if not trigger_on_or_above and low <= price:
                    return candle_ts

        cursor = chunk_end

    # Fall back to spot if no candle data was available for the lookback window.
    if not saw_candles:
        current = get_price(crypto)
        if trigger_on_or_above:
            return stop_ts if current >= price else None
        return stop_ts if current <= price else None

    return None


def _price_extremes_since(crypto: str, start_ts: int, end_ts: int) -> tuple[float | None, float | None]:
    product = f"{crypto.upper()}-USD"
    granularity = _choose_granularity(start_ts, end_ts)
    chunk_seconds = granularity * MAX_CANDLES_PER_REQUEST

    low_seen: float | None = None
    high_seen: float | None = None

    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(end_ts, cursor + chunk_seconds)
        params = {
            "start": _to_iso8601(cursor),
            "end": _to_iso8601(chunk_end),
            "granularity": granularity,
        }
        try:
            resp = requests.get(CANDLES_URL.format(product=product), params=params, timeout=10)
            resp.raise_for_status()
            candles = resp.json()
        except Exception as e:
            raise ValueError(
                f"Could not fetch candle data for {crypto.upper()}/USD. Error: {str(e)}"
            )

        if isinstance(candles, list):
            for candle in candles:
                if not isinstance(candle, list) or len(candle) < 3:
                    continue
                low = float(candle[1])
                high = float(candle[2])
                if low_seen is None or low < low_seen:
                    low_seen = low
                if high_seen is None or high > high_seen:
                    high_seen = high

        cursor = chunk_end

    return low_seen, high_seen


def _to_iso8601(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _choose_granularity(start_ts: int, end_ts: int) -> int:
    duration = max(0, end_ts - start_ts)
    if duration <= 2 * 24 * 60 * 60:
        return 60
    if duration <= 7 * 24 * 60 * 60:
        return 300
    if duration <= 30 * 24 * 60 * 60:
        return 900
    if duration <= 180 * 24 * 60 * 60:
        return 3600
    if duration <= 365 * 24 * 60 * 60:
        return 21600
    return 86400
