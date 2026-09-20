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
import itertools
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = ROOT / "feed.json"
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

CRYPTO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
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


def fetch_btc_range():
    """Real 24h high/low for BTC, added 9/18 -- one line tying MSTR/
    COIN/IREN's real moves to crypto's own range without a second board.
    CoinGecko's /coins/markets endpoint (not the simple/price one used
    above) is what actually carries high_24h/low_24h. Returns None on
    any failure.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": "bitcoin", "price_change_percentage": "24h"},
            timeout=10,
        )
        r.raise_for_status()
        row = r.json()[0]
        return {
            "last": row["current_price"],
            "high_24h": row["high_24h"],
            "low_24h": row["low_24h"],
            "chg_pct": round(row["price_change_percentage_24h"], 2),
        }
    except Exception as e:
        print(f"[tape_update] BTC range fetch failed: {e}")
        return None


# Macro strip, added 9/18: real "last print" snapshot, not live intraday
# -- explicitly matches "freeze after 5pm like stocks." Deliberately NOT
# 2s10s: tested every plausible free 2-year-yield ticker (^UST2Y doesn't
# resolve, ^TYX is the 30Y, ^FVX is the 5Y, ^IRX is the 13-week bill)
# and found no reliable free 2Y source -- shipping a wrong spread would
# be worse than omitting it.
MACRO_TICKERS = {
    "10Y": "^TNX",
    "DXY": "DX-Y.NYB",
    "WTI": "CL=F",
    "VIX": "^VIX",
}


def fetch_macro():
    """regularMarketPrice only (not postMarket*/preMarket* -- tested
    earlier and found unreliable/null on a bare unauthenticated call).
    Per-ticker try/except so one bad symbol doesn't blank the whole
    strip; returns whatever subset succeeded, {} if all four fail.
    """
    out = {}
    for label, ticker in MACRO_TICKERS.items():
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "5d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            if price is not None:
                out[label] = round(price, 2)
        except Exception as e:
            print(f"[tape_update] macro fetch failed for {label} ({ticker}): {e}")
    return out


def merge_macro(existing_macro, fresh_macro, now_iso):
    """Added 9/20, real instruction: don't let the macro strip sit next
    to live BTC pretending to be the same freshness. regularMarketPrice
    for 10Y/DXY/WTI/VIX genuinely does not change while those markets
    are closed (weekends, overnight) -- when a fresh fetch comes back
    byte-identical to what's already stored, this keeps the OLD `asof`
    stamp instead of rewriting it to "now," so the client can honestly
    show "as of Fri close" instead of implying a live tick that never
    happened. Only advances `asof` when a value genuinely moved.
    """
    if not fresh_macro:
        return existing_macro
    existing_values = {k: v for k, v in (existing_macro or {}).items() if k != "asof"}
    result = dict(fresh_macro)
    if existing_values == fresh_macro:
        result["asof"] = (existing_macro or {}).get("asof") or now_iso
    else:
        result["asof"] = now_iso
    return result


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RSS_DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)
_RSS_LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
_RSS_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_RSS_CATEGORY_RE = re.compile(r"<category>(.*?)</category>", re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)

# Rebuilt 9/20, real finding: mw_realtimeheadlines (added 9/18) turned
# out to be a frozen document, not a live feed -- pulled it fresh on
# 9/20 and got byte-identical items to 9/18's first test, several
# stamped 2024/2025 despite this being 2026. Every fetch all weekend
# returned the same 3 filtered items because the SOURCE never changed,
# not because of the scheduling gap (see CHANGELOG). Replaced outright
# rather than patched. Four sources, each tested live before shipping,
# each pre-scoped in its own way so the old broad keyword filter is no
# longer needed for any of them:
#   - Fed: unchanged, already honest, its own "Monetary Policy" category
#     is the real filter (see build_mechanical_news's own reasoning).
#   - Yahoo per-ticker headlines: the `s=` param scopes results server-
#     side to this desk's own board universe (site-build.py's
#     BOARD_UNIVERSE, kept in sync by hand -- this script has no import
#     access to that file) -- confirmed live, items are minutes old,
#     real, and genuinely stock-diverse at 20 names instead of the
#     original 10 (e.g. "The Stock Market's Best Quarter... S&P 500",
#     "Should You Buy SpaceX Stock Before Its Next Earnings").
#   - Cboe Insights: added 9/20, real ask ("what about options news, not
#     just crypto") -- this is the options exchange's own editorial
#     blog (hedging demand, vol commentary, "Week of 9/14: Macro
#     Uncertainty Fuels Hedging Demand Ahead of FOMC"), not a paid
#     unusual-options-flow signal service (that stays on the Skip list,
#     per instruction). Confirmed live: real dated items through
#     Thursday, correctly quiet since -- a weekday-only blog, not stuck.
#   - CoinDesk: 100% crypto by publication scope (real live pull:
#     Gemini, Coinbase, Bitcoin -- all today's date). Needs -L-equivalent
#     handling: the bare URL 308-redirects, requests follows it by
#     default so no special handling needed here.
# CoinTelegraph and Yahoo's general markets index were also tested and
# work, held back as too broad/redundant -- the general index in
# particular is mostly micro-cap noise (AppFolio, Jersey Mike's, Willis
# Lease Finance) with nothing to do with this desk's own universe, the
# same class of problem that got MarketWatch's old topstories feed
# killed in the first place. Reuters' public RSS is still dead (years
# now) and CNBC still 403s a scripted request -- neither retested, no
# reason either would have changed.
NEWS_FEEDS = [
    ("FED", "https://www.federalreserve.gov/feeds/press_all.xml", "Monetary Policy"),
    ("MARKETS", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,DIA,IWM,VIXY,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,MSTR,COIN,HOOD,IBIT,IREN,XLK,XLF,XLE,XLV,GDX", None),
    ("OPTIONS", "https://www.cboe.com/insights/rss/", None),
    ("CRYPTO", "https://www.coindesk.com/arc/outboundfeeds/rss/", None),
]

# Age gate, added 9/20 -- the actual fix for "looks stuck," more than
# any specific source swap: even a genuinely live feed can hand back an
# old cached/evergreen item, and this is what would have caught
# mw_realtimeheadlines's 2024/2025 ghosts outright regardless of which
# source they came from. 5 days -- long enough that a quiet weekend
# (Fed/SEC both go dark Sat-Sun) doesn't get emptied out, short enough
# that nothing from last month ever shows up as "current."
NEWS_MAX_AGE = timedelta(days=5)


def _clean_field(raw):
    """Strips a CDATA wrapper (the Fed feed wraps every field in one,
    MarketWatch doesn't), then HTML tags and entities. Same cleanup
    either feed needs, so both go through one function.
    """
    if not raw:
        return ""
    text = raw.strip()
    m = _CDATA_RE.match(text)
    if m:
        text = m.group(1)
    return html.unescape(_TAG_STRIP_RE.sub("", text)).strip()


def _parse_pubdate(raw):
    """RFC-822-style pubDate ("Fri, 18 Sep 2026 21:16:00 GMT") -> ISO
    UTC, via email.utils (stdlib, handles the real format's edge cases
    rather than a hand-rolled regex). Returns "" on anything unparsable
    so a bad date never crashes the run -- the item just sorts last.
    """
    raw = _clean_field(raw)
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _fetch_one_feed(tag, feed_url, category_filter):
    try:
        r = requests.get(feed_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        # Real bug caught in testing: the Fed's feed doesn't declare a
        # charset in its Content-Type header, so requests falls back to
        # ISO-8859-1 (the old HTTP default) while the actual content is
        # UTF-8 -- corrupted every em/en-dash into "â\x80\x93"-style
        # garbage (e.g. "July 28â29" instead of "July 28-29"). Trust
        # chardet's own content-based detection over the (here, absent)
        # header instead.
        r.encoding = r.apparent_encoding or "utf-8"
        text = r.text
    except Exception as e:
        print(f"[tape_update] news fetch failed for {feed_url}: {e}")
        return []
    now_utc = datetime.now(timezone.utc)
    items = []
    for block in _RSS_ITEM_RE.findall(text):
        tm = _RSS_TITLE_RE.search(block)
        lm = _RSS_LINK_RE.search(block)
        if not tm or not lm:
            continue
        if category_filter is not None:
            cm = _RSS_CATEGORY_RE.search(block)
            if not cm or _clean_field(cm.group(1)) != category_filter:
                continue
        url = _clean_field(lm.group(1))
        title = _clean_field(tm.group(1))
        if not url or not title:
            continue
        # Age gate, added 9/20 -- the real fix for "looks stuck": a
        # feed that's genuinely alive can still hand back one evergreen
        # or mis-cached item, and this is what would have caught
        # mw_realtimeheadlines's 2024/2025 ghosts regardless of which
        # source they came from. No parseable date at all is treated as
        # unverifiable, not assumed fresh -- dropped, same as too old.
        pm = _RSS_PUBDATE_RE.search(block)
        ts = _parse_pubdate(pm.group(1)) if pm else ""
        if not ts:
            continue
        published_dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now_utc - published_dt > NEWS_MAX_AGE:
            continue
        dm = _RSS_DESC_RE.search(block)
        summary = _clean_field(dm.group(1))[:240] if dm else ""
        items.append({"tag": tag, "title": title[:140], "summary": summary, "url": url, "ts": ts})
    return items


def fetch_news(limit=10):
    """Real headlines from NEWS_FEEDS, verbatim -- no rewriting, no
    summarizing, no model call. Every item requires a real `url` (the
    feed's own <link>) -- an item with no link is dropped outright
    rather than shipped as a dead-end card.

    Interleaved round-robin across feeds (first item from each feed,
    then second from each, ...) rather than a global sort by `ts` --
    real bug found while testing: mw_realtimeheadlines's own <pubDate>
    is stale/wrong (dated 2024-2025 on genuinely current headlines), so
    a strict newest-first sort let that one broken field silently crowd
    the Fed's real, current releases out of the list entirely. `ts` is
    still stored per item for display -- just not trusted as a
    cross-feed ordering key. Returns [] only if every feed fails.
    """
    per_feed = [_fetch_one_feed(tag, url, cat) for tag, url, cat in NEWS_FEEDS]
    seen_urls = set()
    out = []
    for item in itertools.chain.from_iterable(itertools.zip_longest(*per_feed)):
        if item is None or item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        out.append(item)
        if len(out) >= limit:
            break
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


# Lowered from 10 to 8 on 9/20, real instruction ("1/N with N=0-8") --
# 5 curated cards already anchor the list on a real trading day, and 8
# total leaves meaningful room for fresh RSS without padding toward a
# round number for its own sake.
NEWS_LIMIT = 8


def merge_news(existing_news, fresh_rss):
    """Added 9/18, real user ask ("can it also use the ones I pasted on
    scout report?"): site-build.py's own build_mechanical_news() already
    parses the user's real pasted morning-scout brief into news cards,
    zero Claude, every cycle -- and neither that nor its Claude-authored
    fallback (site-copy.py) ever carries a real per-item `url` (both are
    synthesized from a pasted brief, not sourced from one article each).
    That's a reliable, structural signal: any existing item with no
    `url` is today's real curated content and is kept untouched, in
    place, on every run. Fresh RSS (always has a real url) fills the
    remaining slots up to NEWS_LIMIT, deduped against every url already
    present. Curated content this script has no access to (it runs from
    a checkout of the public repo alone) never gets silently discarded
    by an off-hours refresh again.
    """
    # Fixed same-day, real bug caught by running this twice in a row
    # before shipping: an earlier draft deduped fresh_rss against every
    # url already sitting in existing_news -- including RSS items THIS
    # SCRIPT wrote last run. Since a live feed's top items are often
    # unchanged a few minutes later, that treated "still the current
    # headline" as "already seen" and silently zeroed out the RSS
    # portion on the very next run. The RSS slice is a fresh snapshot
    # every run, not something accumulated across runs -- it should
    # never be deduped against its own prior output, only within a
    # single fetch (fetch_news already handles that).
    existing_news = existing_news or []
    curated = [n for n in existing_news if not n.get("url")]
    remaining = max(0, NEWS_LIMIT - len(curated))
    return curated + fresh_rss[:remaining]


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
    btc_range = fetch_btc_range()
    macro = fetch_macro()

    feed["session"] = session
    feed["generated_at"] = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if crypto:
        feed["market_board"] = refresh_crypto_board(feed.get("market_board") or {}, crypto)
    feed["news"] = merge_news(feed.get("news"), news)
    if btc_range:
        feed["btc_range"] = btc_range
    feed["macro"] = merge_macro(feed.get("macro"), macro, feed["generated_at"])

    if dry_run:
        print(json.dumps({
            "session": feed["session"], "crypto": crypto, "news_count": len(news),
            "btc_range": btc_range, "macro": macro,
        }, indent=2))
        return

    FEED_PATH.write_text(json.dumps(feed, indent=2))
    print(f"[tape_update] wrote feed.json -- market_state={session['market_state']}, "
          f"crypto={list(crypto.keys())}, news={len(news)} headlines")


if __name__ == "__main__":
    main()
