import os
import re
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from coinbase import (
    get_price,
    should_fill_limit_order,
    should_liquidate,
    should_stop_loss,
    should_take_profit, get_prices,
)
from store import (
    Direction,
    Position,
    UserData,
    ensure_user,
    get_users,
    init_store,
    next_position_id,
    save_data,
)

load_dotenv()
init_store()

_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

app = App(token=_TOKEN, signing_secret=_SIGNING_SECRET)


USAGE = """**Crypto Futures Simulator Commands**:

• **buy** $AMOUNT of CRYPTO/USDT [LEVERAGE]x [at $PRICE]
  e.g. buy $100 of SOL/USDT 20x    (default 10x if omitted)

• **sell** $AMOUNT of CRYPTO/USDT [LEVERAGE]x [at $PRICE]
  (opens short position)

• **check balance** → shows USD + all positions with PNL

• **check standings** → portfolio value leaderboard

• **check positions** → show all user positions

• **close** CRYPTO/USDT → closes all positions for that pair, realizes PNL

• **close all** → closes all open positions

• **close** POSITION_ID → closes only that specific position

• **position** POSITION_ID **set tp** $AMOUNT

• **position** POSITION_ID **set stop** $AMOUNT

• **price** CRYPTO → current spot price (e.g. price DOGE)

• **admin set** @user $AMOUNT → admin only

Most cryptos supported by Coinbase spot prices work (SOL, DOGE, AVAX, XRP, ADA, etc.).
Leverage: 1x - 50x.
"""


def _calculate_pnl(position: Position, current_price: float) -> float:
    pnl_pct = (
        (current_price - position.entry) / position.entry
        if position.side == Direction.LONG
        else (position.entry - current_price) / position.entry
    )
    pnl = pnl_pct * position.margin * position.lev
    return max(pnl, -position.margin)


def _close_position(user_data: UserData, position: Position, current_price: float) -> float:
    pnl = _calculate_pnl(position, current_price)
    user_data.usd += position.margin + pnl
    user_data.positions = [pos for pos in user_data.positions if pos.position_id != position.position_id]
    return pnl


def _update_positions() -> tuple[list[str], list[str]]:
    changed = False
    fill_msgs: list[str] = []
    position_msgs: list[str] = []

    for uid, udata in get_users().items():
        remaining_orders: list[Position] = []
        for order in udata.orders:
            fill_ts = should_fill_limit_order(order)
            if fill_ts is None:
                remaining_orders.append(order)
                continue

            changed = True
            order.timestamp = fill_ts
            if order.take_profit is not None and order.tp_timestamp is None:
                order.tp_timestamp = fill_ts
            if order.stop_loss is not None and order.stop_timestamp is None:
                order.stop_timestamp = fill_ts
            udata.positions.append(order)

            liq_price = order.liquidation_price()
            liq_str = f"${liq_price:,.2f}" if liq_price is not None else "N/A"
            fill_msgs.append(
                f"Limit order `{order.position_id}` for <@{uid}> filled "
                f"(**{order.side.value} {order.crypto}/USDT**). "
                f"Entry price: **${order.entry:,.2f}** | Liquidation price: **{liq_str}**."
            )
        udata.orders = remaining_orders

        survivors: list[Position] = []
        for pos in udata.positions:
            try:
                cur = get_price(pos.crypto)
            except ValueError:
                survivors.append(pos)
                continue

            tp_hit = should_take_profit(pos)
            stop_hit = should_stop_loss(pos)

            if tp_hit or stop_hit:
                changed = True
                pnl = _calculate_pnl(pos, cur)
                udata.usd += pos.margin + pnl
                trigger_name = "take profit" if tp_hit else "stop loss"
                trigger_price = pos.take_profit if tp_hit else pos.stop_loss
                position_msgs.append(
                    f"Position `{pos.position_id}` for <@{uid}> closed by {trigger_name} "
                    f"(**{pos.side.value} {pos.crypto}/USDT**). "
                    f"Trigger: **${trigger_price:,.2f}** | Exit: **${cur:,.2f}** | "
                    f"Realized PNL: **${pnl:+.2f}**."
                )
                continue

            if should_liquidate(pos):
                changed = True
                position_msgs.append(
                    f"Position `{pos.position_id}` for <@{uid}> was liquidated "
                    f"(**{pos.side.value} {pos.crypto}/USDT**). "
                    f"Realized PNL: **${(-pos.margin):+.2f}**."
                )
                continue

            survivors.append(pos)

        udata.positions = survivors

    if changed:
        save_data()

    return fill_msgs, position_msgs


def _background_position_updater():
    while True:
        time.sleep(600)
        fill_msgs, position_msgs = _update_positions()
        if _CHANNEL_ID:
            for msg in fill_msgs:
                try:
                    app.client.chat_postMessage(channel=_CHANNEL_ID, text=msg)
                except Exception as e:
                    print(f"Failed to post fill alert to Slack: {e}")
            for msg in position_msgs:
                try:
                    app.client.chat_postMessage(channel=_CHANNEL_ID, text=msg)
                except Exception as e:
                    print(f"Failed to post position alert to Slack: {e}")


@app.event("app_mention")
def handle_mention(event, say, client, context):
    user = event["user"]
    bot_user_id = context["bot_user_id"]
    mention_pattern = rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*"
    text = re.sub(mention_pattern, "", event["text"], count=1).strip()
    user_data: UserData = ensure_user(user)

    # Auto-fill limit orders and trigger TP/stop/liquidation on every mention.
    fill_msgs, position_msgs = _update_positions()
    for msg in fill_msgs:
        say(msg)
    for msg in position_msgs:
        say(msg)

    # Flexible buy/sell parsing: allows any order without requiring 'of'
    side_match = re.search(r"\b(buy|sell|long|short)\b", text, re.IGNORECASE)
    margin_match = re.search(r"\$([\d.]+)", text)
    crypto_match = re.search(r"\b([A-Z]{2,10})/USDT\b", text, re.IGNORECASE)
    leverage_match = re.search(r"\b(\d+)x\b", text)
    limit_price_match = re.search(r"\bat\s+\$([\d.]+)\b", text, re.IGNORECASE)
    if side_match and margin_match and crypto_match:
        side_str = side_match.group(1)
        margin_str = margin_match.group(1)
        crypto = crypto_match.group(1).upper()
        lev_str = leverage_match.group(1) if leverage_match else None
        side = Direction.LONG if side_str.lower() in ("buy", "long") else Direction.SHORT
        margin = float(margin_str)
        leverage = int(lev_str) if lev_str else 10
        limit_price = float(limit_price_match.group(1)) if limit_price_match else None

        if not (1 <= leverage <= 50):
            say("Leverage must be between 1x and 50x.")
            return

        if margin <= 0:
            say("Margin amount must be positive.")
            return
        if limit_price is not None and limit_price <= 0:
            say("Limit price must be positive.")
            return

        if user_data.usd < margin:
            say(f"Insufficient USD balance. You have ${user_data.usd:.2f}")
            return

        try:
            spot_price = get_price(crypto)
        except ValueError as e:
            say(str(e))
            return

        if limit_price is not None:
            if side == Direction.LONG and spot_price < limit_price:
                say(
                    f"Cannot place LONG limit at ${limit_price:,.2f}: "
                    f"current price is already below it (${spot_price:,.2f})."
                )
                return
            if side == Direction.SHORT and spot_price > limit_price:
                say(
                    f"Cannot place SHORT limit at ${limit_price:,.2f}: "
                    f"current price is already above it (${spot_price:,.2f})."
                )
                return

        pos = Position(
            position_id=next_position_id(),
            crypto=crypto,
            side=side,
            timestamp=int(time.time()),
            entry=limit_price if limit_price is not None else spot_price,
            margin=margin,
            lev=leverage,
        )

        if limit_price is None:
            user_data.positions.append(pos)
        else:
            user_data.orders.append(pos)

        user_data.usd -= margin
        save_data()

        liq_price = pos.liquidation_price()
        liq_str = f"${liq_price:,.2f}" if liq_price is not None else "N/A"
        if limit_price is None:
            say(
                f"Opened **{side.value}** position on **{crypto}/USDT** (ID: `{pos.position_id}`) "
                f"with **${margin:.2f}** margin at **{leverage}x** leverage. "
                f"Entry price: **${spot_price:,.2f}** | Liquidation price: **{liq_str}**"
            )
        else:
            say(
                f"Placed **{side.value}** limit order on **{crypto}/USDT** (ID: `{pos.position_id}`) "
                f"with **${margin:.2f}** margin at **{leverage}x** leverage "
                f"to fill at **${limit_price:,.2f}**. Estimated liquidation price: **{liq_str}**"
            )
        return

    # position POSITION_ID set tp/stop $AMOUNT
    position_set_match = re.fullmatch(
        r"position\s+(\d+)\s+set\s+(tp|stop)\s+\$?([\d.]+)",
        text,
        re.IGNORECASE,
    )
    if position_set_match:
        position_id = int(position_set_match.group(1))
        setting = position_set_match.group(2).lower()
        amount = float(position_set_match.group(3))
        if amount <= 0:
            say("Trigger amount must be positive.")
            return

        target_pos = next((pos for pos in user_data.positions if pos.position_id == position_id), None)
        if target_pos is None:
            say(f"No open position found with ID `{position_id}`.")
            return

        now = int(time.time())
        if setting == "tp":
            target_pos.take_profit = amount
            target_pos.tp_timestamp = now
            save_data()
            say(
                f"Set take profit for position `{position_id}` "
                f"(**{target_pos.side.value} {target_pos.crypto}/USDT**) to **${amount:,.2f}**."
            )
            return

        target_pos.stop_loss = amount
        target_pos.stop_timestamp = now
        save_data()
        say(
            f"Set stop loss for position `{position_id}` "
            f"(**{target_pos.side.value} {target_pos.crypto}/USDT**) to **${amount:,.2f}**."
        )
        return

    # close command: support closing by ID or by symbol
    if text.lower().startswith("close "):
        close_target = text[6:].strip()

        # close ALL
        if close_target.lower() == "all":
            if not user_data.positions:
                say("No open positions.")
                return

            msgs: list[str] = []
            try:
                prices: dict[str, float] = get_prices([pos.crypto for pos in user_data.positions])
                for pos in user_data.positions:
                    cur = prices[pos.crypto]
                    pnl = _close_position(user_data, pos, cur)
                    msgs.append(
                        f"Closed position `{pos.position_id}` (**{pos.side.value} {pos.crypto}/USDT**). "
                        f"Realized PNL: **${pnl:+.2f}** -> New USD balance: **${user_data.usd:.2f}**"
                    )
            except ValueError as e:
                say(str(e))
                return

            save_data()
            for msg in msgs:
                say(f'{msg}\n')

            return


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

            pnl = _close_position(user_data, target_pos, cur)
            save_data()
            say(
                f"Closed position `{target_id}` (**{target_pos.side.value} {target_pos.crypto}/USDT**). "
                f"Realized PNL: **${pnl:+.2f}** -> New USD balance: **${user_data.usd:.2f}**"
            )
            return

        # close CRYPTO/USDT
        pair_part = close_target.upper()
        if not re.match(r"^[A-Z]{2,10}/USDT$", pair_part):
            say(
                "Usage: close CRYPTO/USDT (e.g. close SOL/USDT) or close POSITION_ID (e.g. close 1) or close ALL"
            )
            return
        crypto = pair_part.split("/")[0]

        closed_pnl = 0.0
        closed_margin = 0.0
        survivors: list[Position] = []

        try:
            cur = get_price(crypto)
        except ValueError as e:
            say(str(e))
            return

        for pos in user_data.positions:
            if pos.crypto != crypto:
                survivors.append(pos)
                continue

            pnl = _calculate_pnl(pos, cur)
            closed_pnl += pnl
            closed_margin += pos.margin

        if closed_margin <= 0:
            say(f"No open positions found for **{crypto}/USDT**.")
            return

        user_data.positions = survivors
        user_data.usd += closed_margin + closed_pnl
        save_data()
        say(
            f"Closed all **{crypto}/USDT** positions. "
            f"Realized PNL: **${closed_pnl:+.2f}** -> New USD balance: **${user_data.usd:.2f}**"
        )
        return

    # price command: support any crypto
    if text.lower().startswith("price "):
        crypto_raw = text[6:].strip().upper()
        crypto = crypto_raw[:-5] if crypto_raw.endswith("/USDT") else crypto_raw
        try:
            cur = get_price(crypto)
            say(f"Current **{crypto}/USD** spot price: **${cur:,.2f}**")
        except ValueError as e:
            say(str(e))
        return

    if text.lower() == "check balance":
        msg = f"**USD balance**: ${user_data.usd:.2f}\n\n"
        if user_data.positions:
            msg += "**Open Positions**:\n"
            for pos in user_data.positions:
                try:
                    cur = get_price(pos.crypto)
                except ValueError:
                    msg += f"{str(pos)} | Current: N/A (price fetch failed) | PNL: **$+0.00**\n"
                    continue
                pnl = _calculate_pnl(pos, cur)
                msg += f"{str(pos)} | Current: ${cur:,.2f} | PNL: **${pnl:+.2f}**\n"
        else:
            msg += "No open positions."

        if user_data.orders:
            msg += "\n\n**Pending Limit Orders**:\n"
            for order in user_data.orders:
                liq_price = order.liquidation_price()
                liq_str = f"${liq_price:,.2f}" if liq_price is not None else "N/A"
                msg += (
                    f"- **{order.side.value} {order.crypto}/USDT** (ID: `{order.position_id}`) "
                    f"@{order.lev}x | Margin: ${order.margin:.2f} | Limit: ${order.entry:,.2f} | "
                    f"Liquidation: {liq_str}\n"
                )
        say(msg)
        return

    if text.lower() == "help":
        say(USAGE)
        return

    if text.lower() == "check positions":
        msg = "User Positions:\n"
        for u, udata in get_users().items():
            user_info = client.users_info(user=u)
            username = user_info["user"]["name"]
            msg += f"**{username}**:\n"
            if udata.positions:
                for pos in udata.positions:
                    try:
                        cur = get_price(pos.crypto)
                    except ValueError:
                        msg += "Error fetching price for positions.\n"
                        break
                    pnl = _calculate_pnl(pos, cur)
                    msg += f"{str(pos)} | Current: ${cur:,.2f} | PNL: **${pnl:+.2f}**\n"
            else:
                msg += "No open positions.\n"
            msg += "\n"
        say(msg)
        return

    if text.lower() == "check standings":
        standings: list[tuple[str, float]] = []
        for u, udata in get_users().items():
            total = udata.usd
            for order in udata.orders:
                total += order.margin
            for pos in udata.positions:
                try:
                    cur = get_price(pos.crypto)
                except ValueError:
                    say("Error fetching price for standings.")
                    return
                total += pos.margin + _calculate_pnl(pos, cur)
            user_info = client.users_info(user=u)
            standings.append((user_info["user"]["name"], total))

        standings.sort(key=lambda x: x[1], reverse=True)
        msg = "Standings:\n"
        for name, val in standings:
            msg += f"{name} = ${val:.2f}\n"
        say(msg)
        return

    if text.lower().startswith("brag "):
        crypto_raw = text[5:].strip().upper()
        crypto = crypto_raw[:-5] if crypto_raw.endswith("/USDT") else crypto_raw

        found_pos = False
        brag_msg = ""
        for pos in user_data.positions:
            if pos.crypto != crypto:
                continue
            found_pos = True
            try:
                cur = get_price(crypto)
            except ValueError as e:
                say(str(e))
                return
            pnl = _calculate_pnl(pos, cur)
            brag_msg += f"My {pos.side.value.lower()} position in {crypto}/USDT is on fire!\n"
            brag_msg += f"Entry price: ${pos.entry:,.2f}\n"
            brag_msg += f"Current price: ${cur:,.2f}\n"
            brag_msg += f"PNL: ${pnl:+.2f}\n"
            brag_msg += "To the moon!\n"

        if not found_pos:
            say(f"You don't have an open position in {crypto}/USDT.")
            return

        say(brag_msg)
        return

    if text.startswith("admin set "):
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
        return

    if text.startswith("admin add "):
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
        return

    say("Unknown command. Try '@cryptobot help'.")


if __name__ == "__main__":
    threading.Thread(target=_background_position_updater, daemon=True).start()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
