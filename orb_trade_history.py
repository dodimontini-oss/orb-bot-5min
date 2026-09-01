"""
One-off trade history report for a live ORB bot (works for both orb-bot-5min
and orb-bot-15min - identical script, deployed to both repos, each reading
its own Alpaca paper account via that repo's own secrets).

Read-only: reads Alpaca's own position and order records, prints a summary.
No orders are placed.

IMPORTANT correctness note (learned the hard way on the first version of
this script): every ORB entry is a BRACKET order (order_class="bracket").
Alpaca nests the stop/target CHILD legs inside the parent entry order's
"legs" field - they do NOT appear as separate top-level fills in
GET /v2/orders. A naive "pair the Nth fill with the (N+1)th same-symbol
fill" heuristic is WRONG here: two independent entries for the same symbol
(e.g. a stopped-out short followed by an unrelated new long) look identical
to a real entry->exit pair, and get silently mis-paired into a fake P&L.
This version reads each entry's OWN nested legs to find its real exit
(whichever leg has status="filled"), so pairing is by actual parent/child
relationship, never by chronological guesswork.

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
    """Top-level orders only, newest-first from Alpaca, re-sorted
    chronologically below. nested=true so each bracket entry's stop/target
    child legs come back attached in "legs" rather than as separate
    top-level entries (which would otherwise let a leg fill get mistaken
    for an unrelated new entry)."""
    params = {"status": "all", "limit": 500, "direction": "desc", "nested": "true"}
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/orders", headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    orders = resp.json()
    return sorted(orders, key=lambda o: o.get("submitted_at") or o.get("created_at") or "")


def find_filled_leg(order: dict):
    """Returns (kind, leg_dict) for whichever bracket child leg has
    actually filled - 'target' (take_profit/limit) or 'stop'
    (stop_loss/stop) - or (None, None) if neither has filled yet (position
    still open, or the entry itself never filled)."""
    for leg in order.get("legs") or []:
        if leg.get("status") == "filled":
            kind = "target" if leg.get("type") == "limit" else "stop"
            return kind, leg
    return None, None


def run():
    account = get_account()
    equity = float(account["equity"])

    positions = get_positions()
    orders = get_all_orders()
    entries = [o for o in orders if o.get("order_class") == "bracket"]

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

    print(f"\n=== BRACKET ENTRIES ({len(entries)}) - chronological, each with its own real exit ===")
    if not entries:
        print("  (none)")
    filled_entries = rejected_entries = closed_trades = wins = losses = 0
    realized_pl = 0.0
    for o in entries:
        symbol = o["symbol"]
        side = o["side"]
        status = o["status"]
        qty = o.get("filled_qty") or o.get("qty")
        entry_price = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        ts = (o.get("filled_at") or o.get("submitted_at") or "?")[:19]

        legs = o.get("legs") or []
        stop_price = next((leg.get("stop_price") for leg in legs if leg.get("type") == "stop"), None)
        target_price = next((leg.get("limit_price") for leg in legs if leg.get("type") == "limit"), None)

        if status == "rejected" or o.get("failed_at"):
            rejected_entries += 1
            print(f"  [{symbol}] {side.upper():<4} REJECTED/FAILED - no fill, no trade taken. "
                  f"submitted {ts}")
            continue
        if status != "filled" or entry_price is None:
            print(f"  [{symbol}] {side.upper():<4} {status:<10} (entry did not fill) submitted {ts}")
            continue

        filled_entries += 1
        print(f"  [{symbol}] {side.upper():<4} ENTRY filled qty={qty} @ {entry_price:.2f} {ts} "
              f"[stop={stop_price}, target={target_price}]")

        kind, leg = find_filled_leg(o)
        if kind is None:
            print(f"      -> still open (neither stop nor target has filled) - see OPEN POSITIONS above")
            continue

        exit_price = float(leg["filled_avg_price"])
        exit_ts = (leg.get("filled_at") or "?")[:19]
        direction = 1 if side == "buy" else -1
        pnl = (exit_price - entry_price) * float(qty) * direction
        realized_pl += pnl
        closed_trades += 1
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        print(f"      -> exit: {kind.upper()} hit @ {exit_price:.2f} {exit_ts}  =  {pnl:+.2f}")

    print(f"\n=== SUMMARY ===")
    print(f"Bracket entries: {len(entries)} total - filled={filled_entries} rejected/failed={rejected_entries}")
    print(f"Closed trades (stop or target actually hit): {closed_trades}  (wins={wins} losses={losses})")
    if closed_trades:
        print(f"Win rate: {wins / closed_trades * 100:.1f}%")
    print(f"Realized P/L from closed trades: {realized_pl:+.2f}")
    print(f"Open positions: {len(positions)}")
    print()


if __name__ == "__main__":
    run()
