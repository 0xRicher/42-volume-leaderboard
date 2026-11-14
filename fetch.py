#!/usr/bin/env python3
"""Pull 42.space volume leaderboards and write data/leaderboard.json.

No secrets required. Talks only to the public Market API and the public
points leaderboard used by https://www.42.space.

Classification rules live in wallet-labels.json. The same file is loaded by
index.html; keep the two implementations in step (thresholds are read from
that file, not hardcoded here).
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://rest.ft.42.space"
LEADERBOARD_PATH = "/api/v1/market-data/leaderboard"
MARKETS_PATH = "/api/v1/markets"
POINTS_URL = "https://www.42.space/api/points/leaderboard?limit=1"
PERIODS = ("1d", "7d", "30d", "all")
# Observed maximum: limit=1000 is silently capped at 500.
PAGE_SIZE = 500
TOP_N = 200
REQUEST_PAUSE_S = 0.12
TIMEOUT_S = 45
MAX_RETRIES = 5
# Market-list volume within this percent of all-time trader volume counts as
# the same platform total. See README for why the two series can diverge.
MARKET_CONSISTENT_PCT = 1.0
USER_AGENT = "42-volume-leaderboard/1.1 (+https://github.com/42-volume-leaderboard)"

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "leaderboard.json"
LABELS_PATH = ROOT / "wallet-labels.json"

WEEKDAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            text = body.decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"HTTP {exc.code} for {url}: {text}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise SystemExit(str(last_error)) from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Network error for {url}: {exc.reason}")
        time.sleep(min(8.0, 0.6 * (2**attempt)))
    raise SystemExit(str(last_error))


def load_labels() -> dict:
    try:
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Cannot read {LABELS_PATH}: {exc}") from exc


def _address_entry(item) -> tuple[str, list[str] | None]:
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict) and item.get("address"):
        periods = item.get("periods")
        if periods is not None:
            periods = [str(p) for p in periods]
        return str(item["address"]), periods
    raise SystemExit(f"Bad address entry in wallet-labels.json: {item!r}")


def compile_labels(labels: dict) -> dict:
    tiers: dict[str, dict] = {}
    listed: dict[str, list[dict]] = {}
    for tier in labels.get("tiers") or []:
        tier_id = str(tier["id"])
        tiers[tier_id] = tier
        for item in tier.get("addresses") or []:
            address, periods = _address_entry(item)
            listed.setdefault(address.lower(), []).append(
                {"tier": tier_id, "periods": periods}
            )
    identities: dict[str, dict] = {}
    for identity in labels.get("identities") or []:
        meta = {
            "id": identity["id"],
            "label": identity["label"],
            "tooltip": identity.get("tooltip") or "",
        }
        for item in identity.get("addresses") or []:
            address, _periods = _address_entry(item)
            identities[address.lower()] = meta
    heuristics = labels.get("heuristics") or {}
    if "tierB" not in heuristics or "tierC" not in heuristics:
        raise SystemExit("wallet-labels.json is missing heuristics.tierB or heuristics.tierC")
    if not labels.get("pointsWeekStart"):
        raise SystemExit("wallet-labels.json is missing pointsWeekStart")
    return {
        "tiers": tiers,
        "listed": listed,
        "identities": identities,
        "heuristics": heuristics,
        "points_week_start": str(labels["pointsWeekStart"]),
    }


def _allows(periods: list[str] | None, window: str) -> bool:
    return periods is None or window in periods


def heuristic_tier_b(row: dict, rule: dict) -> bool:
    win = float(row.get("win_rate") or 0)
    if abs(win - float(rule.get("winRate", 0))) > 1e-9:
        return False
    volume = float(row.get("volume") or 0)
    trades = int(row.get("trade_count") or 0)
    if volume < float(rule["volumeMin"]) and trades < int(rule["tradeCountMin"]):
        return False
    pnl_pct = float(row.get("pnl_pct") or 0)
    return float(rule["pnlPctMin"]) <= pnl_pct <= float(rule["pnlPctMax"])


def heuristic_tier_c(row: dict, rule: dict) -> bool:
    trades = int(row.get("trade_count") or 0)
    if trades < int(rule["tradeCountMin"]):
        return False
    preds = int(row.get("winning_predictions") or 0) + int(row.get("losing_predictions") or 0)
    if preds > float(rule["predictionRatioMax"]) * trades + 1e-6:
        return False
    pnl_pct = float(row.get("pnl_pct") or 0)
    return float(rule["pnlPctMin"]) <= pnl_pct <= float(rule["pnlPctMax"])


def classify(row: dict, window: str, compiled: dict) -> dict:
    """Return {tier, source, identity}. tier is 'A'|'B'|'C'|None.

    Mirrors index.html. An explicit period allow-list that excludes `window`
    suppresses heuristics for that address (the 0x3cE29… rule).
    """
    addr = str(row.get("trader") or "").lower()
    entries = compiled["listed"].get(addr) or []
    restricted = any(entry["periods"] is not None for entry in entries)
    for tier_id in ("A", "B", "C"):
        for entry in entries:
            if entry["tier"] == tier_id and _allows(entry["periods"], window):
                return {"tier": tier_id, "source": "list", "identity": None}
    if restricted:
        return {
            "tier": None,
            "source": "retail",
            "identity": compiled["identities"].get(addr),
        }
    identity = compiled["identities"].get(addr)
    if identity:
        return {"tier": None, "source": "retail", "identity": identity}
    rules = compiled["heuristics"]
    if heuristic_tier_c(row, rules["tierC"]):
        return {"tier": "C", "source": "heuristic", "identity": None}
    if heuristic_tier_b(row, rules["tierB"]):
        return {"tier": "B", "source": "heuristic", "identity": None}
    return {"tier": None, "source": "retail", "identity": identity}


def normalize_row(row: dict) -> dict:
    return {
        "rank": int(row.get("rank") or 0),
        "trader": str(row["walletAddress"]),
        "volume": float(row.get("totalVolume") or 0),
        "pnl": float(row.get("pnl") or 0),
        "pnl_pct": float(row.get("pnlPct") or 0),
        "win_rate": float(row.get("winRate") or 0),
        "trade_count": int(row.get("tradeCount") or 0),
        "profitable_sells": int(row.get("profitableSells") or 0),
        "losing_sells": int(row.get("losingSells") or 0),
        "winning_predictions": int(row.get("winningPredictions") or 0),
        "losing_predictions": int(row.get("losingPredictions") or 0),
    }


def fetch_leaderboard(query: str) -> tuple[list[dict], int]:
    """Paginate until the API reports no further pages. Returns unique rows."""
    by_addr: dict[str, dict] = {}
    total = 0
    offset = 0
    pages = 0
    while True:
        sep = "&" if "?" in query else "?"
        url = (
            f"{API_BASE}{LEADERBOARD_PATH}{query}{sep}"
            f"sort_by=volume&limit={PAGE_SIZE}&offset={offset}"
        )
        payload = fetch_json(url)
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            raise SystemExit(f"Unexpected payload for {query}: missing data[]")
        pages += 1
        pagination = payload.get("pagination") or {}
        total = int(pagination.get("totalResults") or total or 0)
        if not chunk:
            break
        for item in chunk:
            row = normalize_row(item)
            key = row["trader"].lower()
            prev = by_addr.get(key)
            if prev is None or row["volume"] > prev["volume"]:
                by_addr[key] = row
        print(
            f"    page {pages} offset={offset} got={len(chunk)} "
            f"unique={len(by_addr)} total={total} hasMore={pagination.get('hasMore')}",
            flush=True,
        )
        if not pagination.get("hasMore"):
            break
        offset += len(chunk)
        if pages > 400:
            raise SystemExit(f"Pagination did not finish for {query}")
        time.sleep(REQUEST_PAUSE_S)
    rows = list(by_addr.values())
    rows.sort(key=lambda r: (-r["volume"], r["rank"], r["trader"].lower()))
    return rows, total


def share(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return part / whole * 100.0


def aggregate(rows: list[dict], window: str, compiled: dict, api_total: int) -> dict:
    total_volume = 0.0
    buckets = {
        "A": {"count": 0, "volume": 0.0, "addresses": []},
        "B": {"count": 0, "volume": 0.0, "addresses": []},
        "C": {"count": 0, "volume": 0.0, "addresses": []},
    }
    retail_count = 0
    retail_volume = 0.0
    top_retail: dict | None = None
    for row in rows:
        verdict = classify(row, window, compiled)
        volume = row["volume"]
        total_volume += volume
        tier = verdict["tier"]
        if tier in buckets:
            buckets[tier]["count"] += 1
            buckets[tier]["volume"] += volume
            if tier == "C":
                buckets["C"]["addresses"].append(
                    {
                        "address": row["trader"],
                        "volume": round(volume, 6),
                        "rank": row["rank"],
                        "trade_count": row["trade_count"],
                        "pnl_pct": row["pnl_pct"],
                        "win_rate": row["win_rate"],
                        "source": verdict["source"],
                    }
                )
        else:
            retail_count += 1
            retail_volume += volume
            if top_retail is None or volume > top_retail["volume"]:
                top_retail = {
                    "trader": row["trader"],
                    "volume": volume,
                    "rank": row["rank"],
                }
    mm_count = sum(buckets[k]["count"] for k in buckets)
    mm_volume = sum(buckets[k]["volume"] for k in buckets)
    buckets["C"]["addresses"].sort(key=lambda item: -item["volume"])
    tier_summary = {}
    for key, bucket in buckets.items():
        entry = {
            "count": bucket["count"],
            "volume": bucket["volume"],
            "share_pct": share(bucket["volume"], total_volume),
        }
        if key == "C":
            entry["addresses"] = bucket["addresses"]
        tier_summary[key] = entry
    return {
        "complete": True,
        "scanned_traders": len(rows),
        "total_traders": api_total or len(rows),
        "total_volume": total_volume,
        "mm": {
            "count": mm_count,
            "volume": mm_volume,
            "share_pct": share(mm_volume, total_volume),
            "tiers": tier_summary,
        },
        "retail": {
            "count": retail_count,
            "volume": retail_volume,
            "share_pct": share(retail_volume, total_volume),
            "top": top_retail,
        },
    }


def window_block(rows: list[dict], window: str, compiled: dict, api_total: int, extra: dict | None = None) -> dict:
    block = {
        "period": window,
        "total_traders": api_total or len(rows),
        "returned": min(TOP_N, len(rows)),
        "rows": rows[:TOP_N],
        "aggregate": aggregate(rows, window, compiled, api_total),
    }
    if extra:
        block.update(extra)
    return block


def parse_points_week_start(spec: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{2}):(\d{2})\s+UTC", spec.strip())
    if not match:
        raise SystemExit(
            "pointsWeekStart must look like 'Thursday 00:00 UTC' "
            f"(got {spec!r})"
        )
    name = match.group(1).lower()
    if name not in WEEKDAY_NAMES:
        raise SystemExit(f"Unknown weekday in pointsWeekStart: {spec}")
    hour = int(match.group(2))
    minute = int(match.group(3))
    if hour > 23 or minute > 59:
        raise SystemExit(f"Bad time in pointsWeekStart: {spec}")
    return WEEKDAY_NAMES.index(name), hour, minute


def current_points_week(now: datetime, spec: str) -> tuple[datetime, datetime]:
    weekday, hour, minute = parse_points_week_start(spec)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_days = (candidate.weekday() - weekday) % 7
    start = candidate - timedelta(days=delta_days)
    if start > now:
        start -= timedelta(days=7)
    end = start + timedelta(days=7)
    return start, end


def fetch_markets_volume() -> dict:
    total = 0.0
    count = 0
    offset = 0
    api_total = 0
    pages = 0
    while True:
        url = f"{API_BASE}{MARKETS_PATH}?limit={PAGE_SIZE}&offset={offset}"
        payload = fetch_json(url)
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            raise SystemExit("Unexpected markets payload: missing data[]")
        pages += 1
        pagination = payload.get("pagination") or {}
        api_total = int(pagination.get("totalResults") or api_total or 0)
        if not chunk:
            break
        for market in chunk:
            total += float(market.get("volume") or 0)
            count += 1
        print(
            f"    markets page {pages} offset={offset} got={len(chunk)} "
            f"volume={total:.2f}",
            flush=True,
        )
        if not pagination.get("hasMore"):
            break
        offset += len(chunk)
        time.sleep(REQUEST_PAUSE_S)
    return {
        "source": f"{API_BASE}{MARKETS_PATH}",
        "fetched_at": iso_now(),
        "market_count": count,
        "reported_markets": api_total or count,
        "total_volume": total,
        "note": (
            "Sum of the volume field on every market returned by "
            "GET /api/v1/markets. This is a lifetime market notional, not a "
            "windowed total, so it is only compared with the all-time trader sum."
        ),
    }


def fetch_points_meta() -> dict:
    payload = fetch_json(POINTS_URL)
    latest = payload.get("latestWeek") or {}
    return {
        "source": "https://www.42.space/api/points/leaderboard",
        "fetched_at": iso_now(),
        "snapshot_id": payload.get("snapshotId"),
        "updated_at": payload.get("updatedAt"),
        "period": payload.get("period"),
        "comparison_period": payload.get("comparisonPeriod"),
        "latest_week": latest or None,
        "note": (
            "latestWeek is the last distributed points week (exclusive end). "
            "It is not the in-progress week. The points route does not send "
            "Access-Control-Allow-Origin for third-party pages, so the "
            "browser reads this copy from the snapshot."
        ),
    }


def print_summary(window: str, block: dict) -> None:
    agg = block["aggregate"]
    tiers = agg["mm"]["tiers"]
    top = (agg["retail"].get("top") or {}).get("trader")
    print(
        f"  {window}: traders={agg['scanned_traders']}/{agg['total_traders']} "
        f"volume={agg['total_volume']:.2f} "
        f"mm={agg['mm']['count']}/{agg['mm']['volume']:.2f} ({agg['mm']['share_pct']:.2f}%) "
        f"retail={agg['retail']['count']}/{agg['retail']['volume']:.2f} "
        f"({agg['retail']['share_pct']:.2f}%) "
        f"A={tiers['A']['count']} B={tiers['B']['count']} C={tiers['C']['count']} "
        f"top_retail={top}",
        flush=True,
    )
    for item in tiers["C"]["addresses"]:
        print(
            f"    tierC {item['address']} vol={item['volume']:.2f} "
            f"rank={item['rank']} trades={item['trade_count']} "
            f"pnl%={item['pnl_pct']:.4f} win={item['win_rate']:.4f}",
            flush=True,
        )


def main() -> int:
    started = iso_now()
    compiled = compile_labels(load_labels())
    now = datetime.now(timezone.utc)
    week_start, week_end = current_points_week(now, compiled["points_week_start"])
    week_to_date = (week_end - timedelta(seconds=1)).date().isoformat()
    week_from = week_start.date().isoformat()
    print(
        f"points week {compiled['points_week_start']}: "
        f"{week_start.strftime('%Y-%m-%dT%H:%M:%SZ')} -> "
        f"{week_end.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"(query from={week_from}&to={week_to_date})",
        flush=True,
    )

    periods: dict[str, dict] = {}
    for index, period in enumerate(PERIODS):
        if index:
            time.sleep(REQUEST_PAUSE_S)
        print(f"fetching period={period} ...", flush=True)
        rows, total = fetch_leaderboard(f"?period={period}&")
        periods[period] = window_block(rows, period, compiled, total)
        print_summary(period, periods[period])

    print("fetching points week ...", flush=True)
    week_rows, week_total = fetch_leaderboard(f"?from={week_from}&to={week_to_date}&")
    periods["week"] = window_block(
        week_rows,
        "week",
        compiled,
        week_total,
        extra={
            "from": week_from,
            "to": week_to_date,
            "start": week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_exclusive": week_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points_week_start": compiled["points_week_start"],
            "method": (
                "GET /api/v1/market-data/leaderboard with from/to UTC calendar "
                "dates, inclusive. from is the Thursday date, to is the "
                "Wednesday date, matching Thursday 00:00 UTC through Wednesday "
                "23:59 UTC. A future `to` is accepted and returns week-to-date."
            ),
        },
    )
    print_summary("week", periods["week"])

    print("fetching markets ...", flush=True)
    markets = fetch_markets_volume()
    all_volume = periods["all"]["aggregate"]["total_volume"]
    if all_volume > 0:
        diff_pct = (markets["total_volume"] - all_volume) / all_volume * 100.0
    else:
        diff_pct = 0.0
    markets["all_time_trader_volume"] = all_volume
    markets["difference_pct"] = diff_pct
    markets["consistent"] = abs(diff_pct) <= MARKET_CONSISTENT_PCT
    markets["consistent_threshold_pct"] = MARKET_CONSISTENT_PCT
    print(
        f"  markets={markets['market_count']} volume={markets['total_volume']:.2f} "
        f"vs traders={all_volume:.2f} diff={diff_pct:.2f}% "
        f"consistent={markets['consistent']}",
        flush=True,
    )

    print("fetching points metadata ...", flush=True)
    points = fetch_points_meta()
    print(f"  latestWeek={points.get('latest_week')} snapshot={points.get('snapshot_id')}", flush=True)

    doc = {
        "source": f"{API_BASE}{LEADERBOARD_PATH}",
        "source_page": "https://www.42.space/points",
        "sort_by": "volume",
        "unit": "USDT",
        "note": (
            "Per-trader trading volume from the public 42.space Market API. "
            "Rolling windows use period=1d|7d|30d|all. The points week uses "
            "from/to UTC calendar dates. Each block keeps the top "
            f"{TOP_N} rows for the table. aggregate covers every trader "
            "returned for that window, including heuristic market-maker and "
            "bot detection. Labels come from wallet-labels.json."
        ),
        "updated_at": iso_now(),
        "fetched_at": started,
        "labels_file": "wallet-labels.json",
        "points_week_start": compiled["points_week_start"],
        "points_api": points,
        "markets": markets,
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
