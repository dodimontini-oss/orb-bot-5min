"""
One-off trade history report for a live ORB bot (works for both orb-bot-5min
and orb-bot-15min - identical script, deployed to both repos, each reading
its own Alpaca paper account via that repo's own secrets).

Read-only: reads Alpaca's own position and order records, prints a summary.
No orders are placed. Alpaca reports fills, not matched round-trip trades -
for a bracket order (entry + stop + target placed together), the CLOSING
leg's fill tells you whether the trade hit its stop or its target: a filled
leg whose price is close to the ORIGINAL entry order's stop_loss.stop_price
was a loser, close to take_profit.limit_price was a winner.

Environment variables required:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
"""

import os

import requests

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}


def get_account() -> dict:
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_positions() -> list:
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_all_orders() -> list:
    """ALL orders (filled, canceled, rejected, open) newest-first from
    Alpaca, re-sorted chronologically below - rejected/canceled orders are
    kept in the report since a missed/failed entry is exactly the kind of
    pattern this report needs to surface, not just the successful trades."""
    params = {"status": "all", "limit": 500, "direction": "desc", "nested": "true"}
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/orders", headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    orders = resp.json()
    return sorted(orders, key=lambda o: o.get("submitted_at") or o.get("created_at") or "")


def describe_bracket(o: dict) -> str:
    """For a bracket entry order, show its stop/target legs so a later fill
    can be matched to 'hit stop' vs 'hit target' by eye."""
    legs = o.get("legs") or []
    parts = []
    for leg in legs:
        lt = leg.get("type")
        if lt == "stop":
            parts.append(f"stop={leg.get('stop_price')}")
        elif lt == "limit":
            parts.append(f"target={leg.get('limit_price')}")
    return f" [{', '.join(parts)}]" if parts else ""


def run():
    account = get_account()
    equity = float(account["equity"])

    positions = get_positions()
    orders = get_all_orders()

    print(f"\nAlpaca account summary: equity=${equity:.2f}\n")

    print(f"=== OPEN POSITIONS ({len(positions)}) ===")
    if not positions:
        print("  (none)")
    for p in positions:
        symbol = p["symbol"]
        qty = float(p["qty"])
        side = p["side"]
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        upl = float(p["unrealized_pl"])
        upl_pct = float(p["unrealized_plpc"]) * 100
        print(f"  [{symbol}] {side.upper():<5} qty={qty} @ {entry:.2f} -> current {current:.2f} "
              f"| unrealized P/L={upl:+.2f} ({upl_pct:+.1f}%)")

    print(f"\n=== ALL ORDERS ({len(orders)}) - chronological, newest last ===")
    if not orders:
        print("  (none)")
    filled = rejected = canceled = 0
    realized_pl = 0.0
    entry_prices = {}  # symbol -> (side, qty, entry_price) for the currently-open leg
    for o in orders:
        symbol = o["symbol"]
        side = o["side"]
        status = o["status"]
        order_class = o.get("order_class", "")
        qty = o.get("filled_qty") or o.get("qty")
        avg_price = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        ts = (o.get("filled_at") or o.get("submitted_at") or o.get("created_at") or "?")[:19]
        bracket_info = describe_bracket(o) if order_class == "bracket" else ""

        if status == "filled":
            filled += 1
        elif status == "rejected":
            rejected += 1
        elif status in ("canceled", "expired"):
            canceled += 1

        price_str = f"{avg_price:.2f}" if avg_price is not None else "-"
        print(f"  [{symbol}] {side.upper():<4} {status:<10} qty={qty} @ {price_str} "
              f"{ts}{bracket_info}")

        if o.get("failed_at") or status == "rejected":
            reason = o.get("cancel_requested_at") or ""
            print(f"      -> REJECTED/FAILED (no fill)")

        if status == "filled" and avg_price is not None:
            if symbol not in entry_prices:
                entry_prices[symbol] = (side, float(qty), avg_price)
            else:
                open_side, open_qty, open_price = entry_prices.pop(symbol)
                closing_side = side
                if open_side == "buy" and closing_side == "sell":
                    pnl = (avg_price - open_price) * open_qty
                elif open_side == "sell" and closing_side == "buy":
                    pnl = (open_price - avg_price) * open_qty
                else:
                    pnl = None
                if pnl is not None:
                    realized_pl += pnl
                    print(f"      -> closed trade: entry {open_price:.2f} -> exit {avg_price:.2f} "
                          f"= {pnl:+.2f}")

    print(f"\n=== SUMMARY ===")
    print(f"Orders: {len(orders)} total - filled={filled} rejected={rejected} canceled/expired={canceled}")
    print(f"Open positions: {len(positions)}")
    print(f"Approx realized P/L from matched entry->exit pairs above: {realized_pl:+.2f}")
    if entry_prices:
        print(f"Note: {len(entry_prices)} symbol(s) have an unmatched open leg (still-open position or "
              f"an odd number of fills) - see OPEN POSITIONS above for the current live figure.")
    print()


if __name__ == "__main__":
    run()
