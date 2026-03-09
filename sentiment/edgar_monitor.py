"""
SEC EDGAR 8-K filing monitor.

Polls for high-impact 8-K filings on watchlist tickers during market hours.
No API key required — EDGAR is publicly accessible.
Uses only stdlib (urllib, html.parser, re) — no new dependencies.

High-impact items tracked:
  1.05  Cybersecurity incident           → bearish
  2.01  Completion of acquisition        → bullish
  2.02  Results of operations (earnings) → bullish
  3.01  Delisting risk                   → bearish
  5.01  Change in board control          → bullish
  5.02  Director/officer departure       → bullish (negotiated exit signal)
"""
import json
import logging
import re
from datetime import date
from html.parser import HTMLParser
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TICKERS_URL   = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS   = "https://data.sec.gov/submissions/CIK{cik}.json"

# SEC requires a User-Agent with contact info — bots without it get blocked
_HEADERS = {
    "User-Agent":      "TraderBot/1.0 automated-trading@traderbot.example.com",
    "Accept-Encoding": "identity",
}

# Combo scores — check subsets so any additional items still match
_COMBO_SCORES: list[tuple[frozenset, float, str, str]] = [
    (frozenset(["2.01", "5.01", "5.02"]), 1.0, "completed_acquisition",   "bullish"),
    (frozenset(["2.01", "5.02"]),          0.9, "acquisition_leadership",  "bullish"),
]

# Single-item scores
_ITEM_SCORES: dict[str, tuple[float, str, str]] = {
    "2.01": (0.8, "acquisition",      "bullish"),
    "5.02": (0.7, "executive_change", "bullish"),
    "1.05": (0.8, "cybersecurity",    "bearish"),
    "3.01": (0.9, "delisting_risk",   "bearish"),
    "2.02": (0.5, "earnings",         "bullish"),
    "5.01": (0.6, "change_in_control","bullish"),
}

# Compiled regex for each item (case-insensitive; matches "Item 2.01" or "ITEM 2.01")
_ITEM_PATTERNS: dict[str, re.Pattern] = {
    item: re.compile(r"\bitem\s+" + re.escape(item), re.IGNORECASE)
    for item in _ITEM_SCORES
}

# Module-level cache — reused across Lambda warm invocations
_CIK_CACHE: dict[str, str] = {}   # "AAPL" → "0000320193"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 12) -> str:
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _fetch_json(url: str, timeout: int = 12):
    return json.loads(_fetch(url, timeout))


# ---------------------------------------------------------------------------
# HTML stripper (stdlib only — no beautifulsoup needed)
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def result(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    parser = _HTMLStripper()
    try:
        parser.feed(html)
        return parser.result()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


# ---------------------------------------------------------------------------
# Ticker → CIK mapping
# ---------------------------------------------------------------------------

def _load_cik_map(tickers: list[str]) -> dict[str, str]:
    """
    Return {TICKER: zero-padded-CIK} for the requested tickers.
    Downloads company_tickers.json once and caches in module scope.
    """
    global _CIK_CACHE
    needed = {t.upper() for t in tickers} - set(_CIK_CACHE)
    if needed:
        try:
            raw = _fetch_json(_TICKERS_URL)
            # raw = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
            for entry in raw.values():
                t = str(entry.get("ticker", "")).upper()
                if t in needed:
                    _CIK_CACHE[t] = str(int(entry["cik_str"])).zfill(10)
        except Exception as exc:
            logger.warning("EDGAR: company_tickers.json load failed: %s", exc)

    return {t.upper(): _CIK_CACHE[t.upper()] for t in tickers if t.upper() in _CIK_CACHE}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_todays_filings(watchlist: list[str]) -> list[dict]:
    """
    Return 8-K filings filed today for any ticker in watchlist.

    Each result:
      {ticker, company_name, filed_at, accession_number, filing_url}
    """
    today   = date.today().isoformat()
    cik_map = _load_cik_map(watchlist)
    results: list[dict] = []

    for ticker, cik in cik_map.items():
        try:
            data     = _fetch_json(_SUBMISSIONS.format(cik=cik))
            recent   = data.get("filings", {}).get("recent", {})
            forms    = recent.get("form", [])
            dates    = recent.get("filingDate", [])
            accnos   = recent.get("accessionNumber", [])
            prim_doc = recent.get("primaryDocument", [])

            for form, filed, accno, doc in zip(forms, dates, accnos, prim_doc):
                if form != "8-K" or filed != today:
                    continue
                cik_short  = str(int(cik))
                acc_nodash = accno.replace("-", "")
                filing_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_short}/{acc_nodash}/{doc}"
                )
                results.append({
                    "ticker":           ticker,
                    "company_name":     data.get("name", ticker),
                    "filed_at":         filed,
                    "accession_number": accno,
                    "filing_url":       filing_url,
                })
                logger.info(
                    "EDGAR 8-K: %s (%s) filed %s — %s",
                    ticker, data.get("name", "?"), filed, accno,
                )
        except Exception as exc:
            logger.debug("EDGAR submissions fetch failed for %s: %s", ticker, exc)

    return results


def get_filing_text(filing_url: str) -> str:
    """Fetch, clean, and truncate 8-K document text to 2000 characters."""
    try:
        raw  = _fetch(filing_url)
        text = _strip_html(raw)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:2000]
    except Exception as exc:
        logger.warning("EDGAR: filing text fetch failed (%s): %s", filing_url, exc)
        return ""


def parse_filing_items(filing_url: str) -> list[str]:
    """
    Fetch the 8-K document and return which Item numbers are present.
    Checks for: 1.05, 2.01, 2.02, 3.01, 5.01, 5.02
    """
    text = get_filing_text(filing_url)
    if not text:
        return []
    return sorted(item for item, pat in _ITEM_PATTERNS.items() if pat.search(text))


def score_filing(items: list[str]) -> dict:
    """
    Score a filing based on which Item numbers are present.

    Returns:
      {"score": float, "catalyst": str, "direction": str}
    """
    item_set = frozenset(items)

    # Check combos first (highest priority — use subset match so extra items still trigger)
    for combo, score, catalyst, direction in _COMBO_SCORES:
        if combo <= item_set:
            return {"score": score, "catalyst": catalyst, "direction": direction}

    # Single-item: take the highest-scoring item present
    best = (0.0, "unknown", "bullish")
    for item in items:
        if item in _ITEM_SCORES:
            sc, cat, direction = _ITEM_SCORES[item]
            if sc > best[0]:
                best = (sc, cat, direction)

    return {"score": best[0], "catalyst": best[1], "direction": best[2]}


def build_signal(filing: dict, items: list[str], score_dict: dict) -> dict:
    """
    Build a unified signal dict from a parsed filing.

    Returns:
      {ticker, company_name, filed_at, accession_number, filing_url,
       items, score, catalyst, direction, confidence, priority, filing_text}
    """
    score = score_dict.get("score", 0.0)
    return {
        "ticker":           filing["ticker"],
        "company_name":     filing["company_name"],
        "filed_at":         filing["filed_at"],
        "accession_number": filing["accession_number"],
        "filing_url":       filing["filing_url"],
        "items":            items,
        "score":            score,
        "catalyst":         score_dict.get("catalyst", "unknown"),
        "direction":        score_dict.get("direction", "bullish"),
        "confidence":       "high" if score >= 0.8 else ("medium" if score >= 0.5 else "low"),
        "priority":         score >= 0.8,
        "filing_text":      filing.get("_text", ""),  # populated by scan_watchlist
    }


def scan_watchlist(watchlist: list[str]) -> dict[str, dict]:
    """
    Full EDGAR scan — fetch today's 8-Ks for all watchlist tickers, parse
    items, score, and return {ticker: signal_dict} for any with score > 0.

    Main integration point for scheduler/jobs.py and the standalone edgar_scan window.
    """
    filings  = get_todays_filings(watchlist)
    signals: dict[str, dict] = {}

    for filing in filings:
        ticker = filing["ticker"]
        try:
            # Fetch text once; reuse for both item parsing and the signal
            text  = get_filing_text(filing["filing_url"])
            items = [item for item, pat in _ITEM_PATTERNS.items() if pat.search(text)] if text else []
            items.sort()

            if not items:
                logger.info("EDGAR: %s 8-K — no high-impact items detected", ticker)
                continue

            score_dict = score_filing(items)
            if score_dict["score"] == 0.0:
                continue

            filing["_text"] = text   # carry text through build_signal
            signal = build_signal(filing, items, score_dict)
            signal["filing_text"] = text[:2000]   # overwrite placeholder
            signals[ticker] = signal

            logger.info(
                "EDGAR signal: %s  catalyst=%s  score=%.1f  items=%s  priority=%s",
                ticker, score_dict["catalyst"], score_dict["score"], items, signal["priority"],
            )
        except Exception as exc:
            logger.warning("EDGAR: failed to process filing for %s: %s", ticker, exc)

    return signals
