import asyncio
import os
import time
import json
from decimal import Decimal
from dotenv import load_dotenv
import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.signer import Signer
from py_clob_client.clob_types import OrderArgs, OrderType

load_dotenv()

# ================== CONFIG ==================
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

ENTRY_THRESHOLD = Decimal("0.65")
BUY_LIMIT_PRICE = Decimal("0.60")
TP_PRICE = Decimal("0.98")
SL_PRICE = Decimal("0.45")
MARTINGALE_SIZES = [5, 7, 10, 14, 20, 28]

# ================== P&L TRACKING ==================
total_pnl = 0.0
last_trade_pnl = 0.0
wins = 0
losses = 0
current_round = 1
consecutive_losses = 0
active_order_id = None
current_window_end = None
position_side = None
position_token_id = None
current_shares = 0
client = None

def get_next_bet_info():
    round_idx = min(current_round - 1, len(MARTINGALE_SIZES) - 1)
    shares = MARTINGALE_SIZES[round_idx]
    usd = round(shares * 0.60, 2)
    return shares, usd

def print_dashboard(event: str):
    shares, usd = get_next_bet_info()
    win_rate = round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0.0
    print("\n" + "="*80)
    print(f"\ud83e\udd16 POLYMARKET 5M BTC BOT - {event.upper()}")
    print(f"Time: {time.strftime('%H:%M:%S')} | Demo: {DEMO_MODE}")
    print(f"Round: {current_round} | Next Bet: ${usd} ({shares} shares)")
    print(f"Consecutive Losses: {consecutive_losses}/6")
    print(f"Last Trade: {'+' if last_trade_pnl >= 0 else ''}{last_trade_pnl:.2f} USD")
    print(f"Total P&L:   {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} USD")
    print(f"Wins: {wins} | Losses: {losses} | Win Rate: {win_rate}%")
    print("="*80 + "\n")

async def init_client():
    global client
    if DEMO_MODE:
        print("\ud83e\uddea DEMO MODE ACTIVE \u2014 No real orders placed")
        return
    try:
        signer = Signer(PRIVATE_KEY, POLYGON)
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            signer=signer,
            wallet_address=WALLET_ADDRESS
        )
        # Derive API credentials from wallet signature
        client.set_api_creds(client.create_or_derive_api_creds())
        print("\u2705 Live client ready")
    except Exception as e:
        print(f"\u274c Client init failed: {e}")
        raise

def get_current_btc_5m_markets():
    """Event-based discovery \u2014 live window only"""
    now = int(time.time())
    interval = 300
    current_ts = (now // interval) * interval
    print(f"[{time.strftime('%H:%M:%S')}] Searching for BTC 5m event via Gamma API...")

    # Try current live window only
    for offset in [0]:
        ts = current_ts + offset
        slug = f"btc-updown-5m-{ts}"
        try:
            resp = requests.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                events = data if isinstance(data, list) else [data] if data else []
                for event in events:
                    if event.get("slug") == slug:
                        for m in event.get("markets", []):
                            clob = m.get("clobTokenIds")
                            if isinstance(clob, str):
                                try:
                                    clob = json.loads(clob)
                                except:
                                    clob = None
                            if isinstance(clob, list) and len(clob) >= 2:
                                up_token = str(clob[0])
                                down_token = str(clob[1])
                                print(f"\u2705 FOUND MARKET: {slug}")
                                return {
                                    "up_token_id": up_token,
                                    "down_token_id": down_token,
                                    "window_end": ts + 300,
                                    "slug": slug
                                }
        except:
            pass

    # Fallback: scan active events
    try:
        resp = requests.get("https://gamma-api.polymarket.com/events",
                           params={"active": "true", "closed": "false", "limit": 50}, timeout=8)
        if resp.status_code == 200:
            events = resp.json()
            if isinstance(events, list):
                for event in events:
                    if "btc-updown-5m" in event.get("slug", ""):
                        for m in event.get("markets", []):
                            clob = m.get("clobTokenIds")
                            if isinstance(clob, str):
                                try:
                                    clob = json.loads(clob)
                                except:
                                    clob = None
                            if isinstance(clob, list) and len(clob) >= 2:
                                up_token = str(clob[0])
                                down_token = str(clob[1])
                                print(f"\u2705 FOUND via active events: {event.get('slug')}")
                                return {
                                    "up_token_id": up_token,
                                    "down_token_id": down_token,
                                    "window_end": int(time.time()) + 300,
                                    "slug": event.get("slug")
                                }
    except Exception as e:
        print(f"Active events fallback error: {e}")

    print("\u26a0\ufe0f  No active BTC 5m market found yet. Retrying...")
    return None

def get_best_ask_sync(token_id: str) -> Decimal:
    """Synchronous orderbook fetch \u2014 py-clob-client is not async"""
    if DEMO_MODE:
        import random
        return Decimal(str(round(0.48 + random.random() * 0.28, 4)))
    try:
        orderbook = client.get_order_book(token_id)
        # orderbook is an object with .asks attribute (list of dicts)
        asks = getattr(orderbook, "asks", None)
        if asks and len(asks) > 0:
            return Decimal(str(asks[0].price))
    except Exception as e:
        print(f"\u26a0\ufe0f Orderbook error: {e}")
    return Decimal("0.50")

async def get_best_ask(token_id: str) -> Decimal:
    """Run sync orderbook call in thread so it doesn't block the event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_best_ask_sync, token_id)

async def print_live_prices(markets):
    if
