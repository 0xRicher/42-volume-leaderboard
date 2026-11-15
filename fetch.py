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
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
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
TIMEOUT_S = 90
MAX_RETRIES = 5
# Market-list volume within this percent of all-time trader volume counts as
# the same platform total. See README for why the two series can diverge.
MARKET_CONSISTENT_PCT = 1.0
USER_AGENT = "42-volume-leaderboard/1.2 (+https://github.com/42-volume-leaderboard)"

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "leaderboard.json"
WEEKS_DIR = ROOT / "data" / "weeks"
LABELS_PATH = ROOT / "wallet-labels.json"
# Points for a finished week can land several hours after Wednesday 00:00 UTC.
# Keep retrying until this grace passes, then freeze the file.
POINTS_GRACE = timedelta(hours=48)
POINTS_PAGE_SIZE = 200
# Walk back through empty Thursday weeks before deciding history has ended.
EMPTY_WEEK_STOP = 2
HISTORY_FLOOR = datetime(2025, 1, 1, tzinfo=timezone.utc)

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


def fetch_json(url: str, *, tolerate: tuple[int, ...] = ()) -> tuple[int, dict]:
    """GET JSON. Returns (status, payload).

    Statuses in `tolerate` are returned instead of aborting, so a points
    query for an unknown week (HTTP 400) can be recorded as "no points".
    """
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
                status = resp.status
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise SystemExit(f"Unexpected JSON from {url}")
            return status, payload
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            text = body.decode("utf-8", errors="replace")[:500]
            if exc.code in tolerate:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"error": text}
                if not isinstance(payload, dict):
                    payload = {"error": text}
                return exc.code, payload
            last_error = RuntimeError(f"HTTP {exc.code} for {url}: {text}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise SystemExit(str(last_error)) from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Network error for {url}: {exc.reason}")
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            last_error = RuntimeError(f"Network error for {url}: {exc}")
        time.sleep(min(8.0, 0.6 * (2**attempt)))
    raise SystemExit(str(last_error))


def fetch_ok(url: str) -> dict:
    _status, payload = fetch_json(url)
    return payload


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
    launch = labels.get("pointsLaunch") or {}
    if not launch.get("from") or not launch.get("to"):
        raise SystemExit("wallet-labels.json is missing pointsLaunch.from and pointsLaunch.to")
    return {
        "tiers": tiers,
        "listed": listed,
        "identities": identities,
        "heuristics": heuristics,
        "points_week_start": str(labels["pointsWeekStart"]),
        "points_launch_from": str(launch["from"]),
        "points_launch_to": str(launch["to"]),
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
        payload = fetch_ok(url)
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
        payload = fetch_ok(url)
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
    payload = fetch_ok(POINTS_URL)
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


def points_iso(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def same_instant(left: datetime, right: datetime) -> bool:
    return abs((left - right).total_seconds()) < 1


def week_dates(start: datetime, end_exclusive: datetime) -> tuple[str, str]:
    """UTC calendar dates for an inclusive leaderboard from/to query."""
    inclusive_end = end_exclusive - timedelta(seconds=1)
    return start.date().isoformat(), inclusive_end.date().isoformat()


def week_path(start: datetime) -> Path:
    return WEEKS_DIR / f"{start.date().isoformat()}.json"


def load_week_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def probe_trader_count(day_from: str, day_to: str) -> int:
    url = (
        f"{API_BASE}{LEADERBOARD_PATH}?sort_by=volume&limit=1&offset=0"
        f"&from={day_from}&to={day_to}"
    )
    payload = fetch_ok(url)
    pagination = payload.get("pagination") or {}
    return int(pagination.get("totalResults") or 0)


def find_earliest_day(now: datetime) -> datetime.date | None:
    """Earliest UTC day the volume leaderboard has at least one trader."""
    lo = HISTORY_FLOOR.date()
    hi = now.astimezone(timezone.utc).date()
    found: datetime.date | None = None
    while lo <= hi:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        count = probe_trader_count(mid.isoformat(), mid.isoformat())
        print(f"  probe {mid.isoformat()} traders={count}", flush=True)
        if count > 0:
            found = mid
            hi = mid - timedelta(days=1)
        else:
            lo = mid + timedelta(days=1)
        time.sleep(REQUEST_PAUSE_S)
    return found


def thursday_series(earliest: datetime.date | None, current_start: datetime, spec: str) -> list[datetime]:
    weekday, hour, minute = parse_points_week_start(spec)
    if earliest is None:
        return [current_start]
    delta = (earliest.weekday() - weekday) % 7
    first_day = earliest - timedelta(days=delta)
    first = datetime.combine(first_day, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if first > current_start:
        return [current_start]
    starts: list[datetime] = []
    cursor = first
    while cursor <= current_start + timedelta(seconds=1):
        if cursor > current_start:
            break
        starts.append(cursor)
        cursor += timedelta(days=7)
    if not starts or not same_instant(starts[-1], current_start):
        starts.append(current_start)
    return starts


def should_skip_week(path: Path, end_exclusive: datetime, now: datetime) -> bool:
    """Finished weeks are frozen. The open week, and a just-ended week still
    waiting on its points distribution, are fetched again."""
    if now < end_exclusive:
        return False
    doc = load_week_file(path)
    if not doc or not doc.get("finalized"):
        return False
    aggregate = (doc.get("volume") or {}).get("aggregate") or {}
    if not aggregate.get("complete"):
        return False
    if doc.get("hasPoints"):
        return True
    status = (doc.get("points") or {}).get("status") or ""
    if status == "unavailable":
        return True
    if now < end_exclusive + POINTS_GRACE and status in {"not_distributed", "pending", "mismatch"}:
        return False
    return status in {"not_distributed", "pending", "mismatch"}


def points_period_bounds(payload: dict) -> tuple[datetime, datetime] | None:
    period = payload.get("period") or {}
    raw_from = period.get("from")
    raw_to = period.get("to")
    if not raw_from or not raw_to:
        return None
    try:
        return parse_iso(str(raw_from)), parse_iso(str(raw_to))
    except ValueError:
        return None


def volume_snapshot_fields(row: dict | None) -> dict:
    if row is None:
        return {
            "volume": 0.0,
            "volume_rank": None,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "winning_predictions": 0,
            "losing_predictions": 0,
            "traded": False,
        }
    return {
        "volume": row["volume"],
        "volume_rank": row["rank"],
        "pnl": row["pnl"],
        "pnl_pct": row["pnl_pct"],
        "win_rate": row["win_rate"],
        "trade_count": row["trade_count"],
        "winning_predictions": row["winning_predictions"],
        "losing_predictions": row["losing_predictions"],
        "traded": True,
    }


POINTS_NOTES = {
    "weekly": "积分接口按这一周的起止时间返回了发放榜。",
    "not_distributed": "这一周的积分还没发放。页面不会用其他周的积分充数。",
    "unavailable": "积分接口不接受这一周的起止时间，没有可展示的积分，只有成交量。",
    "mismatch": "积分接口返回的窗口和这一周对不上，积分数字没有写入。",
    "pending": "这一周已经结束，积分可能还在发放，下次更新会再试。",
}


def fetch_points_pages(start: datetime, end_exclusive: datetime) -> tuple[str, dict]:
    """Paginate a points window.

    Returns ("unavailable", body) on HTTP 400. Any 200 whose echoed period is
    not exactly [start, end) stops after the first page and drops the rows,
    so an unrelated leaderboard cannot be stored as this week's points.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = None
    first: dict | None = None
    pages = 0
    while True:
        query: dict[str, str] = {
            "limit": str(POINTS_PAGE_SIZE),
            "from": points_iso(start),
            "to": points_iso(end_exclusive),
        }
        if cursor:
            query["cursor"] = cursor
        url = "https://www.42.space/api/points/leaderboard?" + urllib.parse.urlencode(query)
        status, payload = fetch_json(url, tolerate=(400,))
        pages += 1
        if status == 400:
            return "unavailable", payload
        if status != 200:
            raise SystemExit(f"Unexpected points status {status} for {url}")
        if pages == 1:
            first = payload
            bounds = points_period_bounds(payload)
            matches = bool(
                bounds
                and same_instant(bounds[0], start)
                and same_instant(bounds[1], end_exclusive)
            )
            if not matches:
                rejected = dict(payload)
                rejected["data"] = []
                return "ok", rejected
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            raise SystemExit("Points payload is missing data[]")
        for item in chunk:
            address = str(item.get("address") or "")
            key = address.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rank = item.get("rank")
            rows.append(
                {
                    "address": address,
                    "name": item.get("name"),
                    "rank": int(rank) if rank is not None else None,
                    "points": float(item.get("points") or 0),
                    "rank_change": item.get("rankChange"),
                    "points_change": item.get("pointsChange"),
                }
            )
        print(
            f"    points page {pages} got={len(chunk)} unique={len(rows)} "
            f"traders={payload.get('totalTraders')}",
            flush=True,
        )
        cursor = payload.get("nextCursor") or None
        if not cursor or not chunk:
            break
        if pages > 200:
            raise SystemExit("Points pagination did not finish")
        time.sleep(REQUEST_PAUSE_S)
    assert first is not None
    merged = dict(first)
    merged["data"] = rows
    return "ok", merged


def classify_points_payload(payload: dict, start: datetime, end_exclusive: datetime) -> str:
    bounds = points_period_bounds(payload)
    if bounds and same_instant(bounds[0], start) and same_instant(bounds[1], end_exclusive):
        traders = int(payload.get("totalTraders") or 0)
        if traders <= 0 and not (payload.get("data") or []):
            return "not_distributed"
        return "weekly"
    period = payload.get("period") or {}
    if period.get("from") and period.get("from") == period.get("to"):
        return "not_distributed"
    if int(payload.get("totalTraders") or 0) <= 0 and not (payload.get("data") or []):
        return "not_distributed"
    return "mismatch"


def fetch_week_points(
    start: datetime,
    end_exclusive: datetime,
    volume_by_addr: dict[str, dict],
    kind: str,
) -> dict:
    query = {"from": points_iso(start), "to": points_iso(end_exclusive)}
    outcome, payload = fetch_points_pages(start, end_exclusive)
    if outcome == "unavailable":
        return {
            "status": "unavailable",
            "note": POINTS_NOTES["unavailable"],
            "source": "https://www.42.space/api/points/leaderboard",
            "query": query,
            "error": payload.get("error"),
            "fetched_at": iso_now(),
            "rows": [],
        }
    status = classify_points_payload(payload, start, end_exclusive)
    note = POINTS_NOTES[status]
    if kind in {"distribution", "launch"} and status == "weekly":
        note = (
            "这是积分接口 latestWeek 的发放窗口，从公告日算到下一周周四 00:00 UTC，"
            "共 8 天，不是周四到周三的标准积分周。" + note
        )
    rows: list[dict] = []
    if status == "weekly":
        for item in payload.get("data") or []:
            stats = volume_snapshot_fields(volume_by_addr.get(str(item["address"]).lower()))
            rows.append({**item, **stats})
        rows.sort(key=lambda item: (item["rank"] is None, item["rank"] or 0, item["address"].lower()))
    return {
        "status": status,
        "note": note,
        "source": "https://www.42.space/api/points/leaderboard",
        "query": query,
        "fetched_at": iso_now(),
        "snapshot_id": payload.get("snapshotId"),
        "updated_at": payload.get("updatedAt"),
        "period": payload.get("period"),
        "latest_week": payload.get("latestWeek"),
        "total_traders": int(payload.get("totalTraders") or 0) if status == "weekly" else None,
        "total_distributed_points": (
            float(payload["totalDistributedPoints"])
            if status == "weekly" and payload.get("totalDistributedPoints") is not None
            else None
        ),
        "returned": len(rows),
        "rows": rows,
    }


def build_week_document(
    *,
    start: datetime,
    end_exclusive: datetime,
    kind: str,
    now: datetime,
    volume_rows: list[dict],
    api_total: int,
    compiled: dict,
    points: dict,
) -> dict:
    week_from, week_to = week_dates(start, end_exclusive)
    status = str(points.get("status") or "pending")
    ended = now >= end_exclusive
    if ended and status == "not_distributed" and now < end_exclusive + POINTS_GRACE:
        points = dict(points)
        status = "pending"
        points["status"] = status
        points["note"] = POINTS_NOTES["pending"]
    has_points = status == "weekly" and bool(points.get("rows"))
    points_settled = status in {"weekly", "unavailable"} or (
        ended and now >= end_exclusive + POINTS_GRACE
    )
    volume = window_block(
        volume_rows,
        "week",
        compiled,
        api_total,
        extra={
            "from": week_from,
            "to": week_to,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_exclusive": end_exclusive.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points_week_start": compiled["points_week_start"],
            "kind": kind,
        },
    )
    by_points = {
        str(item.get("address") or "").lower(): item for item in (points.get("rows") or [])
    }
    for row in volume["rows"]:
        match = by_points.get(str(row["trader"]).lower())
        row["points_rank"] = match.get("rank") if match else None
        row["points"] = match.get("points") if match else None
        row["name"] = match.get("name") if match else None
    return {
        "start": start.date().isoformat(),
        "end": (end_exclusive - timedelta(seconds=1)).date().isoformat(),
        "start_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_exclusive": end_exclusive.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "current": (not ended) and kind == "thursday",
        "finalized": bool(ended and points_settled),
        "hasPoints": has_points,
        "points": points,
        "volume": volume,
    }


def index_entry(doc: dict) -> dict:
    aggregate = doc["volume"]["aggregate"]
    points = doc.get("points") or {}
    return {
        "start": doc["start"],
        "end": doc["end"],
        "startAt": doc["start_at"],
        "endExclusive": doc["end_exclusive"],
        "kind": doc["kind"],
        "group": doc.get("group") or "points",
        "weekNumber": doc.get("weekNumber"),
        "current": bool(doc.get("current")),
        "finalized": bool(doc.get("finalized")),
        "hasPoints": bool(doc.get("hasPoints")),
        "pointsStatus": points.get("status"),
        "pointsNote": points.get("note"),
        "totals": {
            "traders": aggregate.get("total_traders"),
            "total_volume": aggregate.get("total_volume"),
            "mm_share_pct": (aggregate.get("mm") or {}).get("share_pct"),
            "mm_volume": (aggregate.get("mm") or {}).get("volume"),
            "mm_count": (aggregate.get("mm") or {}).get("count"),
            "retail_volume": (aggregate.get("retail") or {}).get("volume"),
            "retail_share_pct": (aggregate.get("retail") or {}).get("share_pct"),
            "retail_count": (aggregate.get("retail") or {}).get("count"),
            "points_traders": points.get("total_traders") if doc.get("hasPoints") else None,
            "total_distributed_points": (
                points.get("total_distributed_points") if doc.get("hasPoints") else None
            ),
        },
    }


def refresh_week(
    start: datetime,
    end_exclusive: datetime,
    kind: str,
    now: datetime,
    compiled: dict,
    *,
    volume_rows: list[dict] | None = None,
    api_total: int | None = None,
) -> dict:
    week_from, week_to = week_dates(start, end_exclusive)
    print(f"week {kind} {week_from}..{week_to}", flush=True)
    if volume_rows is None or api_total is None:
        volume_rows, api_total = fetch_leaderboard(f"?from={week_from}&to={week_to}&")
    by_addr = {row["trader"].lower(): row for row in volume_rows}
    points = fetch_week_points(start, end_exclusive, by_addr, kind)
    doc = build_week_document(
        start=start,
        end_exclusive=end_exclusive,
        kind=kind,
        now=now,
        volume_rows=volume_rows,
        api_total=api_total,
        compiled=compiled,
        points=points,
    )
    path = week_path(start)
    write_json(path, doc)
    aggregate = doc["volume"]["aggregate"]
    print(
        f"  wrote {path.name} finalized={doc['finalized']} hasPoints={doc['hasPoints']} "
        f"status={points.get('status')} traders={aggregate['total_traders']} "
        f"volume={aggregate['total_volume']:.2f} "
        f"mm={aggregate['mm']['share_pct']:.2f}% "
        f"retail={aggregate['retail']['volume']:.2f}",
        flush=True,
    )
    return doc


def plan_week_slots(
    earliest: datetime.date | None,
    current_start: datetime,
    spec: str,
    launch_start: datetime,
    launch_end: datetime,
) -> list[dict]:
    """Points weeks start at the launch distribution.

    Week 1 is the configured launch window (2026-11-04 to 2026-11-12).
    Week 2 and later are Thursday weeks from that exclusive end onward.
    Thursday weeks that start before the launch stay as unnumbered
    pre-points volume weeks. A Thursday week that starts inside the launch
    window (2026-11-05) overlaps it and is omitted.
    """
    slots: list[dict] = []
    for start in thursday_series(earliest, current_start, spec):
        end = start + timedelta(days=7)
        if start < launch_start:
            slots.append({"start": start, "end": end, "kind": "pre", "group": "pre"})
        elif same_instant(start, launch_end) or start > launch_end:
            slots.append({"start": start, "end": end, "kind": "thursday", "group": "points"})
        # launch_start < start < launch_end overlaps the launch window.
    if not any(same_instant(slot["start"], launch_start) for slot in slots):
        slots.append({"start": launch_start, "end": launch_end, "kind": "launch", "group": "points"})
    slots.sort(key=lambda slot: slot["start"])
    number = 0
    for slot in slots:
        if slot["group"] == "points":
            number += 1
            slot["weekNumber"] = number
        else:
            slot["weekNumber"] = None
    return slots


def discover_distribution_week() -> tuple[datetime, datetime] | None:
    # POINTS_URL already includes limit=1. A second ?limit= makes the API 400.
    payload = fetch_ok(POINTS_URL)
    latest = payload.get("latestWeek") or {}
    if not latest.get("from") or not latest.get("to"):
        return None
    try:
        start = parse_iso(str(latest["from"]))
        end = parse_iso(str(latest["to"]))
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


def update_week_history(
    compiled: dict,
    now: datetime,
    current_rows: list[dict] | None,
    current_total: int | None,
    current_start: datetime,
    current_end: datetime,
) -> dict:
    """Write one file per kept week and refresh data/weeks/index.json.

    Pre-points Thursday weeks (before the launch) stay as volume-only files.
    The launch distribution is week 1. Later Thursday weeks are numbered from
    the launch end. A Thursday week that starts inside the launch window is
    deleted and not listed. Finished files are not fetched again.
    """
    del current_end  # the open week is identified by current_start
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    launch_start = parse_iso(compiled["points_launch_from"])
    launch_end = parse_iso(compiled["points_launch_to"])
    print("discovering earliest volume day ...", flush=True)
    earliest = find_earliest_day(now)
    print(f"  earliest day={earliest}", flush=True)
    slots = plan_week_slots(
        earliest,
        current_start,
        compiled["points_week_start"],
        launch_start,
        launch_end,
    )
    keep_names = {week_path(slot["start"]).name for slot in slots}
    keep_names.add("index.json")
    for path in WEEKS_DIR.glob("*.json"):
        if path.name not in keep_names:
            path.unlink()
            print(f"removed overlapping or stale week {path.name}", flush=True)

    documents: list[dict] = []
    current_key = current_start.date().isoformat()
    for slot in slots:
        start = slot["start"]
        end = slot["end"]
        path = week_path(start)
        label = f"{slot['kind']} {start.date().isoformat()}"
        if should_skip_week(path, end, now):
            existing = load_week_file(path)
            if not existing:
                continue
            if existing.get("current"):
                existing["current"] = False
                write_json(path, existing)
            doc = existing
            print(f"week {label} cached", flush=True)
        elif slot["group"] == "points" and same_instant(start, current_start):
            doc = refresh_week(
                start,
                end,
                "thursday",
                now,
                compiled,
                volume_rows=current_rows,
                api_total=current_total,
            )
        else:
            doc = refresh_week(start, end, slot["kind"], now, compiled)
        indexed = dict(doc)
        indexed["group"] = slot["group"]
        indexed["weekNumber"] = slot["weekNumber"]
        indexed["kind"] = slot["kind"]
        indexed["current"] = slot["group"] == "points" and indexed.get("start") == current_key
        documents.append(indexed)

    documents.sort(key=lambda doc: doc.get("start") or "", reverse=True)
    points_weeks = [
        doc["start"] for doc in documents if doc.get("group") == "points" and doc.get("hasPoints")
    ]
    index = {
        "updated_at": iso_now(),
        "points_week_start": compiled["points_week_start"],
        "points_launch": {
            "from": compiled["points_launch_from"],
            "to": compiled["points_launch_to"],
        },
        "current_start": current_key,
        "earliest_day": earliest.isoformat() if earliest else None,
        "pointsHistory": {
            "available": bool(points_weeks),
            "mode": "api-boundary",
            "weeks_with_points": points_weeks,
            "detail": (
                "第1周是积分上线发放窗口。之后的积分周从该窗口结束的周四算起。"
                "更早的周四只有成交量，归在「积分前」，不编号。"
                "落在上线窗口内部的标准周四不单列。"
                "积分接口对不上的周不会编造积分。"
            ),
        },
        "weeks": [index_entry(doc) for doc in documents],
    }
    write_json(WEEKS_DIR / "index.json", index)
    print(
        f"wrote {WEEKS_DIR / 'index.json'} weeks={len(documents)} "
        f"with_points={points_weeks or 'none'}",
        flush=True,
    )
    return index


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
    # Keep the full scan. window_block stores only the top rows, and the
    # per-week file reuses this scan for the in-progress week.
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
        "weeks_index": "data/weeks/index.json",
    }

    write_json(OUT_PATH, doc)
    print(f"wrote {OUT_PATH}", flush=True)

    update_week_history(
        compiled,
        now,
        week_rows,
        week_total,
        week_start,
        week_end,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
