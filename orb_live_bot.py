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
   position carries past today's close). QQQ is a stock, so - unlike the
   crypto bot - this gets a REAL broker-side stop-loss, no virtual
   stop-checking workaround needed. Alpaca supports short selling for
   stocks too, so both LONG and SHORT signals are traded live, matching
   the backtest.
5. EXCEPTION to "no session-close flatten" - Friday-flatten-if-profitable
   (added 2026-09-06, see orb_weekend_and_stop_lab.py /
   project_orb_futures_strategy_findings memory): on a Friday, inside
   FRIDAY_FLATTEN_START-MARKET_CLOSE ET, if still holding a position AND
   its unrealized_pl is positive, cancel the resting bracket legs and
   close it at market instead of letting it ride through the weekend.
   Backtested to beat plain hold-through on every metric (PF, net P&L%,
   max drawdown), walk-forward validated on both timeframes - this is NOT
   a discretionary override, it's a validated rule. Losing positions are
   NOT touched by this - they keep riding the normal bracket stop/target
   exactly as before. The window is 30 minutes wide (not just the literal
   last tick) specifically so an occasional missed/delayed GitHub Actions
   run (see feedback_github_actions_deployment memory - cron firing isn't
   perfectly reliable) still gets multiple 5-minute chances to catch it
   before the session actually ends.
6. RELATIVE-VOLUME CONFLUENCE FILTER (REL_VOL_THRESHOLD) - added
   2026-09-06 after a bug fix to the backtest (same-day stop/target
   resolution - see orb_weekend_and_stop_lab.py) revealed BASELINE
   (no filter) has NEGATIVE expectancy on the 5-minute timeframe under a
   more faithful model (PF 0.973 full-history), while adding this filter
   recovers a real, walk-forward-validated edge (PF 1.232 full-history,
   >1.0 in BOTH the early and late half - see project_orb_futures_
   strategy_findings memory, 2026-09-06 entry). Skips the entry if
   today's opening-range volume is below REL_VOL_THRESHOLD x the trailing
   REL_VOL_LOOKBACK_DAYS sessions' average volume for that same opening
   slot (exact definition matches orb_backtest_lab.py's relvol filter).
   SET DIFFERENTLY PER REPO, same as ORB_WINDOW_MINUTES:
   orb-bot-5min uses REL_VOL_THRESHOLD=1.5 (this is the config that fixes
   it); orb-bot-15min uses REL_VOL_THRESHOLD=None (disabled) because its
   own BASELINE already has solidly positive, walk-forward-validated
   edge and the filter would only trade away most of its return for a
   smaller drawdown improvement - not worth it there. A None threshold
   makes relvol_ok() always return True (a complete no-op), so this is
   the only other line that differs between the two repos' copies of
   this file besides ORB_WINDOW_MINUTES itself.

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
from datetime import datetime, time as dtime, timedelta
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
ORB_WINDOW_MINUTES = 5  # differs between orb-bot-5min (5) and orb-bot-15min (15)
RR_RATIO = 2.0
RISK_PER_TRADE_PCT = 1.0
REL_VOL_THRESHOLD = 1.5  # differs: 1.5 here (5min), None on orb-bot-15min (disabled) - see docstring point 6
REL_VOL_LOOKBACK_DAYS = 20

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
FRIDAY_FLATTEN_START = dtime(15, 30)  # last 30 min of a Friday session - see docstring point 5


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


def get_recent_bars(days_back: int) -> pd.DataFrame:
    """5-min bars from `days_back` calendar days ago through now, paginated.
    Only called when REL_VOL_THRESHOLD is set - wide enough (net of
    weekends/holidays) to comfortably cover REL_VOL_LOOKBACK_DAYS full
    trading sessions for the relative-volume filter."""
    now_et = datetime.now(ET)
    start = (now_et - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    all_rows = []
    page_token = None
    while True:
        params = {
            "timeframe": "5Min",
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "feed": "iex",
        }
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/bars", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_rows.extend(data.get("bars", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["time_et"] = df["t"].dt.tz_convert(ET)
    df["session_date"] = df["time_et"].dt.date
    df["hm"] = df["time_et"].dt.strftime("%H:%M")
    return df.sort_values("t").reset_index(drop=True)


def relvol_ok(today_orb_volume: float, today_session_date) -> bool:
    """True if REL_VOL_THRESHOLD is None (filter disabled - orb-bot-15min)
    or today's opening-range volume is >= REL_VOL_THRESHOLD x the trailing
    REL_VOL_LOOKBACK_DAYS sessions' average opening-range volume (same
    slot) - matches orb_backtest_lab.py's relvol filter exactly. Fails
    SAFE (returns False, skipping the entry) on any data problem rather
    than trading without the filter it was asked to apply."""
    if REL_VOL_THRESHOLD is None:
        return True
    hist = get_recent_bars(days_back=REL_VOL_LOOKBACK_DAYS * 2 + 10)
    if hist.empty:
        log.warning("relvol filter: no historical bars returned - failing safe (skipping entry).")
        return False
    orb_end_hm = _orb_end_hm(ORB_WINDOW_MINUTES)
    past_sessions = sorted(s for s in hist["session_date"].unique() if s < today_session_date)
    past_sessions = past_sessions[-REL_VOL_LOOKBACK_DAYS:]
    if len(past_sessions) < 5:
        log.warning("relvol filter: only %d prior session(s) available (<5) - failing safe (skipping entry).",
                     len(past_sessions))
        return False
    past_vols = []
    for s in past_sessions:
        day_bars = hist[(hist["session_date"] == s) & (hist["hm"] >= "09:30") & (hist["hm"] <= orb_end_hm)]
        if not day_bars.empty:
            past_vols.append(day_bars["volume"].sum())
    if not past_vols:
        return False
    avg_vol = sum(past_vols) / len(past_vols)
    ratio = (today_orb_volume / avg_vol) if avg_vol > 0 else 0
    log.info("relvol check: today's opening volume=%.0f, %d-session avg=%.0f, ratio=%.2fx (need >= %.1fx)",
              today_orb_volume, len(past_vols), avg_vol, ratio, REL_VOL_THRESHOLD)
    return avg_vol > 0 and ratio >= REL_VOL_THRESHOLD


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


def get_account_info() -> dict:
    resp = requests.get(f"{TRADING_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


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
    if resp.status_code >= 400:
        log.error("Alpaca rejected the order (status %d): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def flatten_position(position: dict) -> dict:
    """Cancel any resting bracket legs (a bare closing order can otherwise
    get rejected - Alpaca reserves qty against open sell/buy-to-cover
    orders) then submit a plain market order to close the position."""
    for order in get_open_orders():
        del_resp = requests.delete(f"{TRADING_BASE_URL}/v2/orders/{order['id']}", headers=HEADERS, timeout=10)
        if del_resp.status_code >= 400:
            log.error("Failed to cancel resting order %s (status %d): %s",
                       order["id"], del_resp.status_code, del_resp.text)
    qty = abs(float(position["qty"]))
    side = "sell" if position["side"] == "long" else "buy"
    body = {"symbol": SYMBOL, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day"}
    resp = requests.post(f"{TRADING_BASE_URL}/v2/orders", headers=HEADERS, json=body, timeout=15)
    if resp.status_code >= 400:
        log.error("Alpaca rejected the Friday-flatten close order (status %d): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def check_and_trade():
    if not market_is_open_now():
        log.info("Outside regular market hours (9:30-16:00 ET, weekdays). No action.")
        return

    position = get_position()
    if position is not None and float(position["qty"]) != 0:
        now_et = datetime.now(ET)
        unrealized_pl = float(position.get("unrealized_pl", 0))
        if now_et.weekday() == 4 and FRIDAY_FLATTEN_START <= now_et.time() <= MARKET_CLOSE \
                and unrealized_pl > 0:
            log.info("Friday-flatten window, in a position (%s %s shares, unrealized P&L $%.2f > 0) - "
                      "closing now to lock in profit before the weekend instead of holding through it "
                      "(validated rule, see orb_live_bot.py docstring point 5).",
                      position["side"], position["qty"], unrealized_pl)
            result = flatten_position(position)
            log.info("Alpaca response: %s", result)
            return
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

    if not relvol_ok(orb_bars["volume"].sum(), datetime.now(ET).date()):
        log.info("%s breakout confirmed (range high=%.2f low=%.2f) but relative-volume confluence filter "
                  "failed - skipping (see docstring point 6 / project_orb_futures_strategy_findings memory).",
                  direction, range_high, range_low)
        return

    entry_ref = rest.iloc[-1]["close"]  # reference for sizing/target only - actual fill is the live market price
    stop = range_low if direction == "LONG" else range_high
    stop_distance = abs(entry_ref - stop)
    if stop_distance <= 0:
        log.warning("Zero-width stop distance - skipping.")
        return

    # Guards against a delayed run (e.g. a missed cron trigger) acting on a
    # breakout that's gone stale - if the current reference price has
    # already moved past the fixed stop level, the stop is invalid before
    # it's even submitted (Alpaca enforces stop_price vs. current price on
    # bracket orders and rejects it - this is exactly what happened live on
    # 2026-08-28, see project_orb_futures_strategy_findings memory). No
    # order gets recorded on a local skip, so this re-checks - and can
    # still fire - on every future run this session if price comes back to
    # a level where the stop makes sense again; it doesn't burn today's
    # one-attempt slot.
    STOP_SANITY_BUFFER = 0.01  # matches Alpaca's own minimum stop-vs-price gap
    if direction == "LONG" and entry_ref <= stop + STOP_SANITY_BUFFER:
        log.warning("Stale breakout - price (%.2f) has fallen back through the stop (%.2f). Skipping, will "
                     "recheck next run.", entry_ref, stop)
        return
    if direction == "SHORT" and entry_ref >= stop - STOP_SANITY_BUFFER:
        log.warning("Stale breakout - price (%.2f) has risen back through the stop (%.2f). Skipping, will "
                     "recheck next run.", entry_ref, stop)
        return

    target = entry_ref + stop_distance * RR_RATIO if direction == "LONG" else entry_ref - stop_distance * RR_RATIO

    account = get_account_info()
    equity = float(account["equity"])
    buying_power = float(account["buying_power"])
    risk_amount = equity * RISK_PER_TRADE_PCT / 100
    qty = int(risk_amount / stop_distance)
    # Risk-based sizing can call for more notional than the account can
    # actually pay for (a tight stop on a high-priced instrument sizes up
    # fast) - cap qty at what's affordable so it never gets rejected for
    # insufficient buying power (this is what happened live on 2026-08-28).
    # Only ever shrinks the position, never grows it beyond the 1%-risk size.
    max_affordable_qty = int(buying_power / entry_ref)
    qty = min(qty, max_affordable_qty)
    if qty <= 0:
        log.warning("Computed qty <= 0 (risk_amount=%.2f stop_distance=%.4f buying_power=%.2f) - skipping.",
                     risk_amount, stop_distance, buying_power)
        return

    log.info("%s breakout confirmed (range high=%.2f low=%.2f) - placing bracket: qty=%d stop=%.2f target=%.2f",
              direction, range_high, range_low, qty, stop, target)
    result = place_bracket_order(direction, qty, stop, target)
    log.info("Alpaca response: %s", result)


if __name__ == "__main__":
    check_and_trade()
