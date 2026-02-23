import time
from datetime import datetime, timezone

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
    liq_price = position.liquidation_price()
    now = int(time.time())
    start_ts = position.timestamp if position.timestamp is not None else now

    try:
        if start_ts >= now:
            current = get_price(position.crypto)
            if position.side == Direction.LONG:
                return current <= liq_price
            return current >= liq_price

        low, high = _price_extremes_since(position.crypto, start_ts, now)
        if low is None or high is None:
            current = get_price(position.crypto)
            if position.side == Direction.LONG:
                return current <= liq_price
            return current >= liq_price
    except ValueError:
        # Fail-safe: if Coinbase APIs are unavailable, do not liquidate.
        return False

    if position.side == Direction.LONG:
        return low <= liq_price
    return high >= liq_price


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
