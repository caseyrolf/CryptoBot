import os
import re
import json
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

load_dotenv()

# Data persistence file
DATA_FILE = "crypto_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"users": {}}

data = load_data()

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

def get_price(crypto):
    """Fetch spot price from Coinbase API. crypto should be e.g. 'BTC', 'SOL'"""
    pair = f"{crypto.upper()}-USD"
    url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception as e:
        raise ValueError(f"Could not fetch price for {crypto.upper()}/USD – possibly unsupported pair. Error: {str(e)}")

@app.event("app_mention")
def handle_mention(event, say, client, context):
    user = event["user"]
    bot_user_id = context["bot_user_id"] 
    mention_pattern = rf'<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*'
    text = re.sub(mention_pattern, '', event["text"], count=1).strip()

    if user not in data["users"]:
        data["users"][user] = {"usd": 0.0, "positions": []}
        save_data()

    # Flexible buy/sell parsing: allows any order without requiring 'of'
    side_match = re.search(r'\b(buy|sell)\b', text, re.IGNORECASE)
    margin_match = re.search(r'\$([\d.]+)', text)
    crypto_match = re.search(r'\b([A-Z]{2,10})/USDT\b', text)
    leverage_match = re.search(r'\b(\d+)x\b', text)
    if side_match and margin_match and crypto_match:
        side_str = side_match.group(1)
        margin_str = margin_match.group(1)
        crypto_raw = crypto_match.group(1)
        lev_str = leverage_match.group(1) if leverage_match else None
        side = "long" if side_str.lower() == "buy" else "short"
        margin = float(margin_str)
        crypto = crypto_raw.upper()   # e.g. SOL, DOGE, AVAX
        leverage = int(lev_str) if lev_str else 10   # default 10x if omitted

        if not (1 <= leverage <= 50):
            say("Leverage must be between 1x and 50x.")
            return

        if margin <= 0:
            say("Margin amount must be positive.")
            return

        if data["users"][user]["usd"] < margin:
            say(f"Insufficient USD balance. You have ${data['users'][user]['usd']:.2f}")
            return

        try:
            entry_price = get_price(crypto)
        except ValueError as e:
            say(str(e))
            return

        pos = {
            "crypto": crypto,
            "side": side,
            "entry": entry_price,
            "margin": margin,
            "lev": leverage
        }
        data["users"][user]["positions"].append(pos)
        data["users"][user]["usd"] -= margin
        save_data()

        say(f"Opened **{side.upper()}** position on **{crypto}/USDT** "
            f"with **${margin:.2f}** margin at **{leverage}x** leverage. "
            f"Entry price: **${entry_price:,.2f}**")

    # For close command: allow any crypto
    elif text.lower().startswith("close "):
        pair_part = text[6:].strip().upper()
        if not re.match(r"^[A-Z]{2,10}/USDT$", pair_part):
            say("Usage: close CRYPTO/USDT  (e.g. close SOL/USDT)")
            return
        crypto = pair_part.split("/")[0]

        positions = data["users"][user]["positions"]
        new_pos = []
        closed_pnl = 0.0
        closed_margin = 0.0

        for pos in positions:
            if pos["crypto"] == crypto:
                try:
                    cur = get_price(crypto)
                except ValueError as e:
                    say(str(e))
                    return
                pnl_pct = (cur - pos["entry"]) / pos["entry"] if pos["side"] == "long" else (pos["entry"] - cur) / pos["entry"]
                pnl = pnl_pct * pos["margin"] * pos["lev"]
                # Liquidation simulation: cap loss at margin
                if pnl < -pos["margin"]:
                    pnl = -pos["margin"]
                closed_pnl += pnl
                closed_margin += pos["margin"]
            else:
                new_pos.append(pos)

        if closed_margin > 0:
            data["users"][user]["positions"] = new_pos
            data["users"][user]["usd"] += closed_margin + closed_pnl
            save_data()
            say(f"Closed all **{crypto}/USDT** positions. "
                f"Realized PNL: **${closed_pnl:+.2f}** → "
                f"New USD balance: **${data['users'][user]['usd']:.2f}**")
        else:
            say(f"No open positions found for **{crypto}/USDT**.")

    # Price command: support any crypto
    elif text.lower().startswith("price "):
        crypto_raw = text[6:].strip().upper()
        crypto = crypto_raw.rstrip("/USDT")  # allow "price SOL/USDT" or "price SOL"
        try:
            cur = get_price(crypto)
            say(f"Current **{crypto}/USD** spot price: **${cur:,.2f}**")
        except ValueError as e:
            say(str(e))

    # In check balance: show leverage in positions
    elif text.lower() == "check balance":
        usd = data["users"][user]["usd"]
        msg = f"**USD balance**: ${usd:.2f}\n\n"
        positions = data["users"][user]["positions"]
        if positions:
            msg += "**Open Positions**:\n"
            for pos in positions:
                try:
                    cur = get_price(pos["crypto"])
                except ValueError:
                    cur = "N/A (price fetch failed)"
                pnl_pct = (cur - pos["entry"]) / pos["entry"] if pos["side"] == "long" and isinstance(cur, float) else \
                          (pos["entry"] - cur) / pos["entry"] if pos["side"] == "short" and isinstance(cur, float) else 0
                pnl = pnl_pct * pos["margin"] * pos["lev"]
                if pnl < -pos["margin"]:
                    pnl = -pos["margin"]
                cur_str = f"${cur:,.2f}" if isinstance(cur, float) else str(cur)
                msg += (f"• **{pos['side'].upper()} {pos['crypto']}/USDT** "
                        f"@{pos['lev']}x | Margin: ${pos['margin']:.2f} | "
                        f"Entry: ${pos['entry']:,.2f} | Current: {cur_str} | "
                        f"PNL: **${pnl:+.2f}**\n")
        else:
            msg += "No open positions."
        say(msg)

    # In help command – update syntax
    elif text.lower() == "help":
        msg = """**Crypto Futures Simulator Commands** (pretend trading):

• **buy** $AMOUNT of CRYPTO/USDT [LEVERAGE]x  
  e.g. buy $100 of SOL/USDT 20x    (default 10x if omitted)

• **sell** $AMOUNT of CRYPTO/USDT [LEVERAGE]x  
  (opens short position)

• **check balance** → shows USD + all positions with PNL

• **check standings** → portfolio value leaderboard

• **check positions** → show all user positions

• **close** CRYPTO/USDT → closes all positions for that pair, realizes PNL

• **price** CRYPTO → current spot price (e.g. price DOGE)

• **admin set** @user $AMOUNT → admin only

Most cryptos supported by Coinbase spot prices work (SOL, DOGE, AVAX, XRP, ADA, etc.).  
Leverage: 1x – 50x. All values are simulated — no real money involved!
"""
        say(msg)

    elif text.lower() == "check positions":
        msg = "User Positions:\n"
        for u, udata in data["users"].items():
            user_info = client.users_info(user=u)
            username = user_info["user"]["name"]
            msg += f"**{username}**:\n"
            if udata["positions"]:
                for pos in udata["positions"]:
                    try:
                        cur = get_price(pos["crypto"])
                    except Exception:
                        msg += "Error fetching price for positions.\n"
                        break
                    pnl_pct = (cur - pos["entry"]) / pos["entry"] if pos["side"] == "long" else (pos["entry"] - cur) / pos["entry"]
                    pnl = pnl_pct * pos["margin"] * pos["lev"]
                    if pnl < -pos["margin"]:
                        pnl = -pos["margin"]
                    cur_str = f"${cur:,.2f}" if isinstance(cur, float) else str(cur)
                    msg += (f"• **{pos['side'].upper()} {pos['crypto']}/USDT** "
                            f"@{pos['lev']}x | Margin: ${pos['margin']:.2f} | "
                            f"Entry: ${pos['entry']:,.2f} | Current: {cur_str} | "
                            f"PNL: **${pnl:+.2f}**\n")
            else:
                msg += "No open positions.\n"
            msg += "\n"
        say(msg)

    elif text.lower() == "check standings":
        standings = []
        for u, udata in data["users"].items():
            total = udata["usd"]
            for pos in udata["positions"]:
                try:
                    cur = get_price(pos["crypto"])
                except Exception:
                    say("Error fetching price for standings.")
                    return
                pnl = ((cur - pos["entry"]) / pos["entry"] if pos["side"] == "long" else (pos["entry"] - cur) / pos["entry"]) * pos["margin"] * pos["lev"]
                if pnl < -pos["margin"]:
                    pnl = -pos["margin"]
                total += pos["margin"] + pnl
            # Get username
            user_info = client.users_info(user=u)
            username = user_info["user"]["name"]
            standings.append((username, total))
        standings.sort(key=lambda x: x[1], reverse=True)
        msg = "Standings:\n"
        for name, val in standings:
            msg += f"{name} = ${val:.2f}\n"
        say(msg)

    elif text.lower().startswith("brag "):
        crypto_raw = text[5:].strip().upper()
        crypto = crypto_raw.rstrip("/USDT")  # allow "brag SOL/USDT" or "brag SOL"
        positions = data["users"][user]["positions"]
        brag_msg = ""
        found_pos = False

        for pos in positions:
            if pos["crypto"] == crypto:
                found_pos = True
                try:
                    cur = get_price(crypto)
                except ValueError as e:
                    say(str(e))
                    return
                pnl_pct = (cur - pos["entry"]) / pos["entry"] if pos["side"] == "long" else (pos["entry"] - cur) / pos["entry"]
                pnl = pnl_pct * pos["margin"] * pos["lev"]
                if pnl < -pos["margin"]:
                    pnl = -pos["margin"]

                brag_msg += f"My {pos['side']} position in {crypto}/USDT is on 🔥!\n"
                brag_msg += f"Entry price: ${pos['entry']:,.2f}\n"
                brag_msg += f"Current price: ${cur:,.2f}\n"
                brag_msg += f"PNL: ${pnl:+.2f}\n"
                brag_msg += "🚀🚀🚀 TO THE MOON! 🚀🚀🚀" + "\n"

        if not found_pos:
            say(f"You don't have an open position in {crypto}/USDT.")
            return

        say(brag_msg)

    elif text.startswith("admin set "):
        if user != os.environ.get("ADMIN_ID"):
            say("You are not an admin.")
            return
        match = re.match(r"admin set <@(\w+)> \$(\d+)", text)
        if match:
            target = match.group(1)
            amt = float(match.group(2))
            if target not in data["users"]:
                data["users"][target] = {"usd": 0.0, "positions": []}
            data["users"][target]["usd"] = amt
            save_data()
            say(f"Set <@{target}>'s USD balance to ${amt:.2f}")
        else:
            say("Usage: admin set @user $amount")

    elif text.startswith("admin add "):
        if user != os.environ.get("ADMIN_ID"):
            say("You are not an admin.")
            return
        match = re.match(r"admin add <@(\w+)> \$(\d+)", text)
        if match:
            target = match.group(1)
            amt = float(match.group(2))
            if target not in data["users"]:
                data["users"][target] = {"usd": 0.0, "positions": []}
            data["users"][target]["usd"] += amt
            save_data()
            say(f"Added ${amt:.2f} to <@{target}>'s USD balance")
        else:
            say("Usage: admin add @user $amount")
    else:
        say("Unknown command. Try '@cryptobot help'.")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
