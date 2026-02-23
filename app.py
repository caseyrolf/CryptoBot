import os
import re
import time
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from coinbase import get_price
from store import Direction, init_store, Position, next_position_id, save_data, get_users, ensure_user, UserData

load_dotenv()
init_store()

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

USAGE = """**Crypto Futures Simulator Commands** (pretend trading):

• **buy** $AMOUNT of CRYPTO/USDT [LEVERAGE]x  
  e.g. buy $100 of SOL/USDT 20x    (default 10x if omitted)

• **sell** $AMOUNT of CRYPTO/USDT [LEVERAGE]x  
  (opens short position)

• **check balance** → shows USD + all positions with PNL

• **check standings** → portfolio value leaderboard

• **check positions** → show all user positions

• **close** CRYPTO/USDT → closes all positions for that pair, realizes PNL

• **close** POSITION_ID → closes only that specific position

• **price** CRYPTO → current spot price (e.g. price DOGE)

• **admin set** @user $AMOUNT → admin only

Most cryptos supported by Coinbase spot prices work (SOL, DOGE, AVAX, XRP, ADA, etc.).  
Leverage: 1x – 50x. All values are simulated — no real money involved!
"""


@app.event("app_mention")
def handle_mention(event, say, client, context):
    user = event["user"]
    bot_user_id = context["bot_user_id"] 
    mention_pattern = rf'<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*'
    text = re.sub(mention_pattern, '', event["text"], count=1).strip()
    user_data: UserData = ensure_user(user)

    # Flexible buy/sell parsing: allows any order without requiring 'of'
    side_match = re.search(r'\b(buy|sell|long|short)\b', text, re.IGNORECASE)
    margin_match = re.search(r'\$([\d.]+)', text)
    crypto_match = re.search(r'\b([A-Z]{2,10})/USDT\b', text, re.IGNORECASE)
    leverage_match = re.search(r'\b(\d+)x\b', text)
    if side_match and margin_match and crypto_match:
        side_str = side_match.group(1)
        margin_str = margin_match.group(1)
        crypto = crypto_match.group(1).upper()
        lev_str = leverage_match.group(1) if leverage_match else None
        side = Direction.LONG if side_str.lower() in ("buy", "long") else Direction.SHORT
        margin = float(margin_str)
        leverage = int(lev_str) if lev_str else 10   # default 10x if omitted

        if not (1 <= leverage <= 50):
            say("Leverage must be between 1x and 50x.")
            return

        if margin <= 0:
            say("Margin amount must be positive.")
            return

        if user_data.usd < margin:
            say(f"Insufficient USD balance. You have ${user_data.usd:.2f}")
            return

        try:
            entry_price = get_price(crypto)
        except ValueError as e:
            say(str(e))
            return

        pos = Position(
            position_id=next_position_id(),
            crypto=crypto,
            side=side,
            timestamp=int(time.time()),
            entry=entry_price,
            margin=margin,
            lev=leverage,
        )
        user_data.positions.append(pos)
        user_data.usd -= margin
        save_data()

        say(f"Opened **{side.value}** position on **{crypto}/USDT** "
            f"(ID: `{pos.position_id}`) "
            f"with **${margin:.2f}** margin at **{leverage}x** leverage. "
            f"Entry price: **${entry_price:,.2f}**"
            f"Liquidation price: **${pos.liquidation_price():,.2f}**")

    # For close command: support closing by ID or by symbol
    elif text.lower().startswith("close "):
        close_target = text[6:].strip()

        # close <position_id>
        if re.fullmatch(r"\d+", close_target):
            target_id = int(close_target)
            target_pos = next((pos for pos in user_data.positions if pos.position_id == target_id), None)
            if target_pos is None:
                say(f"No open position found with ID `{target_id}`.")
                return

            try:
                cur = get_price(target_pos.crypto)
            except ValueError as e:
                say(str(e))
                return

            pnl_pct = (cur - target_pos.entry) / target_pos.entry if target_pos.side == Direction.LONG else (target_pos.entry - cur) / target_pos.entry
            pnl = pnl_pct * target_pos.margin * target_pos.lev
            if pnl < -target_pos.margin:
                pnl = -target_pos.margin

            user_data.positions = [pos for pos in user_data.positions if pos.position_id != target_id]
            user_data.usd += target_pos.margin + pnl
            save_data()
            say(f"Closed position `{target_id}` (**{target_pos.side.value} {target_pos.crypto}/USDT**). "
                f"Realized PNL: **${pnl:+.2f}** → "
                f"New USD balance: **${user_data.usd:.2f}**")
            return

        # close CRYPTO/USDT
        pair_part = close_target.upper()
        if not re.match(r"^[A-Z]{2,10}/USDT$", pair_part):
            say("Usage: close CRYPTO/USDT  (e.g. close SOL/USDT) or close POSITION_ID (e.g. close 1)")
            return
        crypto = pair_part.split("/")[0]

        positions = user_data.positions
        new_pos = []
        closed_pnl = 0.0
        closed_margin = 0.0

        try:
            cur = get_price(crypto)
        except ValueError as e:
            say(str(e))
            return

        for pos in positions:
            if pos.crypto == crypto:
                pnl_pct = (cur - pos.entry) / pos.entry if pos.side == Direction.LONG else (pos.entry - cur) / pos.entry
                pnl = pnl_pct * pos.margin * pos.lev
                # Liquidation simulation: cap loss at margin
                if pnl < -pos.margin:
                    pnl = -pos.margin
                closed_pnl += pnl
                closed_margin += pos.margin
            else:
                new_pos.append(pos)

        if closed_margin > 0:
            user_data.positions = new_pos
            user_data.usd += closed_margin + closed_pnl
            save_data()
            say(f"Closed all **{crypto}/USDT** positions. "
                f"Realized PNL: **${closed_pnl:+.2f}** → "
                f"New USD balance: **${user_data.usd:.2f}**")
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
        usd = user_data.usd
        msg = f"**USD balance**: ${usd:.2f}\n\n"
        positions = user_data.positions
        if positions:
            msg += "**Open Positions**:\n"
            for pos in positions:
                try:
                    cur = get_price(pos.crypto)
                except ValueError:
                    cur = "N/A (price fetch failed)"
                pnl_pct = (cur - pos.entry) / pos.entry if pos.side == Direction.LONG and isinstance(cur, float) else \
                          (pos.entry - cur) / pos.entry if pos.side == Direction.SHORT and isinstance(cur, float) else 0
                pnl = pnl_pct * pos.margin * pos.lev
                if pnl < -pos.margin:
                    pnl = -pos.margin
                cur_str = f"${cur:,.2f}" if isinstance(cur, float) else str(cur)
                msg += (f"• **{pos.side.value} {pos.crypto}/USDT** "
                        f"(ID: `{pos.position_id}`) @{pos.lev}x | Margin: ${pos.margin:.2f} | "
                        f"Entry: ${pos.entry:,.2f} | Current: {cur_str} | "
                        f"PNL: **${pnl:+.2f}**\n")
        else:
            msg += "No open positions."
        say(msg)

    # In help command – update syntax
    elif text.lower() == "help":
        say(USAGE)

    elif text.lower() == "check positions":
        msg = "User Positions:\n"
        for u, udata in get_users().items():
            user_info = client.users_info(user=u)
            username = user_info["user"]["name"]
            msg += f"**{username}**:\n"
            if udata.positions:
                for pos in udata.positions:
                    try:
                        cur = get_price(pos.crypto)
                    except Exception:
                        msg += "Error fetching price for positions.\n"
                        break
                    pnl_pct = (cur - pos.entry) / pos.entry if pos.side == Direction.LONG else (pos.entry - cur) / pos.entry
                    pnl = pnl_pct * pos.margin * pos.lev
                    if pnl < -pos.margin:
                        pnl = -pos.margin
                    cur_str = f"${cur:,.2f}" if isinstance(cur, float) else str(cur)
                    msg += (f"• **{pos.side.value} {pos.crypto}/USDT** "
                            f"(ID: `{pos.position_id}`) @{pos.lev}x | Margin: ${pos.margin:.2f} | "
                            f"Entry: ${pos.entry:,.2f} | Current: {cur_str} | "
                            f"PNL: **${pnl:+.2f}**\n")
            else:
                msg += "No open positions.\n"
            msg += "\n"
        say(msg)

    elif text.lower() == "check standings":
        standings = []
        for u, udata in get_users().items():
            total = udata.usd
            for pos in udata.positions:
                try:
                    cur = get_price(pos.crypto)
                except Exception:
                    say("Error fetching price for standings.")
                    return
                pnl = ((cur - pos.entry) / pos.entry if pos.side == Direction.LONG else (pos.entry - cur) / pos.entry) * pos.margin * pos.lev
                if pnl < -pos.margin:
                    pnl = -pos.margin
                total += pos.margin + pnl
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
        positions = user_data.positions
        brag_msg = ""
        found_pos = False

        for pos in positions:
            if pos.crypto == crypto:
                found_pos = True
                try:
                    cur = get_price(crypto)
                except ValueError as e:
                    say(str(e))
                    return
                pnl_pct = (cur - pos.entry) / pos.entry if pos.side == Direction.LONG else (pos.entry - cur) / pos.entry
                pnl = pnl_pct * pos.margin * pos.lev
                if pnl < -pos.margin:
                    pnl = -pos.margin

                brag_msg += f"My {pos.side.value.lower()} position in {crypto}/USDT is on 🔥!\n"
                brag_msg += f"Entry price: ${pos.entry:,.2f}\n"
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
            target_data: UserData = ensure_user(target)
            target_data.usd = amt
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
            target_data: UserData = ensure_user(target)
            target_data.usd += amt
            save_data()
            say(f"Added ${amt:.2f} to <@{target}>'s USD balance")
        else:
            say("Usage: admin add @user $amount")
    else:
        say("Unknown command. Try '@cryptobot help'.")


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()



