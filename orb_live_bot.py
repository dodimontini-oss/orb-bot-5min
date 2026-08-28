"""
Opening Range Breakout (ORB) live bot - BASELINE config (no confluence
filters). GAPCONFIRM-style relative-volume / range-normalization filters
were tested (see orb_backtest_lab.py, orb_multi_timeframe_lab.py) and traded
away total return for lower drawdown - user explicitly chose BASELINE for
BOTH timeframe variants (this file is deployed twice, once per repo, with
only ORB_WINDOW_MINUTES changed) specifically for the higher P&L, and so
both bots share the same confluence setting, keeping the 5-min-vs-15-min
comparison clean (see project_orb_futures_strategy_findings memory).

Trades QQQ directly (not NQ futures - see that memory for why: no free
futures data/execution exists, and QQQ sidesteps it entirely by being the
SAME instrument the backtest was run on, not a proxy needing translation).

Rules (exactly matching the validated backtest):
  1. Mark the high/low of the first ORB_WINDOW_MINUTES of the regular
     session (9:30 AM America/New_York onward).
  2. Watch every subsequent 5-minute bar for a CONFIRMED CLOSE beyond that
     range (not a wick-touch) - the first one found each session triggers
     entry. At most one entry attempt per session.
  3. Stop = opposite side of the opening range (fixed, doesn't depend on
     fill price). Target = entry +/- 2x that risk distance (2:1 R:R).
  4. Enter with a real Alpaca BRACKET order (entry + stop + target in one
     call, time_in_force=GTC so the protective legs stay live even if the
     position carries past today's close - matches the backtest's "no
     session-close flatten" rule). QQQ is a stock, so - unlike the crypto
     bot - this gets a REAL broker-side stop-loss, no virtual stop-checking
     workaround needed. Alpaca supports short selling for stocks too, so
     both LONG and SHORT signals are traded live, matching the backtest.

Checks EVERY bar since the range closed each run (not just the latest), so
an occasional missed/delayed GitHub Actions run doesn't cause a missed
signal entirely - worst case it enters a bit late, at the current market
price rather than the exact breakout bar's price (same category of live-
vs-backtest difference as any live deployment, and small for a liquid ETF).

Runs every 5 minutes via cron across a UTC window wide enough to cover
market hours in both DST regimes - this script checks the actual current
America/New_York time itself and no-ops outside 9:30 AM-4:00 PM ET on a
weekday, so cron never needs manual DST adjustment twice a year.

Environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
"""

import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orb-live")

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TRADING_BASE_URL = "https://paper-api.alpaca.markets"  # paper only - never change without a deliberate decision
DATA_BASE_URL = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

SYMBOL = "QQQ"
ORB_WINDOW_MINUTES = 5  # the only line that differs between orb-bot-5min and orb-bot-15min
RR_RATIO = 2.0
RISK_PER_TRADE_PCT = 1.0

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


def market_is_open_now() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def _orb_end_hm(window_minutes: int) -> str:
    """HH:MM (ET) of the LAST bar included in the opening range - e.g. a 15
    minute window (9:30-9:45) includes the 09:30/09:35/09:40 bars, so the
    range is 'closed' as of 09:40 and breakout-watching starts at 09:45."""
    n_bars = window_minutes // 5
    end_minute_offset = (n_bars - 1) * 5
    hour = 9 + (30 + end_minute_offset) // 60
    minute = (30 + end_minute_offset) % 60
    return f"{hour:02d}:{minute:02d}"


def get_today_bars() -> pd.DataFrame:
    now_et = datetime.now(ET)
    start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    params = {
        "timeframe": "5Min",
        "start": start_of_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 200,
        "feed": "iex",
    }
    resp = requests.get(f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/bars", headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    bars = resp.json().get("bars", [])
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["time_et"] = df["t"].dt.tz_convert(ET)
    df["hm"] = df["time_et"].dt.strftime("%H:%M")
    return df.sort_values("t").reset_index(drop=True)


def get_position():
    resp = requests.get(f"{TRADING_BASE_URL}/v2/positions/{SYMBOL}", headers=HEADERS, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def get_open_orders() -> list:
    resp = requests.get(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS,
                         params={"status": "open", "symbols": SYMBOL}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def already_traded_today() -> bool:
    """Any order (filled, open, or otherwise) for this symbol submitted
    today counts as 'already attempted' - matches the backtest's one-
    entry-attempt-per-session rule, so a stopped-out position doesn't
    immediately re-enter off the same day's range."""
    now_et = datetime.now(ET)
    start_of_day = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = requests.get(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS,
                         params={"status": "all", "symbols": SYMBOL, "direction": "desc", "limit": 50,
                                 "after": start_of_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                         timeout=10)
    resp.raise_for_status()
    return len(resp.json()) > 0


def get_account_equity() -> float:
    resp = requests.get(f"{TRADING_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["equity"])


def place_bracket_order(direction: str, qty: int, stop: float, target: float) -> dict:
    side = "buy" if direction == "LONG" else "sell"
    body = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc",  # keeps the stop/target legs live even if the position carries past today's close
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round(target, 2))},
        "stop_loss": {"stop_price": str(round(stop, 2))},
    }
    resp = requests.post(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_and_trade():
    if not market_is_open_now():
        log.info("Outside regular market hours (9:30-16:00 ET, weekdays). No action.")
        return

    position = get_position()
    if position is not None and float(position["qty"]) != 0:
        log.info("Already in a position (%s %s shares). Bracket order manages the exit. No action.",
                  position["side"], position["qty"])
        return

    if get_open_orders():
        log.info("Open order(s) already pending on %s. No action.", SYMBOL)
        return

    if already_traded_today():
        log.info("Already attempted an entry today. No action (one attempt per session).")
        return

    df = get_today_bars()
    if df.empty:
        log.info("No bars for today yet. No action.")
        return

    orb_end_hm = _orb_end_hm(ORB_WINDOW_MINUTES)
    orb_bars = df[df["hm"] <= orb_end_hm]
    if orb_bars.empty or orb_bars["hm"].max() < orb_end_hm:
        log.info("Opening range not fully formed yet (need bars through %s ET). No action.", orb_end_hm)
        return

    range_high, range_low = orb_bars["high"].max(), orb_bars["low"].min()
    rest = df[df["hm"] > orb_end_hm]
    if rest.empty:
        log.info("Opening range set (high=%.2f low=%.2f) - no bars yet to check for a breakout.",
                  range_high, range_low)
        return

    direction = None
    for _, row in rest.iterrows():
        if row["close"] > range_high:
            direction = "LONG"
            break
        elif row["close"] < range_low:
            direction = "SHORT"
            break
    if direction is None:
        log.info("No confirmed breakout yet (range high=%.2f low=%.2f, latest close=%.2f). No action.",
                  range_high, range_low, rest.iloc[-1]["close"])
        return

    entry_ref = rest.iloc[-1]["close"]  # reference for sizing/target only - actual fill is the live market price
    stop = range_low if direction == "LONG" else range_high
    stop_distance = abs(entry_ref - stop)
    if stop_distance <= 0:
        log.warning("Zero-width stop distance - skipping.")
        return
    target = entry_ref + stop_distance * RR_RATIO if direction == "LONG" else entry_ref - stop_distance * RR_RATIO

    equity = get_account_equity()
    risk_amount = equity * RISK_PER_TRADE_PCT / 100
    qty = int(risk_amount / stop_distance)
    if qty <= 0:
        log.warning("Computed qty <= 0 (risk_amount=%.2f stop_distance=%.4f) - skipping.", risk_amount, stop_distance)
        return

    log.info("%s breakout confirmed (range high=%.2f low=%.2f) - placing bracket: qty=%d stop=%.2f target=%.2f",
              direction, range_high, range_low, qty, stop, target)
    result = place_bracket_order(direction, qty, stop, target)
    log.info("Alpaca response: %s", result)


if __name__ == "__main__":
    check_and_trade()
