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


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RSS_DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)
_RSS_LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
_RSS_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_RSS_CATEGORY_RE = re.compile(r"<category>(.*?)</category>", re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)

# Added 9/18, real feedback: news had no real article link at all, so
# there was no way to actually read more than the 2-line blurb. Two
# real, tested sources. Real content-quality finding along the way:
# MarketWatch's "Top Stories" feed (mw_topstories) is mostly personal-
# finance advice columns ("I have $125K in credit-card debt...") that,
# sorted strict-newest-first against a slow-publishing feed like the
# Fed's, crowd it out of the top 10 entirely on volume alone -- swapped
# for mw_realtimeheadlines instead (confirmed via a real pull: rate
# decisions, PMI prints, FX moves -- the actual "markets" content this
# site wants). Fed press releases (confirmed working, directly relevant
# on a day the headline story IS a Fed decision). Reuters' old public
# RSS endpoint is dead (connection failure) and CNBC's returns a hard
# Akamai "Access Denied" to a scripted request -- both tested and
# dropped rather than shipped on a guess.
# category=None means no filter (MarketWatch's feed has no <category>
# tags at all). The Fed's feed carries a real one per item -- checked
# live, 9/18: roughly half of press_all.xml is "Enforcement Actions"
# (routine actions against individual small banks, e.g. "terminates
# enforcement action with SNB Bancshares") or "Orders on Banking
# Applications" (routine M&A approvals) -- zero market relevance.
# Restricting to the feed's OWN "Monetary Policy" category (FOMC
# statements, meeting minutes, economic projections) uses the source's
# real classification, not an invented keyword filter.
NEWS_FEEDS = [
    ("MARKETS", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", None),
    ("FED", "https://www.federalreserve.gov/feeds/press_all.xml", "Monetary Policy"),
]


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
        dm = _RSS_DESC_RE.search(block)
        summary = _clean_field(dm.group(1))[:240] if dm else ""
        pm = _RSS_PUBDATE_RE.search(block)
        ts = _parse_pubdate(pm.group(1)) if pm else ""
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
