#!/usr/bin/env python3
"""
tape_update.py -- the 24/7 "tape" refresh for feed.json, run by
.github/workflows/tape.yml on GitHub's own infrastructure, NOT the
private trading machine. Added 9/18, real feedback: once the private
site-loop.sh stops rebuilding the feed after hours (see its own 9/18
QUIET_HOURS branch), the board/board fields would otherwise freeze
completely -- but the site's own copy already promises "crypto stays
live 24/7." This script is what actually keeps that promise once the
desk itself is closed.

Touches ONLY: session (market_state/next_open/next_open_iso/position),
market_board's crypto rows (BTC/ETH/SOL -- last/chg_pct/history), news,
generated_at. Everything else (tickets/stats/desk_board/roast/weeks/
skip_summary*/scan_log*/streak_last8/last_fill_at/tenor_calendar/
desk_events/board_movers/calendar/news_public) is carried through
byte-for-byte -- this script runs against a checkout of the PUBLIC repo
only, has no access to trade-log.md or watch-state.json, and must never
guess at real trading state.

Refuses to touch feed.json at all if it computes market_state == "OPEN"
-- during real trading hours the private machine's site-build.py is the
sole authority on the feed and this script must never race it. The
workflow's cron fires every 15 min around the clock; this in-script
guard is what actually enforces the RTH boundary, not the cron schedule
(GitHub Actions cron has no timezone support, so encoding "5pm CT" into
the YAML itself would need hand-shifted, DST-fragile UTC entries).

Usage: python3 scripts/tape_update.py [--dry-run]
  --dry-run: print what would be written, touch nothing on disk.
"""
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = ROOT / "feed.json"
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

CRYPTO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
RSS_URL = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
HISTORY_CAP = 40


def now_et():
    return datetime.now(tz=ET)


def build_session(now):
    """Ported by hand from the private repo's site-build.py build_session()
    (9/14) -- this script runs from a separate checkout with no access to
    that file, so keep the two in sync manually if that logic ever changes.
    One deliberate difference: position is hardcoded FLAT, never read from
    trade-log.md (this script has no access to it) -- safe only because
    main() below refuses to run at all once market_state would be OPEN,
    and this system never holds a position overnight.
    """
    weekday = now.weekday()  # 0=Mon .. 6=Sun
    day_label = now.strftime("%a").upper()
    hhmm = now.hour * 100 + now.minute

    def next_open_at(days_ahead):
        target_date = (now + timedelta(days=days_ahead)).date()
        return datetime.combine(target_date, datetime.min.time(), tzinfo=CT).replace(hour=8, minute=30)

    if weekday >= 5:
        market_state = "WEEKEND"
        days_to_mon = (7 - weekday) % 7
        next_open_dt = next_open_at(days_to_mon if days_to_mon else 1)
        next_open = f"{next_open_dt.strftime('%a').upper()} 8:30 CT"
    elif hhmm < 830:
        market_state = "CLOSED"
        next_open_dt = next_open_at(0)
        next_open = "TODAY 8:30 CT"
    elif 830 <= hhmm < 1500:
        market_state = "OPEN"
        days_ahead = 3 if weekday == 4 else 1
        next_open_dt = next_open_at(days_ahead)
        next_open = "TOMORROW 8:30 CT" if weekday < 4 else "MON 8:30 CT"
    else:
        market_state = "CLOSED"
        days_ahead = 3 if weekday == 4 else 1
        next_open_dt = next_open_at(days_ahead)
        next_open_label = "TOMORROW" if weekday < 4 else "MON"
        next_open = f"{next_open_label} 8:30 CT"

    return {
        "day_label": day_label,
        "market_state": market_state,
        "next_open": next_open,
        "next_open_iso": next_open_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "position": "FLAT",
    }


def fetch_crypto():
    """Real CoinGecko simple-price pull, BTC/ETH/SOL only -- the only
    board names genuinely still moving with the desk closed. Returns {}
    on any failure (never raises) so a network hiccup just skips this
    refresh instead of failing the whole run.
    """
    ids = ",".join(CRYPTO_IDS.values())
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[tape_update] crypto fetch failed: {e}")
        return {}
    out = {}
    for sym, cg_id in CRYPTO_IDS.items():
        row = data.get(cg_id)
        if not row or "usd" not in row:
            continue
        out[sym] = {"last": row["usd"], "chg_pct": row.get("usd_24h_change")}
    return out


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RSS_DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def fetch_news(limit=6):
    """Real MarketWatch top-stories RSS, headline + description verbatim
    -- no rewriting, no summarizing, no model call. html.unescape handles
    the feed's own entities (e.g. "&#x2019;" -> an apostrophe). Returns
    [] on any failure -- the client already renders an empty news list
    fine ("-- / --" in the carousel).
    """
    try:
        r = requests.get(RSS_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.text
    except Exception as e:
        print(f"[tape_update] news fetch failed: {e}")
        return []
    out = []
    for block in _RSS_ITEM_RE.findall(text)[:limit]:
        tm = _RSS_TITLE_RE.search(block)
        if not tm:
            continue
        dm = _RSS_DESC_RE.search(block)
        title = html.unescape(_TAG_STRIP_RE.sub("", tm.group(1))).strip()
        summary = html.unescape(_TAG_STRIP_RE.sub("", dm.group(1))).strip() if dm else ""
        if title:
            out.append({"tag": "Markets", "title": title, "summary": summary})
    return out


def refresh_crypto_board(market_board, crypto):
    """Appends one real point per symbol rather than overwriting history
    outright, so the sparkline shows real quiet-hours motion instead of
    a flat frozen line -- that's the actual point of "crypto stays live
    24/7." Equity/index rows are untouched: their price genuinely does
    not move once the exchange is closed, so leaving them exactly as the
    private loop last wrote them is correct, not stale.
    """
    for sym, vals in crypto.items():
        row = market_board.get(sym) or {"last": vals["last"], "chg_pct": vals.get("chg_pct") or 0, "history": []}
        row["last"] = vals["last"]
        if vals.get("chg_pct") is not None:
            row["chg_pct"] = round(vals["chg_pct"], 2)
        hist = row.get("history") or []
        hist.append(vals["last"])
        row["history"] = hist[-HISTORY_CAP:]
        market_board[sym] = row
    return market_board


def main():
    dry_run = "--dry-run" in sys.argv

    if not FEED_PATH.exists():
        print("ERROR: feed.json not found -- refusing to create one from scratch.")
        sys.exit(1)

    feed = json.loads(FEED_PATH.read_text())
    now = now_et()
    session = build_session(now)

    if session["market_state"] == "OPEN":
        print("[tape_update] market_state=OPEN -- the private loop owns this window, not touching feed.json.")
        return

    crypto = fetch_crypto()
    news = fetch_news()

    feed["session"] = session
    feed["generated_at"] = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if crypto:
        feed["market_board"] = refresh_crypto_board(feed.get("market_board") or {}, crypto)
    if news:
        feed["news"] = news

    if dry_run:
        print(json.dumps({"session": feed["session"], "crypto": crypto, "news_count": len(news)}, indent=2))
        return

    FEED_PATH.write_text(json.dumps(feed, indent=2))
    print(f"[tape_update] wrote feed.json -- market_state={session['market_state']}, "
          f"crypto={list(crypto.keys())}, news={len(news)} headlines")


if __name__ == "__main__":
    main()
