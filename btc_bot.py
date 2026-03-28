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
from py_clob_client.clob_types import OrderArgs

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
    print(f"🤖 POLYMARKET 5M BTC BOT - {event.upper()}")
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
        print("🧪 DEMO MODE ACTIVE — No real orders placed")
        return
    try:
        signer = Signer(PRIVATE_KEY, POLYGON)
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            signer=signer,
            wallet_address=WALLET_ADDRESS
        )
        # Derive API credentials from wallet signature (MetaMask flow)
        client.set_api_creds(client.create_or_derive_api_creds())
        print("✅ Live client ready")
    except Exception as e:
        print(f"❌ Client init failed: {e}")
        raise

def get_current_btc_5m_markets():
    """Event-based discovery — live window only"""
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
                                print(f"✅ FOUND MARKET: {slug}")
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
                                print(f"✅ FOUND via active events: {event.get('slug')}")
                                return {
                                    "up_token_id": up_token,
                                    "down_token_id": down_token,
                                    "window_end": int(time.time()) + 300,
                                    "slug": event.get("slug")
                                }
    except Exception as e:
        print(f"Active events fallback error: {e}")

    print("⚠️  No active BTC 5m market found yet. Retrying...")
    return None

def get_best_ask_sync(token_id: str) -> Decimal:
    """Synchronous orderbook fetch — py-clob-client is not async"""
    if DEMO_MODE:
        import random
        return Decimal(str(round(0.48 + random.random() * 0.28, 4)))
    try:
        orderbook = client.get_order_book(token_id)
        # orderbook is an object with .asks attribute
        asks = getattr(orderbook, "asks", None)
        if asks and len(asks) > 0:
            return Decimal(str(asks[0].price))
    except Exception as e:
        print(f"⚠️ Orderbook error: {e}")
    return Decimal("0.50")

async def get_best_ask(token_id: str) -> Decimal:
    """Run sync orderbook call in thread so it doesn't block the event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_best_ask_sync, token_id)

async def print_live_prices(markets):
    if not markets:
        return
    up_price = await get_best_ask(markets["up_token_id"])
    down_price = await get_best_ask(markets["down_token_id"])
    print(f"[{time.strftime('%H:%M:%S')}] Window: {markets['slug']}")
    print(f"   BTC Up   → {up_price:.4f}     BTC Down → {down_price:.4f}")
    if up_price >= ENTRY_THRESHOLD:
        print(f"   🔥🔥 UP HIT {ENTRY_THRESHOLD} → ENTRY SIGNAL!")
    elif down_price >= ENTRY_THRESHOLD:
        print(f"   🔥🔥 DOWN HIT {ENTRY_THRESHOLD} → ENTRY SIGNAL!")
    else:
        print(f"   Waiting for either side to reach {ENTRY_THRESHOLD}...")
    print("-" * 80)

def place_order_sync(token_id: str, shares: int) -> str:
    """Place a real limit buy order — sync"""
    order_args = OrderArgs(
        token_id=token_id,
        price=float(BUY_LIMIT_PRICE),
        size=float(shares),
        side="BUY",
    )
    response = client.create_and_post_order(order_args)
    return response.get("orderID") or response.get("id") or str(response)

async def place_limit_buy(token_id: str, side: str):
    global active_order_id, position_side, position_token_id, current_shares
    shares, usd = get_next_bet_info()
    current_shares = shares
    print(f"🚀 {side} HIT {ENTRY_THRESHOLD} → BUYING {shares} shares @ ${BUY_LIMIT_PRICE} (${usd} USD)")
    if DEMO_MODE:
        print("🧪 DEMO: Buy order SIMULATED")
        active_order_id = "demo-" + str(int(time.time()))
        position_side = side
        position_token_id = token_id
        return
    try:
        loop = asyncio.get_event_loop()
        order_id = await loop.run_in_executor(None, place_order_sync, token_id, shares)
        active_order_id = order_id
        position_side = side
        position_token_id = token_id
        print(f"✅ REAL Order placed: {active_order_id}")
    except Exception as e:
        print(f"❌ Order failed: {e}")

async def close_position(reason: str):
    global active_order_id, total_pnl, last_trade_pnl, wins, losses, consecutive_losses, current_round, current_shares
    shares = current_shares
    if reason == "TP":
        pnl = round(shares * 0.38, 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        wins += 1
        consecutive_losses = 0
        current_round = 1
        print(f"🎉 TAKE PROFIT (+${pnl:.2f})")
    else:
        pnl = round(shares * -0.15, 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        losses += 1
        consecutive_losses += 1
        if consecutive_losses >= 6:
            print("🔄 6 losses → HARD RESET to Round 1")
            consecutive_losses = 0
            current_round = 1
        else:
            current_round += 1
            print(f"❌ STOP LOSS (-${abs(pnl):.2f}) → Next Round {current_round}")
    print_dashboard(f"{reason} COMPLETE")
    active_order_id = None

async def monitor_prices():
    global active_order_id, position_side, position_token_id, current_window_end
    print("🤖 Bot started — live window only mode\n")
    print_dashboard("STARTUP")

    last_print = 0
    while True:
        markets = get_current_btc_5m_markets()
        if markets:
            if markets.get("window_end") != current_window_end:
                print(f"🕒 NEW 5-MIN WINDOW → {markets.get('slug')}")
                active_order_id = None
                position_side = None
                current_window_end = markets.get("window_end")
                print_dashboard("NEW WINDOW")

            if time.time() - last_print > 2.5:
                await print_live_prices(markets)
                last_print = time.time()

            if active_order_id is None and position_side is None:
                up_price = await get_best_ask(markets["up_token_id"])
                down_price = await get_best_ask(markets["down_token_id"])
                if up_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["up_token_id"], "UP")
                elif down_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["down_token_id"], "DOWN")
        else:
            if time.time() - last_print > 20:
                print("⚠️  Still waiting for active BTC 5m market...")
                last_print = time.time()

        await asyncio.sleep(0.2)

async def monitor_position():
    """
    Demo: randomly simulates TP/SL outcomes.
    Live: polls real token price and triggers TP/SL based on market price.
    """
    global active_order_id
    while True:
        if active_order_id:
            if DEMO_MODE:
                import random
                if random.random() < 0.18:
                    if random.random() < 0.65:
                        await close_position("TP")
                    else:
                        await close_position("SL")
            else:
                # Live mode: check current price against TP/SL
                if position_token_id:
                    try:
                        current_price = await get_best_ask(position_token_id)
                        if current_price >= TP_PRICE:
                            print(f"🎯 Price {current_price} hit TP {TP_PRICE}")
                            await close_position("TP")
                        elif current_price <= SL_PRICE:
                            print(f"🛑 Price {current_price} hit SL {SL_PRICE}")
                            await close_position("SL")
                    except Exception as e:
                        print(f"⚠️ Position monitor error: {e}")
        await asyncio.sleep(0.25)

async def main():
    await init_client()
    await asyncio.gather(monitor_prices(), monitor_position())

if __name__ == "__main__":
    asyncio.run(main())
