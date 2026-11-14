#!/usr/bin/env python3
"""Pull 42.space per-trader volume leaderboards and write data/leaderboard.json.

No secrets required. Talks only to the public Market API used by https://www.42.space.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://rest.ft.42.space"
LEADERBOARD_PATH = "/api/v1/market-data/leaderboard"
PERIODS = ("1d", "7d", "30d", "all")
# Official UI pages 20 at a time; 200 per window keeps the snapshot useful
# without hammering the gateway (limit=200 is accepted).
PAGE_SIZE = 200
TOP_N = 200
REQUEST_PAUSE_S = 0.35
TIMEOUT_S = 30
USER_AGENT = "42-volume-leaderboard/1.0 (+https://github.com/42-volume-leaderboard)"

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "leaderboard.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network error for {url}: {exc.reason}") from exc


def normalize_row(row: dict) -> dict:
    volume = float(row.get("totalVolume") or 0)
    pnl = float(row.get("pnl") or 0)
    pnl_pct = float(row.get("pnlPct") or 0)
    win_rate = float(row.get("winRate") or 0)
    return {
        "rank": int(row["rank"]),
        "trader": str(row["walletAddress"]),
        "volume": volume,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "win_rate": win_rate,
        "trade_count": int(row.get("tradeCount") or 0),
        "profitable_sells": int(row.get("profitableSells") or 0),
        "losing_sells": int(row.get("losingSells") or 0),
        "winning_predictions": int(row.get("winningPredictions") or 0),
        "losing_predictions": int(row.get("losingPredictions") or 0),
    }


def fetch_period(period: str) -> dict:
    rows: list[dict] = []
    total = None
    offset = 0
    while len(rows) < TOP_N:
        limit = min(PAGE_SIZE, TOP_N - len(rows))
        url = (
            f"{API_BASE}{LEADERBOARD_PATH}"
            f"?period={period}&sort_by=volume&limit={limit}&offset={offset}"
        )
        payload = fetch_json(url)
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            raise SystemExit(f"Unexpected payload for period={period}: missing data[]")
        if not chunk:
            break
        rows.extend(normalize_row(item) for item in chunk)
        pagination = payload.get("pagination") or {}
        total = pagination.get("totalResults", total)
        if not pagination.get("hasMore"):
            break
        offset += len(chunk)
        time.sleep(REQUEST_PAUSE_S)

    # Keep official API rank, but re-assert volume descending in case of ties/pages.
    rows.sort(key=lambda r: (-r["volume"], r["rank"], r["trader"].lower()))
    for i, row in enumerate(rows, start=1):
        # Preserve API rank when present; only fill if missing.
        if not row.get("rank"):
            row["rank"] = i

    if not rows:
        raise SystemExit(f"No trader rows returned for period={period}")

    return {
        "period": period,
        "total_traders": int(total) if total is not None else len(rows),
        "returned": len(rows),
        "rows": rows,
    }


def main() -> int:
    started = iso_now()
    periods: dict[str, dict] = {}
    for i, period in enumerate(PERIODS):
        if i:
            time.sleep(REQUEST_PAUSE_S)
        print(f"fetching period={period} ...", flush=True)
        periods[period] = fetch_period(period)
        print(
            f"  traders={periods[period]['total_traders']} "
            f"rows={periods[period]['returned']} "
            f"top={periods[period]['rows'][0]['trader']} "
            f"vol={periods[period]['rows'][0]['volume']:.2f}",
            flush=True,
        )

    doc = {
        "source": f"{API_BASE}{LEADERBOARD_PATH}",
        "source_page": "https://www.42.space/points",
        "sort_by": "volume",
        "unit": "USDT",
        "note": (
            "Per-trader trading volume from the public 42.space Market API. "
            "period must be one of: 1d, 7d, 30d, all. Default official sort is PnL; "
            "this snapshot always requests sort_by=volume."
        ),
        "updated_at": iso_now(),
        "fetched_at": started,
        "periods": periods,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT_PATH)
    print(f"wrote {OUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
