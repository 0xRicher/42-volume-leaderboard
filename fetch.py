#!/usr/bin/env python3
"""Pull 42.space volume leaderboards and write data/leaderboard.json.

No secrets required. Talks only to the public Market API and the public
points leaderboard used by https://www.42.space.

Classification rules live in wallet-labels.json. The same file is loaded by
index.html; keep the two implementations in step (thresholds are read from
that file, not hardcoded here).
"""

from __future__ import annotations

import copy
import gzip
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
USER_AGENT = "42-volume-leaderboard/1.3 (+https://github.com/42-volume-leaderboard)"
# Token-race volume calls take a few seconds each. Stay at or under the
# documented burst (x-ratelimit-limit: 10) by keeping this many in flight.
BLITZ_WORKERS = 4
BLITZ_REFRESH = timedelta(hours=1)
RECONCILE_USDT = 1.0
# Per-wallet Blitz calls are slow. One Actions run spends at most this long
# on them and resumes from the cache on the next run.
PER_WALLET_BUDGET_S = 300
# Player-table volumes are separate from the attribution crawl above. The
# leaderboard itself is one or two requests. Filling userVolume is capped so
# a run never adds more than this on top of the existing budget. Finalized
# weeks reuse volumes already stored on the week file.
BLITZ_PLAYER_BUDGET_S = 180
BLITZ_PLAYER_PAGE = 500
# Open week: wallets whose racesPlayed or leaderboard total moved since the
# stored userVolume are refreshed first. After those, re-read this many
# leaders by PnL and by races when the stored figure is older than
# BLITZ_REFRESH. Everyone still missing a volume is queued ahead of both.
BLITZ_PLAYER_REFRESH_TOP = 25
# Ignore sub-cent noise when deciding that a wallet's total volume changed.
VOLUME_CHANGE_EPS = 0.01
AGENT_HISTORY_START = datetime(2026, 10, 15, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "leaderboard.json"
WEEKS_DIR = ROOT / "data" / "weeks"
BLITZ_CACHE_PATH = WEEKS_DIR / "_blitz_cache.jsonl"
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
    agents: dict[str, dict] = {}
    agent_group = (labels.get("groups") or {}).get("agent") or {}
    for item in agent_group.get("addresses") or []:
        if isinstance(item, str):
            address = item
            meta = {"id": "agent", "label": "Agent", "tooltip": agent_group.get("tooltip") or ""}
        elif isinstance(item, dict) and item.get("address"):
            address = str(item["address"])
            meta = {
                "id": str(item.get("id") or "agent"),
                "label": str(item.get("label") or item.get("id") or "Agent"),
                "tooltip": str(item.get("tooltip") or agent_group.get("tooltip") or ""),
            }
        else:
            raise SystemExit(f"Bad agent entry in wallet-labels.json: {item!r}")
        meta["address"] = address
        agents[address.lower()] = meta
    return {
        "tiers": tiers,
        "listed": listed,
        "identities": identities,
        "agents": agents,
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


def listed_tier(addr: str, window: str, compiled: dict) -> str | None:
    entries = compiled["listed"].get(addr) or []
    for tier_id in ("A", "B", "C"):
        for entry in entries:
            if entry["tier"] == tier_id and _allows(entry["periods"], window):
                return tier_id
    return None


def classify(
    row: dict,
    window: str,
    compiled: dict,
    seed_addresses: set[str] | None = None,
    *,
    apply_agents: bool = True,
) -> dict:
    """Return {tier, source, identity}. tier is 'A'|'B'|'C'|None.

    Mirrors index.html. Agents and Blitz seed wallets are neither market-maker
    nor retail. An explicit period allow-list that excludes `window` suppresses
    heuristics for that address (the 0x3cE29… rule). `apply_agents=False` reproduces
    the classification used before the agent group existed, so a stored aggregate
    can be rebucketed without a full rescan.
    """
    addr = str(row.get("trader") or row.get("address") or "").lower()
    agent = compiled.get("agents", {}).get(addr) if apply_agents else None
    if agent:
        return {"tier": None, "source": "agent", "identity": None, "agent": agent}
    if seed_addresses and addr in seed_addresses:
        return {
            "tier": None,
            "source": "seed",
            "identity": None,
            "listed_tier": listed_tier(addr, window, compiled),
        }
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


def aggregate(
    rows: list[dict],
    window: str,
    compiled: dict,
    api_total: int,
    seed_addresses: set[str] | None = None,
) -> dict:
    total_volume = 0.0
    buckets = {
        "A": {"count": 0, "volume": 0.0, "addresses": []},
        "B": {"count": 0, "volume": 0.0, "addresses": []},
        "C": {"count": 0, "volume": 0.0, "addresses": []},
    }
    retail_count = 0
    retail_volume = 0.0
    top_retail: dict | None = None
    agent_count = 0
    agent_volume = 0.0
    agent_addresses: list[dict] = []
    seed_count = 0
    seed_volume = 0.0
    seed_rows: list[dict] = []
    for row in rows:
        verdict = classify(row, window, compiled, seed_addresses)
        volume = row["volume"]
        total_volume += volume
        tier = verdict["tier"]
        source = verdict["source"]
        if source == "agent":
            agent_count += 1
            agent_volume += volume
            agent_meta = verdict.get("agent") or {}
            agent_addresses.append(
                {
                    "address": row["trader"],
                    "id": agent_meta.get("id"),
                    "label": agent_meta.get("label"),
                    "volume": round(volume, 6),
                    "rank": row["rank"],
                }
            )
        elif source == "seed":
            seed_count += 1
            seed_volume += volume
            seed_rows.append(
                {
                    "address": row["trader"],
                    "volume": round(volume, 6),
                    "rank": row["rank"],
                    "listed_tier": verdict.get("listed_tier"),
                }
            )
        elif tier in buckets:
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
        "agent": {
            "count": agent_count,
            "volume": agent_volume,
            "share_pct": share(agent_volume, total_volume),
            "addresses": sorted(agent_addresses, key=lambda item: -item["volume"]),
        },
        "seed": {
            "count": seed_count,
            "volume": seed_volume,
            "share_pct": share(seed_volume, total_volume),
            "addresses": sorted(seed_rows, key=lambda item: -item["volume"]),
        },
    }


def window_block(
    rows: list[dict],
    window: str,
    compiled: dict,
    api_total: int,
    extra: dict | None = None,
    seed_addresses: set[str] | None = None,
) -> dict:
    block = {
        "period": window,
        "total_traders": api_total or len(rows),
        "returned": min(TOP_N, len(rows)),
        "rows": rows[:TOP_N],
        "aggregate": aggregate(rows, window, compiled, api_total, seed_addresses),
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
    """Earliest UTC day the volume leaderboard has at least one trader.

    The index already records this. It does not move earlier, so a scheduled
    run reuses it instead of probing the API again.
    """
    index = load_week_file(WEEKS_DIR / "index.json") or {}
    raw = index.get("earliest_day")
    if raw:
        try:
            cached = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            cached = None
        today = now.astimezone(timezone.utc).date()
        if cached and HISTORY_FLOOR.date() <= cached <= today:
            print(f"  earliest day={cached.isoformat()} (cached)", flush=True)
            return cached
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
            "agent_volume": (aggregate.get("agent") or {}).get("volume"),
            "agent_count": (aggregate.get("agent") or {}).get("count"),
            "seed_volume": (aggregate.get("seed") or {}).get("volume"),
            "seed_count": (aggregate.get("seed") or {}).get("count"),
            "points_traders": points.get("total_traders") if doc.get("hasPoints") else None,
            "total_distributed_points": (
                points.get("total_distributed_points") if doc.get("hasPoints") else None
            ),
        },
        "breakdown": _breakdown_summary(doc.get("breakdown")),
    }


def _breakdown_summary(breakdown: dict | None) -> dict | None:
    if not isinstance(breakdown, dict):
        return None
    agents = breakdown.get("agents") or {}
    detail = {}
    for key in ("codex", "claude", "grok"):
        entry = agents.get(key)
        if isinstance(entry, dict):
            detail[key] = entry.get("volume")
        else:
            detail[key] = None
    return {
        "status": breakdown.get("status"),
        "fetchedAt": breakdown.get("fetched_at"),
        "total": breakdown.get("total"),
        "regular": breakdown.get("regular"),
        "blitzTotal": breakdown.get("blitz_total"),
        "blitzReal": breakdown.get("blitz_real"),
        "blitzSeed": breakdown.get("blitz_seed"),
        "agents": agents.get("sum"),
        "agentsDetail": detail,
        "reconciled": breakdown.get("reconciled"),
        "note": breakdown.get("note") or "",
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
    doc["_scanned_rows"] = volume_rows
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
    keep_names.add("cumulative.json")
    for path in WEEKS_DIR.glob("*.json"):
        if path.name not in keep_names:
            path.unlink()
            print(f"removed overlapping or stale week {path.name}", flush=True)

    history_from = AGENT_HISTORY_START
    pending_start: datetime | None = None
    for slot in slots:
        start = slot["start"]
        end = slot["end"]
        path = week_path(start)
        existing = load_week_file(path)
        is_current = slot["group"] == "points" and same_instant(start, current_start)
        frozen = (
            not is_current
            and should_skip_week(path, end, now)
            and (
                breakdown_fresh((existing or {}).get("breakdown"), now, current=False, kind=slot["kind"])
                or platform_split_cached((existing or {}).get("breakdown"))
            )
        )
        if frozen:
            continue
        if pending_start is None or start < pending_start:
            pending_start = start
    if pending_start is not None:
        history_from = max(AGENT_HISTORY_START, pending_start - timedelta(days=1))
    print(f"fetching agent mining history from {history_from.date().isoformat()} ...", flush=True)
    histories = fetch_agent_histories(compiled, history_from, now)
    load_blitz_cache()
    wallet_budget = WalletBudget(PER_WALLET_BUDGET_S)
    documents: list[dict] = []
    current_key = current_start.date().isoformat()
    for slot in slots:
        start = slot["start"]
        end = slot["end"]
        path = week_path(start)
        label = f"{slot['kind']} {start.date().isoformat()}"
        is_current = slot["group"] == "points" and same_instant(start, current_start)
        existing = load_week_file(path)
        # Finished weeks keep the platform split already in the file. Wallet
        # gaps are not re-crawled: the per-wallet cache does not survive a
        # fresh Actions checkout, so repeating the crawl spends the budget
        # and never resumes.
        if should_skip_week(path, end, now) and (
            breakdown_fresh((existing or {}).get("breakdown"), now, current=False, kind=slot["kind"])
            or platform_split_cached((existing or {}).get("breakdown"))
        ):
            if not existing:
                continue
            if existing.get("current"):
                existing["current"] = False
                write_json(path, existing)
            doc = existing
            print(f"week {label} cached", flush=True)
        else:
            if should_skip_week(path, end, now) and existing:
                doc = existing
                if doc.get("current"):
                    doc["current"] = False
                print(f"week {label} adding breakdown", flush=True)
            elif is_current:
                # Fetch this week's leaderboard now, not the copy taken before
                # the historical Blitz backfill, so the total matches Blitz.
                doc = refresh_week(start, end, "thursday", now, compiled)
            else:
                doc = refresh_week(start, end, slot["kind"], now, compiled)
            if existing and existing.get("breakdown") and not doc.get("breakdown"):
                doc["breakdown"] = existing["breakdown"]
            if existing and existing.get("blitzPlayers") and not doc.get("blitzPlayers"):
                doc["blitzPlayers"] = existing["blitzPlayers"]
            doc = enrich_week_breakdown(
                doc, compiled, histories, now, current=is_current, budget=wallet_budget
            )
            # Full per-wallet totals stay in memory for the open-week volume
            # refresh. They are not part of the week file.
            open_volumes = doc.pop("_open_volumes", None)
            write_json(path, doc)
            if open_volumes:
                doc["_open_volumes"] = open_volumes
        indexed = dict(doc)
        indexed.pop("_scanned_rows", None)
        indexed["group"] = slot["group"]
        indexed["weekNumber"] = slot["weekNumber"]
        indexed["kind"] = slot["kind"]
        indexed["current"] = slot["group"] == "points" and indexed.get("start") == current_key
        documents.append(indexed)

    documents.sort(key=lambda doc: doc.get("start") or "", reverse=True)
    attach_blitz_players(documents, compiled, now, current_start)
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
    seed_wallets: list[str] = []
    seen_seed: set[str] = set()
    for doc in documents:
        for address in (doc.get("breakdown") or {}).get("seed_addresses") or []:
            key = str(address).lower()
            if key and key not in seen_seed:
                seen_seed.add(key)
                seed_wallets.append(str(address))
    index["blitzSeedWallets"] = seed_wallets
    agents_now = fetch_mining_agents()
    if agents_now:
        index["agentsNow"] = agents_now
    write_json(WEEKS_DIR / "index.json", index)
    flush_blitz_cache()
    print(
        f"wrote {WEEKS_DIR / 'index.json'} weeks={len(documents)} "
        f"with_points={points_weeks or 'none'}",
        flush=True,
    )
    return index


def fetch_points_overall() -> dict:
    """The site's Overall board: /api/points/leaderboard with no from/to.

    That payload is every distributed point up to the latest snapshot,
    including points from before the first weekly window. A response whose
    period has a `from` is a weekly slice, not this board.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = None
    first: dict | None = None
    pages = 0
    while True:
        query: dict[str, str] = {"limit": str(POINTS_PAGE_SIZE)}
        if cursor:
            query["cursor"] = cursor
        elif first is not None:
            period = first.get("period") or {}
            if period.get("to"):
                query["to"] = str(period["to"])
        url = "https://www.42.space/api/points/leaderboard?" + urllib.parse.urlencode(query)
        status, payload = fetch_json(url, tolerate=(400,))
        pages += 1
        if status == 400:
            return {
                "status": "unavailable",
                "note": "积分总榜接口返回 400，没有可展示的全部积分。页面不会用各周积分相加来代替。",
                "source": "https://www.42.space/api/points/leaderboard",
                "query": {"limit": POINTS_PAGE_SIZE},
                "fetched_at": iso_now(),
                "rows": [],
            }
        if status != 200:
            raise SystemExit(f"Unexpected points status {status} for {url}")
        if pages == 1:
            first = payload
            period = payload.get("period") or {}
            if period.get("from"):
                return {
                    "status": "unavailable",
                    "note": (
                        "不带 from/to 的积分接口返回了带起点的窗口，不是全部积分总榜。"
                        "页面不会把各周积分加总来估算。"
                    ),
                    "source": "https://www.42.space/api/points/leaderboard",
                    "query": {"limit": POINTS_PAGE_SIZE},
                    "fetched_at": iso_now(),
                    "period": period,
                    "rows": [],
                }
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            raise SystemExit("Points overall payload is missing data[]")
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
            f"    points overall page {pages} got={len(chunk)} unique={len(rows)} "
            f"traders={payload.get('totalTraders')}",
            flush=True,
        )
        cursor = payload.get("nextCursor") or None
        if not cursor or not chunk:
            break
        if pages > 400:
            raise SystemExit("Points overall pagination did not finish")
        time.sleep(REQUEST_PAUSE_S)
    assert first is not None
    total_points = first.get("totalDistributedPoints")
    return {
        "status": "overall",
        "note": (
            "站点积分页 Overall 用的就是不带 from/to 的 /api/points/leaderboard。"
            "这个榜包含第1周之前已经发放的积分，不是只把每周发放加起来。"
        ),
        "source": "https://www.42.space/api/points/leaderboard",
        "query": {"limit": POINTS_PAGE_SIZE},
        "fetched_at": iso_now(),
        "snapshot_id": first.get("snapshotId"),
        "updated_at": first.get("updatedAt"),
        "period": first.get("period"),
        "comparison_period": first.get("comparisonPeriod"),
        "total_traders": int(first.get("totalTraders") or len(rows)),
        "total_distributed_points": float(total_points) if total_points is not None else None,
        "returned": len(rows),
        "rows": rows,
    }


def build_cumulative(
    compiled: dict,
    now: datetime,
    volume_rows: list[dict] | None = None,
    api_total: int | None = None,
    seed_addresses: set[str] | None = None,
) -> dict:
    """All-time points (official overall board) joined to all-time volume."""
    print("building 总榜 cumulative.json ...", flush=True)
    points = fetch_points_overall()
    if volume_rows is None or api_total is None:
        print("fetching all-time volume for 总榜 ...", flush=True)
        volume_rows, api_total = fetch_leaderboard("?period=all&")
    by_addr = {row["trader"].lower(): row for row in volume_rows}
    joined_points: list[dict] = []
    if points.get("status") == "overall":
        for item in points.get("rows") or []:
            stats = volume_snapshot_fields(by_addr.get(str(item["address"]).lower()))
            if stats["traded"]:
                vol = by_addr[str(item["address"]).lower()]
                item = dict(item)
                item["address"] = vol["trader"]
            joined_points.append({**item, **stats})
        joined_points.sort(
            key=lambda item: (item["rank"] is None, item["rank"] or 0, str(item["address"]).lower())
        )
    points = dict(points)
    points["rows"] = joined_points
    points["returned"] = len(joined_points)
    volume = window_block(
        volume_rows,
        "all",
        compiled,
        api_total or len(volume_rows),
        extra={
            "period": "all",
            "method": (
                "GET /api/v1/market-data/leaderboard?period=all. "
                "All-time trader volume from platform start, same window as the "
                "main 全部 tab."
            ),
        },
        seed_addresses=seed_addresses,
    )
    by_points = {str(item.get("address") or "").lower(): item for item in joined_points}
    for row in volume["rows"]:
        match = by_points.get(str(row["trader"]).lower())
        row["points_rank"] = match.get("rank") if match else None
        row["points"] = match.get("points") if match else None
        row["name"] = match.get("name") if match else None
    doc = {
        "id": "cumulative",
        "label": "总榜（全部积分）",
        "kind": "cumulative",
        "fetched_at": iso_now(),
        "hasPoints": points.get("status") == "overall" and bool(joined_points),
        "points": points,
        "volume": volume,
    }
    attach_cumulative_breakdown(doc, compiled, now)
    path = WEEKS_DIR / "cumulative.json"
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(path, doc)
    aggregate = volume["aggregate"]
    print(
        f"  wrote {path.name} points={points.get('status')} "
        f"wallets={points.get('total_traders')} "
        f"points_total={points.get('total_distributed_points')} "
        f"traders={aggregate['total_traders']} volume={aggregate['total_volume']:.2f} "
        f"mm={aggregate['mm']['share_pct']:.2f}% retail={aggregate['retail']['volume']:.2f}",
        flush=True,
    )
    return doc


_BLITZ_CACHE: dict[str, float] = {}
_BLITZ_CACHE_NEW: list[str] = []
_BLITZ_CACHE_LOCK = threading.Lock()


def attribution_limit(base: float | None) -> float:
    """Wallet sums can miss a few dollars of rounding. Don't page the book for that."""
    return max(25.0, abs(base or 0.0) * 0.0002)


def amounts_close(left: float | None, right: float | None, tolerance: float = RECONCILE_USDT) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= max(tolerance, abs(right) * 1e-6)


def fetch_ok_soft(url: str) -> dict | None:
    try:
        return fetch_ok(url)
    except (SystemExit, Exception) as exc:
        print(f"  soft-fail {url}: {exc}", flush=True)
        return None


def load_blitz_cache() -> None:
    if not BLITZ_CACHE_PATH.exists():
        return
    loaded = 0
    for line in BLITZ_CACHE_PATH.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            _BLITZ_CACHE[str(item["k"])] = float(item["v"])
            loaded += 1
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    if loaded:
        print(f"blitz cache {loaded} entries", flush=True)


def flush_blitz_cache() -> None:
    with _BLITZ_CACHE_LOCK:
        pending = list(_BLITZ_CACHE_NEW)
        _BLITZ_CACHE_NEW.clear()
        payload = [{"k": key, "v": _BLITZ_CACHE[key]} for key in pending if key in _BLITZ_CACHE]
    if not payload:
        return
    BLITZ_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BLITZ_CACHE_PATH.open("a", encoding="utf-8") as handle:
        for item in payload:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _cache_get(key: str) -> float | None:
    with _BLITZ_CACHE_LOCK:
        if key in _BLITZ_CACHE:
            return _BLITZ_CACHE[key]
    return None


def _cache_put(key: str, value: float) -> None:
    with _BLITZ_CACHE_LOCK:
        if key not in _BLITZ_CACHE:
            _BLITZ_CACHE_NEW.append(key)
        _BLITZ_CACHE[key] = value
        pending = len(_BLITZ_CACHE_NEW)
    if pending >= 20:
        flush_blitz_cache()


def blitz_cache_key(frm: int, to: int, user: str, exclude: bool) -> str:
    return f"{frm}:{to}:{user.lower()}:{1 if exclude else 0}"


def fetch_blitz_user(
    frm: int,
    to: int,
    user: str,
    exclude: bool,
    *,
    use_cache: bool,
    cache_scope: str = "",
    refresh: bool = False,
) -> float | None:
    # Finished weeks use a stable key. The open week adds an hour bucket so a
    # later run can resume inside that hour without freezing an old value forever.
    # refresh=True skips that read: a wallet whose races or total volume just
    # moved must not reuse the figure stored earlier in the same hour.
    key = blitz_cache_key(frm, to, user, exclude) + (f":{cache_scope}" if cache_scope else "")
    if not refresh and (use_cache or cache_scope):
        cached = _cache_get(key)
        if cached is not None:
            return cached
    url = f"{API_BASE}/api/v1/token-races/volume?from={frm}&to={to}&user={user}"
    if exclude:
        url += "&excludeSeed=true"
    payload = fetch_ok_soft(url)
    if not payload:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if not isinstance(data, dict):
        return None
    raw = data.get("userVolume")
    try:
        value = 0.0 if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        return None
    # Always remember per-wallet results. The open week keys include an hour
    # bucket, so they are not reused after that hour or after the week closes.
    if use_cache or cache_scope or refresh:
        _cache_put(key, value)
    return value


def fetch_blitz_totals(frm: int, to: int, *, use_cache: bool) -> tuple[float | None, float | None, int | None]:
    """Return (total, real, raceCount). real is excludeSeed=true."""

    def one(exclude: bool) -> tuple[float | None, int | None]:
        key = blitz_cache_key(frm, to, "*", exclude)
        if use_cache:
            cached = _cache_get(key)
            if cached is not None:
                count_key = key + ":races"
                count = _cache_get(count_key)
                return cached, None if count is None else int(count)
        url = f"{API_BASE}/api/v1/token-races/volume?from={frm}&to={to}"
        if exclude:
            url += "&excludeSeed=true"
        payload = fetch_ok_soft(url)
        if not payload:
            return None, None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if not isinstance(data, dict) or data.get("totalVolume") is None:
            return None, None
        try:
            total = float(data["totalVolume"])
            races = int(data.get("raceCount") or 0)
        except (TypeError, ValueError):
            return None, None
        if use_cache:
            _cache_put(key, total)
            _cache_put(key + ":races", float(races))
        return total, races

    total, races = one(False)
    real, races_real = one(True)
    return total, real, races if races is not None else races_real


class WalletBudget:
    """Wall-clock cap for per-wallet Blitz calls. Cache hits do not matter."""

    def __init__(self, seconds: float = PER_WALLET_BUDGET_S):
        self.deadline = time.monotonic() + seconds
        self.exhausted = False

    def allow(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.exhausted = True
            return False
        return True


def parallel_map(fn, items: list, workers: int = BLITZ_WORKERS, budget: WalletBudget | None = None):
    if not items:
        return []
    if budget is None:
        return _parallel_map(fn, items, workers)
    done: list = []
    # One wave of workers. A larger batch keeps running after the deadline
    # because each Blitz call takes many seconds.
    step = max(1, workers)
    for offset in range(0, len(items), step):
        if not budget.allow():
            print("    per-wallet budget reached; resume next run", flush=True)
            break
        done.extend(_parallel_map(fn, items[offset:offset + step], workers))
    return done


def _parallel_map(fn, items: list, workers: int) -> list:
    out = [None] * len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                out[index] = future.result()
            except Exception as exc:
                print(f"  blitz worker error: {exc}", flush=True)
                out[index] = None
            done += 1
            if done % 25 == 0 or done == len(items):
                print(f"    blitz wallets {done}/{len(items)}", flush=True)
    flush_blitz_cache()
    return out


def fetch_race_leaderboard(day_from: str, day_to: str) -> list[dict] | None:
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    pages = 0
    while True:
        url = (
            f"{API_BASE}/api/v1/token-races/leaderboard"
            f"?from={day_from}&to={day_to}&include_total=1&limit=100&offset={offset}"
        )
        payload = fetch_ok_soft(url)
        if payload is None:
            return None
        chunk = payload.get("data") or []
        if not isinstance(chunk, list):
            return None
        pages += 1
        for item in chunk:
            address = str(item.get("walletAddress") or "")
            key = address.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(item)
        pagination = payload.get("pagination") or {}
        print(
            f"    race board page {pages} got={len(chunk)} unique={len(rows)} "
            f"total={pagination.get('totalResults')}",
            flush=True,
        )
        if not chunk or not pagination.get("hasMore"):
            break
        offset += len(chunk)
        if pages > 80:
            return None
        time.sleep(REQUEST_PAUSE_S)
    return rows


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_history_chunk(agent_id: str, start: datetime, end: datetime, depth: int = 0) -> list[dict] | None:
    query = urllib.parse.urlencode(
        {"agent": agent_id, "from": rfc3339(start), "to": rfc3339(end)}
    )
    payload = fetch_ok_soft(f"{API_BASE}/api/v1/mining/history?{query}")
    if payload is None:
        return None
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return None
    points: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("hour"):
            continue
        try:
            points.append(
                {"hour": parse_iso(str(row["hour"])), "volume": float(row.get("volume") or 0)}
            )
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda item: item["hour"])
    if (
        points
        and points[0]["hour"] - start > timedelta(hours=2)
        and end - start > timedelta(hours=12)
        and depth < 8
    ):
        mid = start + (end - start) / 2
        left = fetch_history_chunk(agent_id, start, mid, depth + 1)
        right = fetch_history_chunk(agent_id, mid, end, depth + 1)
        if left is None or right is None:
            return None
        return left + right
    return points


def fetch_agent_histories(compiled: dict, start: datetime, end: datetime) -> dict[str, list[dict] | None]:
    histories: dict[str, list[dict] | None] = {}
    for meta in compiled.get("agents", {}).values():
        agent_id = str(meta["id"])
        cursor = start
        series: list[dict] = []
        failed = False
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(days=20))
            part = fetch_history_chunk(agent_id, cursor, chunk_end)
            if part is None:
                failed = True
                break
            series.extend(part)
            cursor = chunk_end
        if failed:
            histories[agent_id] = None
            print(f"  agent {agent_id} history unavailable", flush=True)
            continue
        dedup: dict[datetime, float] = {}
        for point in series:
            dedup[point["hour"]] = point["volume"]
        histories[agent_id] = [{"hour": hour, "volume": dedup[hour]} for hour in sorted(dedup)]
        print(f"  agent {agent_id} snapshots={len(histories[agent_id])}", flush=True)
    return histories


def agent_window_volume(
    series: list[dict] | None,
    start: datetime,
    end: datetime,
    now: datetime,
) -> tuple[float | None, dict]:
    """Cumulative mining volume inside [start, end).

    Snapshot timestamps are as-of times. The window's volume is the snapshot at
    or just before `end` (or now, if the week is still open) minus the snapshot
    at or just before `start`.
    """
    if series is None:
        return None, {"reason": "history unavailable"}
    if not series:
        return 0.0, {"baseline": 0.0, "end": 0.0}
    as_of = min(end, now)
    at_start = [point for point in series if point["hour"] <= start]
    up_to_end = [point for point in series if point["hour"] <= as_of]
    if at_start:
        baseline = at_start[-1]["volume"]
        baseline_hour = at_start[-1]["hour"]
    elif series[0]["hour"] > start and (series[0]["volume"] == 0 or series[0]["hour"] >= end):
        baseline = 0.0
        baseline_hour = None
    elif series[0]["hour"] > start and series[0]["volume"] > 0:
        return None, {"reason": "missing baseline before window"}
    else:
        baseline = 0.0
        baseline_hour = None
    if not up_to_end or up_to_end[-1]["hour"] < start:
        return 0.0, {
            "baseline": baseline,
            "end": baseline,
            "baseline_hour": rfc3339(baseline_hour) if baseline_hour else None,
        }
    end_point = up_to_end[-1]
    return end_point["volume"] - baseline, {
        "baseline": baseline,
        "end": end_point["volume"],
        "baseline_hour": rfc3339(baseline_hour) if baseline_hour else None,
        "end_hour": rfc3339(end_point["hour"]),
    }


def fetch_mining_agents() -> dict | None:
    payload = fetch_ok_soft(f"{API_BASE}/api/v1/mining/agents")
    if not payload:
        return None
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return None
    agents = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        volume = row.get("volume")
        try:
            volume_value = None if volume is None else float(volume)
        except (TypeError, ValueError):
            volume_value = None
        agents.append(
            {
                "agent": row.get("agent"),
                "minted": row.get("minted"),
                "supply": row.get("supply"),
                "volume": volume_value,
                "snapshotAt": row.get("snapshotAt"),
            }
        )
    return {"fetched_at": iso_now(), "agents": agents}


def platform_split_cached(breakdown: dict | None) -> bool:
    """True when the file already has the four platform numbers.

    That is enough to keep a finished week. Per-wallet Blitz columns stay as
    stored; a later run does not spend the wallet budget on them.
    """
    if not isinstance(breakdown, dict) or breakdown.get("status") == "unavailable":
        return False
    if breakdown.get("blitz_total") is None or breakdown.get("blitz_real") is None:
        return False
    if breakdown.get("blitz_seed") is None:
        return False
    agents = (breakdown.get("agents") or {}).get("sum")
    return agents is not None


def breakdown_fresh(breakdown: dict | None, now: datetime, *, current: bool, kind: str | None = None) -> bool:
    if not isinstance(breakdown, dict) or breakdown.get("blitz_total") is None:
        return False
    agents = (breakdown.get("agents") or {}).get("sum")
    if agents is None:
        return False
    # Pre-points weeks only need the platform split. Don't crawl wallets again.
    if kind == "pre":
        return True
    if breakdown.get("status") != "ok":
        check = breakdown.get("wallet_check") or {}
        real_gap = check.get("real_gap")
        seed_gap = check.get("seed_gap")
        if (
            check.get("failed")
            or real_gap is None
            or seed_gap is None
            or not breakdown.get("wallets")
            or abs(float(real_gap)) > attribution_limit(breakdown.get("blitz_real"))
            or abs(float(seed_gap)) > attribution_limit(breakdown.get("blitz_seed"))
        ):
            return False
    if not current:
        return True
    raw = breakdown.get("fetched_at")
    if not raw:
        return False
    try:
        fetched = parse_iso(str(raw))
    except ValueError:
        return False
    return now - fetched < BLITZ_REFRESH


def _volume_of(rows: list[dict] | None, address: str) -> float | None:
    if rows is None:
        return None
    key = address.lower()
    for row in rows:
        if str(row.get("trader") or "").lower() == key:
            return float(row.get("volume") or 0)
    return 0.0


def _classify_bucket(verdict: dict) -> str:
    if verdict.get("source") == "agent":
        return "agent"
    if verdict.get("source") == "seed":
        return "seed"
    tier = verdict.get("tier")
    if tier in {"A", "B", "C"}:
        return tier
    return "retail"


def rebucket_aggregate(
    aggregate: dict,
    rows: list[dict],
    compiled: dict,
    window: str,
    seed_addresses: set[str],
) -> dict:
    """Move stored top rows that are agents or Blitz seed out of MM and retail.

    Idempotent: an address already listed under agent or seed is left alone.
    Wallets outside `rows` stay in whatever bucket the original full scan used.
    """
    agg = copy.deepcopy(aggregate)
    mm = agg.setdefault("mm", {})
    tiers = mm.setdefault("tiers", {})
    for key in ("A", "B", "C"):
        bucket = tiers.setdefault(key, {"count": 0, "volume": 0.0, "share_pct": 0.0})
        bucket.setdefault("count", 0)
        bucket.setdefault("volume", 0.0)
    if "C" in tiers:
        tiers["C"].setdefault("addresses", [])
    retail = agg.setdefault("retail", {"count": 0, "volume": 0.0, "share_pct": 0.0, "top": None})
    agent = agg.setdefault("agent", {"count": 0, "volume": 0.0, "share_pct": 0.0, "addresses": []})
    seed = agg.setdefault("seed", {"count": 0, "volume": 0.0, "share_pct": 0.0, "addresses": []})
    agent.setdefault("addresses", [])
    seed.setdefault("addresses", [])
    already = {
        str(item.get("address") or "").lower()
        for item in (agent.get("addresses") or []) + (seed.get("addresses") or [])
    }

    def add(bucket: str, row: dict, sign: int) -> None:
        volume = float(row.get("volume") or 0) * sign
        if bucket in {"A", "B", "C"}:
            tiers[bucket]["count"] = int(tiers[bucket].get("count") or 0) + sign
            tiers[bucket]["volume"] = float(tiers[bucket].get("volume") or 0) + volume
            if bucket == "C" and sign < 0:
                tiers["C"]["addresses"] = [
                    item
                    for item in tiers["C"].get("addresses") or []
                    if str(item.get("address") or "").lower() != str(row.get("trader") or "").lower()
                ]
        elif bucket == "retail":
            retail["count"] = int(retail.get("count") or 0) + sign
            retail["volume"] = float(retail.get("volume") or 0) + volume
        elif bucket == "agent":
            agent["count"] = int(agent.get("count") or 0) + sign
            agent["volume"] = float(agent.get("volume") or 0) + volume
        elif bucket == "seed":
            seed["count"] = int(seed.get("count") or 0) + sign
            seed["volume"] = float(seed.get("volume") or 0) + volume

    for row in rows:
        verdict = classify(row, window, compiled, seed_addresses)
        bucket = _classify_bucket(verdict)
        if bucket not in {"agent", "seed"}:
            continue
        key = str(row.get("trader") or "").lower()
        if not key or key in already:
            continue
        old = _classify_bucket(classify(row, window, compiled, None, apply_agents=False))
        add(old, row, -1)
        add(bucket, row, 1)
        record = {
            "address": row.get("trader"),
            "volume": round(float(row.get("volume") or 0), 6),
            "rank": row.get("rank"),
        }
        if bucket == "agent":
            meta = verdict.get("agent") or {}
            record["id"] = meta.get("id")
            record["label"] = meta.get("label")
            agent["addresses"].append(record)
        else:
            record["listed_tier"] = verdict.get("listed_tier")
            seed["addresses"].append(record)
        already.add(key)

    total = float(agg.get("total_volume") or 0)
    for key in ("A", "B", "C"):
        if tiers[key]["count"] < 0 and tiers[key]["count"] > -1:
            tiers[key]["count"] = 0
        if -1 < tiers[key]["volume"] < 0:
            tiers[key]["volume"] = 0.0
        tiers[key]["share_pct"] = share(tiers[key]["volume"], total)
    mm["count"] = sum(int(tiers[key]["count"]) for key in ("A", "B", "C"))
    mm["volume"] = sum(float(tiers[key]["volume"]) for key in ("A", "B", "C"))
    mm["share_pct"] = share(mm["volume"], total)
    for bucket in (retail, agent, seed):
        if int(bucket.get("count") or 0) < 0 and int(bucket.get("count") or 0) > -1:
            bucket["count"] = 0
        if -1 < float(bucket.get("volume") or 0) < 0:
            bucket["volume"] = 0.0
        bucket["share_pct"] = share(float(bucket.get("volume") or 0), total)
    agent["addresses"].sort(key=lambda item: -float(item.get("volume") or 0))
    seed["addresses"].sort(key=lambda item: -float(item.get("volume") or 0))
    top = retail.get("top") or {}
    top_key = str(top.get("trader") or "").lower()
    if top_key in already:
        best = None
        for row in rows:
            if _classify_bucket(classify(row, window, compiled, seed_addresses)) != "retail":
                continue
            if best is None or float(row.get("volume") or 0) > float(best.get("volume") or 0):
                best = row
        retail["top"] = (
            {"trader": best["trader"], "volume": best["volume"], "rank": best["rank"]}
            if best
            else None
        )
    agg["pool_adjusted"] = True
    return agg


def _empty_breakdown(status: str, note: str) -> dict:
    return {
        "status": status,
        "fetched_at": iso_now(),
        "formula": "regular = total - blitz_total - agents; blitz_seed = blitz_total - blitz_real",
        "total": None,
        "blitz_total": None,
        "blitz_real": None,
        "blitz_seed": None,
        "agents": {"sum": None, "codex": None, "claude": None, "grok": None},
        "regular": None,
        "reconciled": False,
        "residual": None,
        "race_count": None,
        "seed_addresses": [],
        "note": note,
        "wallet_check": {
            "real_sum": None,
            "real_gap": None,
            "seed_sum": None,
            "seed_gap": None,
            "failed": 0,
        },
        "sources": {
            "blitz": "GET /api/v1/token-races/volume",
            "leaderboard": "GET /api/v1/token-races/leaderboard",
            "agents": "GET /api/v1/mining/history",
        },
        "wallets": [],
    }


def build_week_breakdown(
    doc: dict,
    compiled: dict,
    histories: dict[str, list[dict] | None],
    now: datetime,
    scanned: list[dict] | None,
    *,
    per_wallet: bool = True,
    budget: WalletBudget | None = None,
) -> dict:
    start = parse_iso(doc["start_at"])
    end = parse_iso(doc["end_exclusive"])
    aggregate = (doc.get("volume") or {}).get("aggregate") or {}
    total = aggregate.get("total_volume")
    total_value = None if total is None else float(total)
    frm = int(start.timestamp())
    to = int(end.timestamp())
    use_cache = now >= end
    wallet_scope = "" if use_cache else now.strftime("%Y%m%d%H")
    day_from, day_to = week_dates(start, end)
    print(f"  blitz {day_from}..{day_to} unix={frm}..{to}", flush=True)
    blitz_total, blitz_real, race_count = fetch_blitz_totals(frm, to, use_cache=use_cache)
    if blitz_total is None and blitz_real is None:
        return _empty_breakdown("unavailable", "Blitz 成交量没有取到。")
    blitz_seed = None
    if (
        blitz_total is not None
        and blitz_real is not None
        and blitz_real <= blitz_total + RECONCILE_USDT
    ):
        blitz_seed = blitz_total - blitz_real

    volume_rows = scanned if scanned is not None else ((doc.get("volume") or {}).get("rows") or [])
    agent_rows: dict[str, dict] = {}
    agent_sum = 0.0
    agent_ok = True
    for meta in compiled.get("agents", {}).values():
        delta, detail = agent_window_volume(histories.get(str(meta["id"])), start, end, now)
        leaderboard = _volume_of(scanned, meta["address"])
        if leaderboard is None:
            leaderboard = _volume_of((doc.get("volume") or {}).get("rows") or [], meta["address"])
            if leaderboard is None and delta == 0:
                leaderboard_match: bool | None = True
            elif leaderboard is None:
                leaderboard_match = None
            else:
                leaderboard_match = amounts_close(delta, leaderboard)
        else:
            leaderboard_match = amounts_close(delta, leaderboard) if delta is not None else False
        entry = {
            "address": meta["address"],
            "label": meta["label"],
            "volume": delta,
            "leaderboard_volume": leaderboard,
            "baseline_volume": detail.get("baseline"),
            "end_volume": detail.get("end"),
            "baseline_hour": detail.get("baseline_hour"),
            "end_hour": detail.get("end_hour"),
            "match": leaderboard_match,
        }
        if detail.get("reason"):
            entry["reason"] = detail["reason"]
        agent_rows[str(meta["id"])] = entry
        if delta is None:
            agent_ok = False
        else:
            agent_sum += delta

    notes: list[str] = []
    wallets: list[dict] = []
    seed_addresses: list[str] = []
    failed = 0
    real_sum = 0.0
    seed_sum = 0.0
    real_gap = None
    seed_gap = None
    quiet = (
        blitz_total is not None
        and blitz_seed is not None
        and blitz_total <= RECONCILE_USDT
        and blitz_seed <= RECONCILE_USDT
    )
    if quiet:
        real_gap = (blitz_real or 0) - 0
        seed_gap = (blitz_seed or 0) - 0
        for meta in compiled.get("agents", {}).values():
            agent_volume = (agent_rows.get(str(meta["id"])) or {}).get("volume")
            leaderboard = _volume_of(scanned, meta["address"])
            if leaderboard is None:
                leaderboard = _volume_of((doc.get("volume") or {}).get("rows") or [], meta["address"])
            regular = None
            if leaderboard is not None and agent_volume is not None:
                regular = leaderboard - agent_volume
            wallets.append(
                {
                    "address": meta["address"],
                    "blitz": 0.0,
                    "regular": regular,
                    "leaderboard": leaderboard,
                    "agent": agent_volume,
                    "role": "agent",
                    "races": None,
                }
            )
    elif not per_wallet:
        notes.append("积分前的周只统计全平台拆分，不逐个钱包查 Blitz。表格里的 Blitz 列没有逐钱包数字。")
    elif budget is not None and not budget.allow():
        notes.append("本轮每钱包 Blitz 已到约 5 分钟上限，下次运行从缓存继续。")
    else:
        race_rows = fetch_race_leaderboard(day_from, day_to)
        race_addrs = [str(item.get("walletAddress") or "") for item in (race_rows or [])]
        race_addrs = [addr for addr in race_addrs if addr]
        race_keys = {addr.lower() for addr in race_addrs}
        races_played = {
            str(item.get("walletAddress") or "").lower(): item.get("racesPlayed")
            for item in (race_rows or [])
        }
        if race_rows is None:
            notes.append("竞速榜没有取到，真实玩家没能逐个对上。")
            failed += 1

        def exclude_job(address: str):
            return address, fetch_blitz_user(frm, to, address, True, use_cache=True, cache_scope=wallet_scope)

        real_of: dict[str, float | None] = {}
        if race_addrs:
            for address, value in parallel_map(exclude_job, race_addrs, budget=budget):
                real_of[address.lower()] = value

        labeled: list[str] = []
        seen_labeled: set[str] = set()
        for key in compiled.get("listed", {}):
            if key not in seen_labeled:
                labeled.append(key)
                seen_labeled.add(key)
        for meta in compiled.get("agents", {}).values():
            key = meta["address"].lower()
            if key not in seen_labeled:
                labeled.append(meta["address"])
                seen_labeled.add(key)
        plain_targets = list(labeled)
        for address in race_addrs:
            if real_of.get(address.lower()) == 0 and address.lower() not in {item.lower() for item in plain_targets}:
                plain_targets.append(address)

        def plain_job(address: str):
            return address, fetch_blitz_user(frm, to, address, False, use_cache=True, cache_scope=wallet_scope)

        plain_of: dict[str, float | None] = {}
        if plain_targets:
            for address, value in parallel_map(plain_job, plain_targets, budget=budget):
                plain_of[address.lower()] = value

        def remember_wallet(address: str, role: str, blitz: float, leaderboard: float | None, agent_volume: float | None):
            regular = None
            if leaderboard is not None and blitz is not None and agent_volume is not None:
                regular = leaderboard - blitz - agent_volume
            elif leaderboard is not None and blitz is not None and role != "agent":
                regular = leaderboard - blitz
            wallets.append(
                {
                    "address": address,
                    "blitz": blitz,
                    "regular": regular,
                    "leaderboard": leaderboard,
                    "agent": agent_volume,
                    "role": role,
                    "races": races_played.get(address.lower()),
                }
            )

        known: set[str] = set()
        for address, value in real_of.items():
            if value is None:
                failed += 1
                continue
            if value > RECONCILE_USDT:
                casing = next((item for item in race_addrs if item.lower() == address), address)
                leaderboard = _volume_of(volume_rows if scanned is not None else None, casing)
                if leaderboard is None:
                    leaderboard = _volume_of((doc.get("volume") or {}).get("rows") or [], casing)
                remember_wallet(casing, "blitz_real", value, leaderboard, None)
                real_sum += value
                known.add(address)

        def consider_seed(address: str) -> bool:
            key = address.lower()
            plain = plain_of.get(key)
            if plain is None:
                return False
            excluded = real_of.get(key)
            if excluded is None and key in race_keys:
                return False
            if excluded is None:
                excluded_value = fetch_blitz_user(frm, to, address, True, use_cache=True, cache_scope=wallet_scope)
                real_of[key] = excluded_value
                if excluded_value is None:
                    return False
                excluded = excluded_value
            if key in known:
                return True
            leaderboard = _volume_of(volume_rows if scanned is not None else None, address)
            if leaderboard is None:
                leaderboard = _volume_of((doc.get("volume") or {}).get("rows") or [], address)
            if plain > RECONCILE_USDT and excluded <= RECONCILE_USDT:
                remember_wallet(address, "seed", plain, leaderboard, None)
                known.add(key)
                seed_addresses.append(address)
                return True
            if excluded > RECONCILE_USDT:
                # A labeled wallet can be a real Blitz trader and still be
                # absent from the race leaderboard. Count that volume.
                remember_wallet(address, "blitz_real", excluded, leaderboard, None)
                known.add(key)
                return True
            return False

        for address in list(plain_of):
            consider_seed(address)
        seed_sum = sum(
            float(item["blitz"]) for item in wallets if item["role"] == "seed" and item.get("blitz") is not None
        )
        real_sum = sum(
            float(item["blitz"]) for item in wallets if item["role"] == "blitz_real" and item.get("blitz") is not None
        )
        real_short = (
            blitz_real is not None
            and race_rows is not None
            and blitz_real - real_sum > attribution_limit(blitz_real)
        )
        seed_short = blitz_seed is not None and blitz_seed - seed_sum > attribution_limit(blitz_seed)
        if real_short or seed_short:
            ranked = sorted(volume_rows, key=lambda row: -float(row.get("volume") or 0))
            extra = []
            for row in ranked:
                key = str(row.get("trader") or "").lower()
                if not key or key in known or key in plain_of:
                    continue
                extra.append(str(row["trader"]))
                if len(extra) >= 200:
                    break

            def both_job(address: str):
                plain = fetch_blitz_user(frm, to, address, False, use_cache=True, cache_scope=wallet_scope)
                excluded = fetch_blitz_user(frm, to, address, True, use_cache=True, cache_scope=wallet_scope)
                return address, plain, excluded

            if extra:
                print(
                    f"    wallet gap seed={0 if blitz_seed is None else blitz_seed - seed_sum:.2f} "
                    f"real={0 if blitz_real is None else blitz_real - real_sum:.2f}, "
                    f"checking up to {len(extra)} large wallets",
                    flush=True,
                )
                for offset in range(0, len(extra), 20):
                    batch = extra[offset:offset + 20]
                    if budget is not None and not budget.allow():
                        notes.append("本轮每钱包 Blitz 已到约 5 分钟上限，下次运行从缓存继续。")
                        break
                    for address, plain, excluded in parallel_map(both_job, batch):
                        if plain is None or excluded is None:
                            failed += 1
                            continue
                        plain_of[address.lower()] = plain
                        real_of[address.lower()] = excluded
                        if plain > RECONCILE_USDT and excluded <= RECONCILE_USDT:
                            consider_seed(address)
                        elif excluded > RECONCILE_USDT and address.lower() not in known:
                            leaderboard = _volume_of(volume_rows, address)
                            remember_wallet(address, "blitz_real", excluded, leaderboard, None)
                            real_sum += excluded
                            known.add(address.lower())
                    seed_sum = sum(
                        float(item["blitz"])
                        for item in wallets
                        if item["role"] == "seed" and item.get("blitz") is not None
                    )
                    real_sum = sum(
                        float(item["blitz"])
                        for item in wallets
                        if item["role"] == "blitz_real" and item.get("blitz") is not None
                    )
                    seed_short = blitz_seed is not None and blitz_seed - seed_sum > attribution_limit(blitz_seed)
                    real_short = (
                        blitz_real is not None
                        and race_rows is not None
                        and blitz_real - real_sum > attribution_limit(blitz_real)
                    )
                    if not seed_short and not real_short:
                        break

        for row in volume_rows:
            key = str(row.get("trader") or "").lower()
            if not key or key in known:
                continue
            plain = plain_of.get(key)
            if plain == 0:
                remember_wallet(str(row["trader"]), "trader", 0.0, float(row.get("volume") or 0), None)
                known.add(key)

        for meta in compiled.get("agents", {}).values():
            key = meta["address"].lower()
            if key in known:
                for item in wallets:
                    if item["address"].lower() == key:
                        item["role"] = "agent"
                        item["agent"] = (agent_rows.get(meta["id"]) or {}).get("volume")
                        if item.get("leaderboard") is not None and item["agent"] is not None and item["blitz"] is not None:
                            item["regular"] = item["leaderboard"] - item["blitz"] - item["agent"]
                continue
            blitz = plain_of.get(key)
            if blitz is None:
                blitz = real_of.get(key)
            leaderboard = _volume_of(volume_rows if scanned is not None else None, meta["address"])
            if leaderboard is None:
                leaderboard = _volume_of((doc.get("volume") or {}).get("rows") or [], meta["address"])
            agent_volume = (agent_rows.get(meta["id"]) or {}).get("volume")
            if blitz is None and not quiet:
                failed += 1
                remember_wallet(meta["address"], "agent", None, leaderboard, agent_volume)
            else:
                remember_wallet(meta["address"], "agent", float(blitz or 0), leaderboard, agent_volume)
            known.add(key)

        seed_sum = sum(
            float(item["blitz"]) for item in wallets if item["role"] == "seed" and item.get("blitz") is not None
        )
        real_sum = sum(
            float(item["blitz"]) for item in wallets if item["role"] == "blitz_real" and item.get("blitz") is not None
        )
        if blitz_real is not None and race_rows is not None:
            real_gap = blitz_real - real_sum
        if blitz_seed is not None:
            seed_gap = blitz_seed - seed_sum
        if race_rows is None:
            real_gap = None

    for meta_id, entry in agent_rows.items():
        if entry.get("match") is False:
            notes.append(
                f"{meta_id} 的挖矿成交量与排行榜成交量不一致"
                f"（挖矿 {entry.get('volume')}，排行榜 {entry.get('leaderboard_volume')}）。拆分使用挖矿差额。"
            )
    if real_gap is not None and abs(real_gap) > attribution_limit(blitz_real):
        notes.append(f"真实玩家 Blitz 还有 {real_gap:.2f} 没有落到具体钱包。")
    if seed_gap is not None and abs(seed_gap) > attribution_limit(blitz_seed):
        notes.append(f"种子 Blitz 还有 {seed_gap:.2f} 没有落到具体钱包。")
    if failed:
        notes.append(f"{failed} 个 Blitz 请求失败。失败的钱包不填 0。")

    agent_sum_value = agent_sum if agent_ok else None
    regular = None
    if total_value is not None and blitz_total is not None and agent_sum_value is not None:
        regular = total_value - blitz_total - agent_sum_value
    gaps_closed = (
        blitz_total is not None
        and failed == 0
        and real_gap is not None
        and seed_gap is not None
        and abs(real_gap) <= attribution_limit(blitz_real)
        and abs(seed_gap) <= attribution_limit(blitz_seed)
    )
    if quiet:
        gaps_closed = failed == 0 and blitz_seed is not None and blitz_real is not None
    residual = None
    if None not in (regular, blitz_real, blitz_seed, agent_sum_value, total_value):
        residual = regular + blitz_real + blitz_seed + agent_sum_value - total_value
    reconciled = bool(
        residual is not None
        and abs(residual) <= RECONCILE_USDT
        and regular is not None
        and regular >= -RECONCILE_USDT
        and gaps_closed
        and agent_ok
    )
    if blitz_total is None or blitz_real is None or blitz_seed is None:
        status = "unavailable" if blitz_total is None and blitz_real is None else "partial"
    elif not agent_ok or not gaps_closed or (regular is not None and regular < -RECONCILE_USDT):
        status = "partial"
    else:
        status = "ok"
    # Pre-points weeks are done once the four platform numbers add up. Per-wallet
    # columns stay empty on purpose.
    if (
        not per_wallet
        and blitz_total is not None
        and blitz_seed is not None
        and agent_ok
        and regular is not None
        and regular >= -RECONCILE_USDT
        and residual is not None
        and abs(residual) <= RECONCILE_USDT
    ):
        status = "ok"
        reconciled = True
        gaps_closed = False
    if regular is not None and regular < -RECONCILE_USDT:
        notes.append("普通交易差额为负：Blitz 与 Agent 之和大于本周排行榜总成交量。")
    agents_payload = {"sum": agent_sum_value}
    for key in ("codex", "claude", "grok"):
        agents_payload[key] = agent_rows.get(key)
    return {
        "status": status,
        "fetched_at": iso_now(),
        "formula": "regular = total - blitz_total - agents; blitz_seed = blitz_total - blitz_real",
        "total": total_value,
        "blitz_total": blitz_total,
        "blitz_real": blitz_real,
        "blitz_seed": blitz_seed,
        "agents": agents_payload,
        "regular": regular,
        "reconciled": reconciled,
        "residual": residual,
        "race_count": race_count,
        "seed_addresses": seed_addresses,
        "gaps_closed": gaps_closed,
        "note": " ".join(notes),
        "wallet_check": {
            "real_sum": real_sum if blitz_real is not None else None,
            "real_gap": real_gap,
            "seed_sum": seed_sum if blitz_seed is not None else None,
            "seed_gap": seed_gap,
            "failed": failed,
        },
        "sources": {
            "blitz": "GET /api/v1/token-races/volume?from&to，excludeSeed=true 为真实玩家",
            "leaderboard": "GET /api/v1/token-races/leaderboard",
            "agents": "GET /api/v1/mining/history 累计 volume 的窗口差额",
        },
        "wallets": wallets,
    }


def stamp_pool_fields(doc: dict, breakdown: dict) -> None:
    by = {str(item.get("address") or "").lower(): item for item in breakdown.get("wallets") or []}
    gaps_closed = bool(breakdown.get("gaps_closed")) and breakdown.get("status") in {"ok", "partial"}

    def apply(row: dict, address_field: str) -> None:
        key = str(row.get(address_field) or "").lower()
        item = by.get(key)
        if item and item.get("blitz") is not None:
            row["blitz_volume"] = item["blitz"]
            row["regular_volume"] = item.get("regular")
            row["pool_role"] = item.get("role")
            return
        if item and item.get("role") == "agent":
            row["blitz_volume"] = item.get("blitz")
            row["regular_volume"] = item.get("regular")
            row["pool_role"] = "agent"
            return
        if gaps_closed:
            row["blitz_volume"] = 0.0
            volume = row.get("volume")
            row["regular_volume"] = None if volume is None else float(volume)
            row["pool_role"] = "trader"
            return
        row["blitz_volume"] = None
        row["regular_volume"] = None
        row["pool_role"] = None

    for row in (doc.get("volume") or {}).get("rows") or []:
        apply(row, "trader")
    for row in (doc.get("points") or {}).get("rows") or []:
        apply(row, "address")


def apply_seed_aggregate(doc: dict, compiled: dict, seed_addresses: set[str], scanned: list[dict] | None) -> None:
    volume = doc.get("volume") or {}
    if scanned is not None:
        total = (volume.get("aggregate") or {}).get("total_traders") or volume.get("total_traders") or len(scanned)
        volume["aggregate"] = aggregate(scanned, "week", compiled, int(total), seed_addresses)
        volume["aggregate"]["pool_adjusted"] = True
        return
    current = volume.get("aggregate")
    if not current:
        return
    volume["aggregate"] = rebucket_aggregate(current, volume.get("rows") or [], compiled, "week", seed_addresses)


def retarget_breakdown(breakdown: dict, doc: dict) -> dict:
    """Keep a fresh Blitz/agent snapshot and recompute the regular residual."""
    updated = copy.deepcopy(breakdown)
    total = ((doc.get("volume") or {}).get("aggregate") or {}).get("total_volume")
    agents = (updated.get("agents") or {}).get("sum")
    blitz_total = updated.get("blitz_total")
    if total is None or agents is None or blitz_total is None:
        return updated
    total_value = float(total)
    updated["total"] = total_value
    updated["regular"] = total_value - float(blitz_total) - float(agents)
    by = {str(item.get("address") or "").lower(): item for item in updated.get("wallets") or []}
    for row in (doc.get("volume") or {}).get("rows") or []:
        item = by.get(str(row.get("trader") or "").lower())
        if not item or item.get("blitz") is None:
            continue
        item["leaderboard"] = row.get("volume")
        agent_volume = item.get("agent") or 0
        if item.get("role") == "agent" and item.get("agent") is None:
            item["regular"] = None
        else:
            item["regular"] = float(row.get("volume") or 0) - float(item["blitz"]) - float(agent_volume)
    updated["wallets"] = list(by.values())
    note = updated.get("note") or ""
    extra = "Blitz 与 Agent 沿用一小时内的结果；普通交易按最新总成交量重算。"
    if extra not in note:
        updated["note"] = (note + " " + extra).strip()
    residual = None
    if None not in (updated.get("regular"), updated.get("blitz_real"), updated.get("blitz_seed"), agents):
        residual = (
            float(updated["regular"])
            + float(updated["blitz_real"])
            + float(updated["blitz_seed"])
            + float(agents)
            - total_value
        )
    updated["residual"] = residual
    updated["reconciled"] = bool(
        residual is not None
        and abs(residual) <= RECONCILE_USDT
        and updated["regular"] >= -RECONCILE_USDT
        and updated.get("gaps_closed")
    )
    _relax_gaps(updated)
    return updated


def _relax_gaps(breakdown: dict) -> None:
    """A few dollars of unattributed Blitz is rounding, not a missing wallet."""
    check = breakdown.get("wallet_check") or {}
    real_gap = check.get("real_gap")
    seed_gap = check.get("seed_gap")
    if real_gap is None or seed_gap is None or check.get("failed"):
        return
    if abs(float(real_gap)) > attribution_limit(breakdown.get("blitz_real")):
        return
    if abs(float(seed_gap)) > attribution_limit(breakdown.get("blitz_seed")):
        return
    if not breakdown.get("wallets"):
        return
    breakdown["gaps_closed"] = True
    if breakdown.get("regular") is not None and float(breakdown["regular"]) >= -RECONCILE_USDT:
        breakdown["status"] = "ok"
        breakdown["reconciled"] = True


def _refresh_platform(
    previous: dict,
    doc: dict,
    compiled: dict,
    histories: dict[str, list[dict] | None],
    now: datetime,
) -> dict:
    """Refresh the open week's platform split and keep per-wallet rows."""
    updated = copy.deepcopy(previous)
    start = parse_iso(doc["start_at"])
    end = parse_iso(doc["end_exclusive"])
    total, real, races = fetch_blitz_totals(int(start.timestamp()), int(end.timestamp()), use_cache=False)
    if total is None or real is None or real > total + RECONCILE_USDT:
        return retarget_breakdown(previous, doc)
    agent_sum = 0.0
    agents = updated.setdefault("agents", {})
    for meta in compiled.get("agents", {}).values():
        delta, _detail = agent_window_volume(histories.get(str(meta["id"])), start, end, now)
        if delta is None:
            return retarget_breakdown(previous, doc)
        agent_sum += delta
        entry = agents.get(meta["id"])
        if isinstance(entry, dict):
            entry["volume"] = delta
    agents["sum"] = agent_sum
    leaderboard_total = ((doc.get("volume") or {}).get("aggregate") or {}).get("total_volume")
    updated["blitz_total"] = total
    updated["blitz_real"] = real
    updated["blitz_seed"] = total - real
    updated["race_count"] = races
    updated["fetched_at"] = iso_now()
    if leaderboard_total is not None:
        updated["total"] = float(leaderboard_total)
        updated["regular"] = float(leaderboard_total) - total - agent_sum
        updated["residual"] = (
            float(updated["regular"]) + float(updated["blitz_real"]) + float(updated["blitz_seed"]) + agent_sum
            - float(leaderboard_total)
        )
    _relax_gaps(updated)
    extra = "全平台拆分已更新；每钱包数字沿用已查到的结果。"
    note = updated.get("note") or ""
    if extra not in note:
        updated["note"] = (note + " " + extra).strip()
    return updated


def enrich_week_breakdown(
    doc: dict,
    compiled: dict,
    histories: dict[str, list[dict] | None],
    now: datetime,
    *,
    current: bool,
    budget: WalletBudget | None = None,
) -> dict:
    scanned = doc.pop("_scanned_rows", None)
    if current and scanned:
        doc["_open_volumes"] = {
            str(row.get("trader") or "").lower(): float(row.get("volume") or 0)
            for row in scanned
            if row.get("trader")
        }
    previous = doc.get("breakdown") if isinstance(doc.get("breakdown"), dict) else None
    # Week files written before the pre/points split still say kind=thursday.
    # Points weeks start at the launch date in wallet-labels.json.
    points_week = str(doc.get("start") or "") >= compiled["points_launch_from"][:10]
    kind = "pre" if not points_week else str(doc.get("kind") or "thursday")
    if not current and platform_split_cached(previous):
        breakdown = retarget_breakdown(previous, doc)
        print("  finished week split cached; skipping per-wallet", flush=True)
    elif breakdown_fresh(previous, now, current=current, kind=kind):
        breakdown = retarget_breakdown(previous, doc)
        print(f"  blitz cached fetched_at={breakdown.get('fetched_at')}", flush=True)
    elif (
        current
        and previous
        and previous.get("wallets")
        and breakdown_fresh(previous, now, current=False, kind=kind)
    ):
        # Wallet rows are already good. Refresh only the two platform totals.
        breakdown = _refresh_platform(previous, doc, compiled, histories, now)
        print("  blitz platform refreshed, wallets kept", flush=True)
    else:
        try:
            breakdown = build_week_breakdown(
                doc,
                compiled,
                histories,
                now,
                scanned,
                per_wallet=points_week,
                budget=budget,
            )
        except Exception as exc:
            print(f"  blitz failed: {exc}", flush=True)
            breakdown = _empty_breakdown("unavailable", "Blitz 成交量没有取到。")
        if breakdown.get("status") == "unavailable" and previous and previous.get("blitz_total") is not None:
            print("  blitz fetch failed, keeping previous breakdown", flush=True)
            breakdown = copy.deepcopy(previous)
            note = breakdown.get("note") or ""
            extra = "本次刷新失败，沿用上次结果。"
            if extra not in note:
                breakdown["note"] = (note + " " + extra).strip()
    seed = {str(addr).lower() for addr in breakdown.get("seed_addresses") or []}
    apply_seed_aggregate(doc, compiled, seed, scanned)
    # Retarget once more so regular uses the post-classification total (unchanged)
    # and wallet leaderboard volumes from the rows we just kept.
    if breakdown.get("blitz_total") is not None:
        breakdown["total"] = ((doc.get("volume") or {}).get("aggregate") or {}).get("total_volume")
        agents = (breakdown.get("agents") or {}).get("sum")
        if breakdown["total"] is not None and agents is not None:
            breakdown["regular"] = float(breakdown["total"]) - float(breakdown["blitz_total"]) - float(agents)
            if None not in (breakdown.get("blitz_real"), breakdown.get("blitz_seed")):
                breakdown["residual"] = (
                    float(breakdown["regular"])
                    + float(breakdown["blitz_real"])
                    + float(breakdown["blitz_seed"])
                    + float(agents)
                    - float(breakdown["total"])
                )
                # The four platform numbers are reconciled when they add back to
                # the leaderboard total. Wallet gaps are reported separately.
                breakdown["reconciled"] = bool(
                    abs(breakdown["residual"]) <= RECONCILE_USDT
                    and breakdown["regular"] >= -RECONCILE_USDT
                    and breakdown.get("status") != "unavailable"
                )
    doc["breakdown"] = breakdown
    stamp_pool_fields(doc, breakdown)
    agg = (doc.get("volume") or {}).get("aggregate") or {}
    print(
        f"  breakdown status={breakdown.get('status')} reconciled={breakdown.get('reconciled')} "
        f"total={breakdown.get('total')} regular={breakdown.get('regular')} "
        f"blitz_real={breakdown.get('blitz_real')} blitz_seed={breakdown.get('blitz_seed')} "
        f"agents={(breakdown.get('agents') or {}).get('sum')} "
        f"mm={agg.get('mm', {}).get('volume')} retail={agg.get('retail', {}).get('volume')}",
        flush=True,
    )
    if breakdown.get("note"):
        print(f"  note {breakdown['note']}", flush=True)
    return doc


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value, default: int = 0) -> int:
    number = _as_float(value)
    if number is None:
        return default
    return int(number)


def _blitz_player_from_api(row: dict) -> dict | None:
    address = str(row.get("walletAddress") or "").strip()
    if not address:
        return None
    return {
        "address": address,
        "rank": _as_int(row.get("rank"), 0) or None,
        "pnl": _as_float(row.get("racePnl")),
        "races": _as_int(row.get("racesPlayed")),
        "wins": _as_int(row.get("wins")),
        "losses": _as_int(row.get("losses")),
        "win_rate": _as_float(row.get("winRate")),
        "best_streak": _as_int(row.get("bestStreak")),
        "longest_loss_streak": _as_int(row.get("longestLossStreak")),
        "volume": None,
    }


def fetch_blitz_player_rows(day_from: str, day_to: str) -> tuple[list[dict] | None, int | None]:
    """Full Blitz leaderboard for an inclusive UTC date window, sorted by PnL.

    limit=500, so a week is one request until the board passes 500 players,
    then a second page. Returns (rows, total) or (None, None) on failure.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    pages = 0
    total: int | None = None
    while True:
        url = (
            f"{API_BASE}/api/v1/token-races/leaderboard"
            f"?from={day_from}&to={day_to}&sort_by=pnl&limit={BLITZ_PLAYER_PAGE}"
            f"&offset={offset}&include_total=1"
        )
        payload = fetch_ok_soft(url)
        if payload is None:
            return None, None
        chunk = payload.get("data")
        if not isinstance(chunk, list):
            return None, None
        pages += 1
        pagination = payload.get("pagination") or {}
        if pagination.get("totalResults") is not None:
            parsed = _as_int(pagination.get("totalResults"), -1)
            if parsed >= 0:
                total = parsed
        for item in chunk:
            if not isinstance(item, dict):
                continue
            player = _blitz_player_from_api(item)
            if player is None:
                continue
            key = player["address"].lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(player)
        print(
            f"    blitz players {day_from}..{day_to} page {pages} "
            f"got={len(chunk)} unique={len(rows)} total={total}",
            flush=True,
        )
        if not chunk or not pagination.get("hasMore"):
            break
        offset += len(chunk)
        if pages >= 6:
            print("    blitz players pagination stopped", flush=True)
            return None, None
        time.sleep(REQUEST_PAUSE_S)
    return rows, total if total is not None else len(rows)


def _pick_cached_volume(
    previous: dict | None,
    wallet_volume: float | None,
    wallet_at: str | None,
) -> tuple[float | None, str | None]:
    """Prefer the newer of the stored player volume and the breakdown figure."""
    prev_vol = _as_float((previous or {}).get("volume")) if previous else None
    prev_at = str(previous.get("volume_at")) if previous and previous.get("volume_at") else None
    if prev_vol is not None and wallet_volume is not None:
        if prev_at and wallet_at and wallet_at > prev_at:
            return wallet_volume, wallet_at
        return prev_vol, prev_at
    if prev_vol is not None:
        return prev_vol, prev_at
    if wallet_volume is not None:
        return wallet_volume, wallet_at
    return None, None


def blitz_volume_refresh_kind(
    *,
    current: bool,
    missing: bool,
    prev_races: int | None,
    new_races: int,
    prev_total: float | None,
    new_total: float | None,
    priority_rank: int,
    volume_stale: bool,
) -> str | None:
    """Why this wallet's open-week userVolume should be fetched.

    Missing volumes come first. Then a wallet whose racesPlayed or leaderboard
    total moved since the stored figure. Then the small stale leader slice.
    Finished weeks only fill gaps.
    """
    if missing:
        return "missing"
    if not current:
        return None
    races_changed = prev_races is not None and int(prev_races) != int(new_races or 0)
    total_changed = (
        prev_total is not None
        and new_total is not None
        and abs(float(prev_total) - float(new_total)) > VOLUME_CHANGE_EPS
    )
    if races_changed or total_changed:
        return "changed"
    if volume_stale and priority_rank <= BLITZ_PLAYER_REFRESH_TOP:
        return "stale"
    return None


def blitz_change_delta(
    kind: str | None,
    prev_races: int | None,
    new_races: int,
    prev_total: float | None,
    new_total: float | None,
) -> float:
    """Larger moves are fetched first when the 3-minute budget runs out."""
    if kind != "changed":
        return 0.0
    if (
        prev_total is not None
        and new_total is not None
        and abs(float(prev_total) - float(new_total)) > VOLUME_CHANGE_EPS
    ):
        return abs(float(new_total) - float(prev_total))
    if prev_races is not None:
        return float(abs(int(new_races or 0) - int(prev_races)))
    return 0.0


def _apply_refreshed_blitz_volumes(doc: dict, players: list[dict]) -> bool:
    """Copy freshly read userVolume onto breakdown wallets.

    The volume table reads breakdown.wallets, not blitzPlayers. Returns True
    when a stored wallet figure changed.
    """
    breakdown = doc.get("breakdown")
    if not isinstance(breakdown, dict):
        return False
    by = {
        str(item.get("address") or "").lower(): item
        for item in (breakdown.get("wallets") or [])
        if isinstance(item, dict)
    }
    dirty = False
    for player in players:
        if player.get("flag") == "seed" or player.get("volume") is None:
            continue
        key = str(player.get("address") or "").lower()
        item = by.get(key)
        if not item or item.get("role") == "seed":
            continue
        vol = float(player["volume"])
        lb = _as_float(player.get("leaderboard"))
        prev_blitz = _as_float(item.get("blitz"))
        prev_lb = _as_float(item.get("leaderboard"))
        blitz_moved = prev_blitz is None or abs(prev_blitz - vol) > 1e-9
        lb_moved = lb is not None and (prev_lb is None or abs(prev_lb - float(lb)) > VOLUME_CHANGE_EPS)
        if not blitz_moved and not lb_moved:
            continue
        item["blitz"] = vol
        if lb is not None:
            item["leaderboard"] = lb
            agent = _as_float(item.get("agent")) or 0.0
            if item.get("role") == "agent":
                item["regular"] = float(lb) - vol - agent
            else:
                item["regular"] = float(lb) - vol
        if player.get("races") is not None:
            item["races"] = player.get("races")
        dirty = True
    if dirty:
        note = breakdown.get("note") or ""
        extra = "成交量或轮次有变化的钱包已重查 Blitz 成交量。"
        kept = "全平台拆分已更新；每钱包数字沿用已查到的结果。"
        if kept in note:
            note = note.replace(kept, "全平台拆分已更新。")
        if extra not in note:
            breakdown["note"] = (note + " " + extra).strip()
    return dirty


def _blitz_players_same(left: dict | None, right: dict | None) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("from", "to", "total", "status", "volume_missing"):
        if left.get(key) != right.get(key):
            return False
    old_rows = left.get("players") or []
    new_rows = right.get("players") or []
    if len(old_rows) != len(new_rows):
        return False
    fields = (
        "address",
        "rank",
        "pnl",
        "races",
        "wins",
        "losses",
        "win_rate",
        "best_streak",
        "longest_loss_streak",
        "volume",
        "volume_at",
        "leaderboard",
        "flag",
    )
    for old, new in zip(old_rows, new_rows):
        for field in fields:
            if old.get(field) != new.get(field):
                return False
    return True


def _volume_is_stale(volume_at: str | None, now: datetime) -> bool:
    if not volume_at:
        return True
    try:
        fetched = parse_iso(volume_at)
    except ValueError:
        return True
    return now - fetched >= BLITZ_REFRESH


def attach_blitz_players(
    documents: list[dict],
    compiled: dict,
    now: datetime,
    current_start: datetime,
) -> dict:
    """Store each points week's Blitz leaderboard on the week file.

    userVolume is filled from the week file and from breakdown wallets first.
    Anything still missing is fetched first. On the open week, wallets whose
    racesPlayed or leaderboard total changed since that stored figure are
    next, then a small stale slice of the leaders. All of that stays inside
    BLITZ_PLAYER_BUDGET_S. Refreshed figures are copied onto breakdown
    wallets so the volume table's Blitz column matches. Seeds are flagged
    and stored as 0 because excludeSeed strips them. Missing volumes stay null.
    """
    started = time.monotonic()
    budget = WalletBudget(BLITZ_PLAYER_BUDGET_S)
    current_key = current_start.date().isoformat()
    launch_day = compiled["points_launch_from"][:10]
    index_prev = load_week_file(WEEKS_DIR / "index.json") or {}
    seeds = {str(addr).lower() for addr in (index_prev.get("blitzSeedWallets") or []) if addr}
    agents = {str(addr).lower() for addr in (compiled.get("agents") or {})}
    points_docs: list[dict] = []
    for doc in documents:
        if doc.get("group") != "points" and str(doc.get("start") or "") < launch_day:
            continue
        if str(doc.get("start") or "") < launch_day:
            continue
        points_docs.append(doc)
        for addr in (doc.get("breakdown") or {}).get("seed_addresses") or []:
            if addr:
                seeds.add(str(addr).lower())
    points_docs.sort(key=lambda doc: doc.get("start") or "", reverse=True)

    prepared: list[dict] = []
    jobs: list[dict] = []
    for doc in points_docs:
        start = parse_iso(doc["start_at"])
        end = parse_iso(doc["end_exclusive"])
        day_from, day_to = week_dates(start, end)
        current = bool(doc.get("current")) or doc.get("start") == current_key
        open_volumes = doc.pop("_open_volumes", None) or {}
        if not isinstance(open_volumes, dict):
            open_volumes = {}
        print(f"  blitz players {doc.get('start')} {day_from}..{day_to}", flush=True)
        rows, total = fetch_blitz_player_rows(day_from, day_to)
        previous = doc.get("blitzPlayers") if isinstance(doc.get("blitzPlayers"), dict) else None
        prev_by = {
            str(item.get("address") or "").lower(): item
            for item in ((previous or {}).get("players") or [])
            if isinstance(item, dict)
        }
        if rows is None:
            if previous:
                kept = copy.deepcopy(previous)
                extra = "本次竞速榜没有取到，沿用上次玩家表。"
                note = kept.get("note") or ""
                if extra not in note:
                    kept["note"] = (note + " " + extra).strip()
                prepared.append({"doc": doc, "payload": kept, "failed": True})
            else:
                prepared.append(
                    {
                        "doc": doc,
                        "failed": True,
                        "payload": {
                            "status": "unavailable",
                            "fetched_at": iso_now(),
                            "from": day_from,
                            "to": day_to,
                            "sort_by": "pnl",
                            "total": None,
                            "volume_missing": None,
                            "players": [],
                            "note": "竞速榜没有取到。",
                        },
                    }
                )
            continue

        breakdown = doc.get("breakdown") or {}
        breakdown_at = str(breakdown.get("fetched_at") or "") or None
        wallet_vol: dict[str, float] = {}
        wallet_lb: dict[str, float] = {}
        for item in breakdown.get("wallets") or []:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or "")
            if not address:
                continue
            leaderboard_total = _as_float(item.get("leaderboard"))
            if leaderboard_total is not None:
                wallet_lb[address.lower()] = leaderboard_total
            if item.get("role") != "blitz_real":
                continue
            amount = _as_float(item.get("blitz"))
            if amount is None:
                continue
            wallet_vol[address.lower()] = amount

        players: list[dict] = []
        for row in rows:
            player = dict(row)
            key = player["address"].lower()
            if key in agents:
                player["flag"] = "agent"
            elif key in seeds:
                player["flag"] = "seed"
            prev = prev_by.get(key)
            if player.get("flag") == "seed":
                player["volume"] = 0.0
                if prev and prev.get("volume_at"):
                    player["volume_at"] = prev.get("volume_at")
                elif breakdown_at:
                    player["volume_at"] = breakdown_at
            else:
                volume, volume_at = _pick_cached_volume(prev, wallet_vol.get(key), breakdown_at)
                if volume is None and not current:
                    cached = _cache_get(blitz_cache_key(int(start.timestamp()), int(end.timestamp()), player["address"], True))
                    if cached is not None:
                        volume = cached
                        volume_at = volume_at or iso_now()
                player["volume"] = volume
                if volume is not None and volume_at:
                    player["volume_at"] = volume_at
                if prev and prev.get("leaderboard") is not None:
                    player["leaderboard"] = prev.get("leaderboard")
                elif volume is not None and key in wallet_lb:
                    player["leaderboard"] = wallet_lb[key]
            players.append(player)

        races_order = sorted(
            range(len(players)),
            key=lambda index: (-(players[index]["races"] or 0), -(players[index]["pnl"] or 0), players[index]["address"].lower()),
        )
        races_rank = {index: pos for pos, index in enumerate(races_order, start=1)}
        frm = int(start.timestamp())
        to = int(end.timestamp())
        scope = "" if not current else now.strftime("%Y%m%d%H")
        start_ordinal = start.date().toordinal()
        for index, player in enumerate(players):
            if player.get("flag") == "seed":
                continue
            key = player["address"].lower()
            pnl_rank = player.get("rank") or 10**9
            priority_rank = min(int(pnl_rank), races_rank[index])
            missing = player.get("volume") is None
            prev = prev_by.get(key)
            prev_races = None if not prev or prev.get("races") is None else int(prev.get("races") or 0)
            prev_total = _as_float(player.get("leaderboard"))
            new_total = _as_float(open_volumes.get(key)) if current else None
            kind = blitz_volume_refresh_kind(
                current=current,
                missing=missing,
                prev_races=prev_races,
                new_races=int(player.get("races") or 0),
                prev_total=prev_total,
                new_total=new_total,
                priority_rank=priority_rank,
                volume_stale=_volume_is_stale(player.get("volume_at"), now),
            )
            if kind is None:
                # Remember the total we just saw so the next run can tell if it moved.
                if (
                    current
                    and new_total is not None
                    and (
                        player.get("leaderboard") is None
                        or abs(float(player["leaderboard"]) - new_total) > VOLUME_CHANGE_EPS
                    )
                ):
                    player["leaderboard"] = new_total
                continue
            delta = blitz_change_delta(
                kind,
                prev_races,
                int(player.get("races") or 0),
                prev_total,
                new_total,
            )
            jobs.append(
                {
                    "sort": (
                        0 if kind == "missing" else 1 if kind == "changed" else 2,
                        0 if current else 1,
                        -start_ordinal,
                        -delta,
                        priority_rank,
                    ),
                    "player": player,
                    "frm": frm,
                    "to": to,
                    "scope": scope,
                    "address": player["address"],
                    "refresh": kind in {"changed", "stale"},
                    "new_total": new_total,
                    "kind": kind,
                }
            )
        prepared.append(
            {
                "doc": doc,
                "failed": False,
                "players": players,
                "from": day_from,
                "to": day_to,
                "total": total if total is not None else len(players),
                "current": current,
            }
        )

    jobs.sort(key=lambda job: job["sort"])
    calls = 0

    def volume_job(job: dict) -> bool:
        value = fetch_blitz_user(
            job["frm"],
            job["to"],
            job["address"],
            True,
            use_cache=True,
            cache_scope=job["scope"],
            refresh=bool(job.get("refresh")),
        )
        if value is None:
            return False
        job["player"]["volume"] = value
        job["player"]["volume_at"] = iso_now()
        if job.get("new_total") is not None:
            job["player"]["leaderboard"] = job["new_total"]
        return True

    if jobs:
        changed = sum(1 for job in jobs if job.get("kind") == "changed")
        print(
            f"  blitz player volumes queued={len(jobs)} changed={changed} "
            f"budget={BLITZ_PLAYER_BUDGET_S}s",
            flush=True,
        )
        done = parallel_map(volume_job, jobs, budget=budget)
        calls = len(done)
    elapsed = time.monotonic() - started
    missing_total = 0
    written = 0
    budget_note = "本轮交易量查询已到约 3 分钟上限，没查到的下次继续。已结束的周会沿用已经写入的交易量。"
    base_note = (
        "盈亏是已结算、含手续费的 racePnl。"
        "交易量是该周 /token-races/volume 的 userVolume（excludeSeed=true，周四 00:00 UTC 到下周四 00:00 UTC，不含结束时刻）；"
        "没查到的留空，页面显示「—」。"
        "轮次是 racesPlayed（参与轮数），不是成交笔数。"
        "种子钱包和 3 个 Agent 即使出现在榜上也会标出；种子的 userVolume 为 0。"
    )
    for item in prepared:
        doc = item["doc"]
        if item.get("failed"):
            payload = item["payload"]
        else:
            players = item["players"]
            missing = sum(1 for player in players if player.get("volume") is None)
            missing_total += missing
            notes = [base_note]
            if missing and budget.exhausted:
                notes.append(budget_note)
            payload = {
                "status": "partial" if missing else "ok",
                "fetched_at": iso_now(),
                "from": item["from"],
                "to": item["to"],
                "sort_by": "pnl",
                "total": item["total"],
                "volume_missing": missing,
                "players": players,
                "note": " ".join(notes),
            }
        path = WEEKS_DIR / f"{doc['start']}.json"
        on_disk = load_week_file(path)
        if not on_disk:
            print(f"  blitz players skip missing file {path.name}", flush=True)
            continue
        previous = on_disk.get("blitzPlayers") if isinstance(on_disk.get("blitzPlayers"), dict) else None
        breakdown_changed = False
        if not item.get("failed") and item.get("current"):
            breakdown_changed = _apply_refreshed_blitz_volumes(doc, item.get("players") or [])
            if breakdown_changed:
                on_disk["breakdown"] = doc.get("breakdown")
                stamp_pool_fields(on_disk, on_disk["breakdown"])
        if _blitz_players_same(previous, payload) and not breakdown_changed:
            print(f"  blitz players {doc.get('start')} unchanged", flush=True)
            continue
        on_disk["blitzPlayers"] = payload
        write_json(path, on_disk)
        doc["blitzPlayers"] = payload
        written += 1
        print(
            f"  blitz players {doc.get('start')} status={payload.get('status')} "
            f"rows={len(payload.get('players') or [])} missing={payload.get('volume_missing')}",
            flush=True,
        )
    print(
        f"blitz players added_runtime={elapsed:.1f}s budget={BLITZ_PLAYER_BUDGET_S}s "
        f"volume_calls={calls} files={written} volume_missing={missing_total}",
        flush=True,
    )
    return {
        "elapsed_s": round(elapsed, 1),
        "budget_s": BLITZ_PLAYER_BUDGET_S,
        "volume_calls": calls,
        "files": written,
        "volume_missing": missing_total,
    }


def refresh_blitz_players_only() -> int:
    """Refresh blitzPlayers on points weeks already stored under data/weeks/."""
    compiled = compile_labels(load_labels())
    now = datetime.now(timezone.utc)
    current_start, _current_end = current_points_week(now, compiled["points_week_start"])
    index = load_week_file(WEEKS_DIR / "index.json") or {}
    documents: list[dict] = []
    for entry in index.get("weeks") or []:
        if entry.get("group") != "points":
            continue
        doc = load_week_file(WEEKS_DIR / f"{entry['start']}.json")
        if not doc:
            print(f"missing week file {entry['start']}", flush=True)
            continue
        doc["group"] = "points"
        doc["current"] = bool(entry.get("current")) or doc.get("start") == current_start.date().isoformat()
        documents.append(doc)
    load_blitz_cache()
    attach_blitz_players(documents, compiled, now, current_start)
    flush_blitz_cache()
    return 0


def attach_cumulative_breakdown(doc: dict, compiled: dict, now: datetime) -> dict:
    """Sum per-week Blitz and agent volume onto the all-time board.

    The 2026-10-29 week and week 1 both contain 2026-11-04. That day's Blitz
    is subtracted once so the total is not double-counted. The regular slice
    is all-time leaderboard volume minus that Blitz total minus agent volume.
    """
    index = load_week_file(WEEKS_DIR / "index.json") or {}
    weeks = []
    missing = []
    for entry in index.get("weeks") or []:
        loaded = load_week_file(WEEKS_DIR / f"{entry['start']}.json")
        if not loaded:
            missing.append(entry["start"])
            continue
        weeks.append(loaded)
    existing = load_week_file(WEEKS_DIR / "cumulative.json") or {}
    previous = existing.get("breakdown") if isinstance(existing.get("breakdown"), dict) else None
    usable = []
    for week in weeks:
        breakdown = week.get("breakdown") or {}
        agents = (breakdown.get("agents") or {}).get("sum")
        if breakdown.get("status") in {"ok", "partial"} and breakdown.get("blitz_total") is not None and agents is not None:
            usable.append(week)
        else:
            missing.append(week.get("start"))
    if missing:
        note = "这些周没有可用的拆分，总榜不把缺的周当成 0：" + "、".join(str(item) for item in missing)
        if previous and previous.get("blitz_total") is not None and breakdown_fresh(previous, now, current=True):
            doc["breakdown"] = previous
            stamp_pool_fields(doc, previous)
            return doc
        empty = _empty_breakdown("unavailable", note)
        doc["breakdown"] = empty
        stamp_pool_fields(doc, empty)
        return doc

    blitz_total = sum(float(week["breakdown"]["blitz_total"]) for week in usable)
    blitz_real = sum(float(week["breakdown"]["blitz_real"] or 0) for week in usable)
    blitz_seed = sum(float(week["breakdown"]["blitz_seed"] or 0) for week in usable)
    agent_sum = sum(float(week["breakdown"]["agents"]["sum"]) for week in usable)
    per_agent = {key: 0.0 for key in ("codex", "claude", "grok")}
    agent_meta = {key: None for key in ("codex", "claude", "grok")}
    for week in usable:
        for key in per_agent:
            entry = (week["breakdown"]["agents"] or {}).get(key)
            if isinstance(entry, dict) and entry.get("volume") is not None:
                per_agent[key] += float(entry["volume"])
                agent_meta[key] = entry.get("address")

    launch = parse_iso(compiled["points_launch_from"])
    overlap = None
    for week in usable:
        start = parse_iso(week["start_at"])
        end = parse_iso(week["end_exclusive"])
        if start < launch < end:
            overlap = (launch, end if end < parse_iso(compiled["points_launch_to"]) else launch + (end - launch))
            # The overlapping pre-week runs through its own end. Week 1 starts at launch.
            overlap = (launch, end)
            break
    overlap_block = None
    if overlap:
        prev_overlap = (previous or {}).get("overlap") or {}
        frm = int(overlap[0].timestamp())
        to = int(overlap[1].timestamp())
        if prev_overlap.get("from") == rfc3339(overlap[0]) and prev_overlap.get("blitz_total") is not None:
            overlap_block = prev_overlap
        else:
            total, real, races = fetch_blitz_totals(frm, to, use_cache=True)
            if total is None or real is None:
                empty = _empty_breakdown(
                    "unavailable",
                    "2026-11-04 同时落在积分前一周和第1周。这一天的 Blitz 没有取到，总榜不使用会重复计算的合计。",
                )
                doc["breakdown"] = empty
                stamp_pool_fields(doc, empty)
                return doc
            overlap_block = {
                "from": rfc3339(overlap[0]),
                "to": rfc3339(overlap[1]),
                "blitz_total": total,
                "blitz_real": real,
                "blitz_seed": total - real if real <= total + RECONCILE_USDT else None,
                "race_count": races,
                "wallets": dict(prev_overlap.get("wallets") or {}),
            }
        if overlap_block.get("blitz_seed") is None:
            empty = _empty_breakdown("unavailable", "重叠日的种子成交量对不上，总榜拆分暂无数据。")
            doc["breakdown"] = empty
            stamp_pool_fields(doc, empty)
            return doc
        blitz_total -= float(overlap_block["blitz_total"])
        blitz_real -= float(overlap_block["blitz_real"])
        blitz_seed -= float(overlap_block["blitz_seed"])

    sums: dict[str, dict] = {}
    for week in usable:
        for item in week["breakdown"].get("wallets") or []:
            key = str(item.get("address") or "").lower()
            if not key:
                continue
            slot = sums.setdefault(
                key,
                {
                    "address": item["address"],
                    "blitz": 0.0,
                    "agent": 0.0,
                    "role": "trader",
                    "missing": False,
                    "parts": {},
                },
            )
            if item.get("blitz") is None:
                slot["missing"] = True
            else:
                slot["blitz"] += float(item["blitz"])
                slot["parts"][week["start"]] = float(item["blitz"])
            if item.get("agent") is not None:
                slot["agent"] += float(item["agent"])
            if item.get("role") == "seed":
                slot["role"] = "seed"
            elif item.get("role") == "agent":
                slot["role"] = "agent"
            elif item.get("role") == "blitz_real" and slot["role"] == "trader":
                slot["role"] = "blitz_real"

    if overlap and overlap_block is not None:
        pre = next((week for week in usable if parse_iso(week["start_at"]) < overlap[0] < parse_iso(week["end_exclusive"])), None)
        launch_week = next((week for week in usable if week.get("kind") == "launch" or week.get("start") == overlap[0].date().isoformat()), None)
        both = []
        if pre and launch_week:
            pre_map = {
                str(item.get("address") or "").lower(): float(item.get("blitz") or 0)
                for item in pre["breakdown"].get("wallets") or []
                if item.get("blitz")
            }
            launch_map = {
                str(item.get("address") or "").lower(): item
                for item in launch_week["breakdown"].get("wallets") or []
            }
            for key, pre_blitz in pre_map.items():
                other = launch_map.get(key)
                if pre_blitz > 0 and other and float(other.get("blitz") or 0) > 0:
                    both.append(sums[key]["address"] if key in sums else key)
        cached = overlap_block.setdefault("wallets", {})
        frm = int(overlap[0].timestamp())
        to = int(overlap[1].timestamp())
        need = [addr for addr in both if addr.lower() not in cached]

        def overlap_job(address: str):
            return address, fetch_blitz_user(frm, to, address, False, use_cache=True)

        failed = 0
        for address, value in parallel_map(overlap_job, need):
            if value is None:
                failed += 1
                continue
            cached[address.lower()] = value
        for address in both:
            key = address.lower()
            if key not in cached:
                if key in sums:
                    sums[key]["missing"] = True
                continue
            if key in sums:
                sums[key]["blitz"] -= float(cached[key])
        overlap_block["wallet_failed"] = failed

    total = ((doc.get("volume") or {}).get("aggregate") or {}).get("total_volume")
    regular = None if total is None else float(total) - blitz_total - agent_sum
    residual = None
    if regular is not None:
        residual = regular + blitz_real + blitz_seed + agent_sum - float(total)
    seed_addresses = sorted({addr for week in usable for addr in (week["breakdown"].get("seed_addresses") or [])}, key=str.lower)
    wallets = []
    for slot in sums.values():
        blitz = None if slot["missing"] else slot["blitz"]
        wallets.append(
            {
                "address": slot["address"],
                "blitz": blitz,
                "regular": None,
                "leaderboard": None,
                "agent": slot["agent"] if slot["role"] == "agent" else None,
                "role": slot["role"],
            }
        )
    by_wallet = {item["address"].lower(): item for item in wallets}
    for row in (doc.get("volume") or {}).get("rows") or []:
        item = by_wallet.get(str(row.get("trader") or "").lower())
        if not item:
            continue
        item["leaderboard"] = row.get("volume")
        if item.get("blitz") is None:
            item["regular"] = None
        elif item.get("role") == "agent":
            item["regular"] = float(row.get("volume") or 0) - float(item["blitz"]) - float(item.get("agent") or 0)
        else:
            item["regular"] = float(row.get("volume") or 0) - float(item["blitz"])
    agents_payload = {"sum": agent_sum}
    for key in ("codex", "claude", "grok"):
        agents_payload[key] = {"address": agent_meta[key], "volume": per_agent[key], "label": key}
    note = (
        "总榜的 Blitz 与 Agent 是各周相加。"
        "2026-11-04 同时落在积分前的 2026-10-29 那一周和第1周，已按这一天的 Blitz 接口扣除，避免算两次。"
        "普通交易 = 全部时间排行榜总成交量 − Blitz − Agent。"
    )
    if overlap_block and overlap_block.get("wallet_failed"):
        note += " 有钱包没取到重叠日的成交，这些钱包的总榜 Blitz 留空。"
    reconciled = bool(
        residual is not None
        and abs(residual) <= RECONCILE_USDT
        and regular is not None
        and regular >= -RECONCILE_USDT
    )
    if regular is not None and regular < -RECONCILE_USDT:
        note += " 普通交易差额为负，拆分和全部时间总成交量对不上。"
    breakdown = {
        "status": "ok" if reconciled else "partial",
        "fetched_at": iso_now(),
        "formula": "regular = all-time total - sum(week blitz) + overlap-day blitz - sum(week agents)",
        "total": None if total is None else float(total),
        "blitz_total": blitz_total,
        "blitz_real": blitz_real,
        "blitz_seed": blitz_seed,
        "agents": agents_payload,
        "regular": regular,
        "reconciled": reconciled,
        "residual": residual,
        "seed_addresses": seed_addresses,
        "gaps_closed": all(not slot["missing"] for slot in sums.values()),
        "note": note,
        "overlap": overlap_block,
        "wallet_check": {
            "weeks": len(usable),
            "failed": int((overlap_block or {}).get("wallet_failed") or 0),
        },
        "sources": {
            "blitz": "各周 /token-races/volume 相加，再减去重叠日",
            "agents": "各周 /mining/history 差额相加",
        },
        "wallets": wallets,
    }
    doc["breakdown"] = breakdown
    stamp_pool_fields(doc, breakdown)
    print(
        f"  cumulative breakdown reconciled={reconciled} total={breakdown['total']} "
        f"regular={regular} blitz_real={blitz_real} blitz_seed={blitz_seed} agents={agent_sum}",
        flush=True,
    )
    return doc

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
    held_rows: dict[str, tuple[list[dict], int]] = {}
    all_rows: list[dict] | None = None
    all_total = 0
    for index, period in enumerate(PERIODS):
        if index:
            time.sleep(REQUEST_PAUSE_S)
        print(f"fetching period={period} ...", flush=True)
        rows, total = fetch_leaderboard(f"?period={period}&")
        held_rows[period] = (rows, total)
        if period == "all":
            all_rows = rows
            all_total = total
        periods[period] = window_block(rows, period, compiled, total)
        print_summary(period, periods[period])

    print("fetching points week ...", flush=True)
    week_rows, week_total = fetch_leaderboard(f"?from={week_from}&to={week_to_date}&")
    held_rows["week"] = (week_rows, week_total)
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

    week_index = update_week_history(
        compiled,
        now,
        week_rows,
        week_total,
        week_start,
        week_end,
    )
    seed_addresses = {str(addr).lower() for addr in (week_index.get("blitzSeedWallets") or [])}
    for period, (rows, total) in held_rows.items():
        extra = {
            key: value
            for key, value in periods[period].items()
            if key not in {"period", "total_traders", "returned", "rows", "aggregate"}
        }
        periods[period] = window_block(
            rows,
            "week" if period == "week" else period,
            compiled,
            total,
            extra=extra or None,
            seed_addresses=seed_addresses,
        )
        print_summary(period + "+pools", periods[period])
    doc["periods"] = periods
    write_json(OUT_PATH, doc)
    print(f"rewrote {OUT_PATH} with agent and seed excluded from retail and MM", flush=True)
    build_cumulative(compiled, now, all_rows, all_total, seed_addresses)
    return 0


if __name__ == "__main__":
    if "--blitz-players" in sys.argv:
        sys.exit(refresh_blitz_players_only())
    sys.exit(main())
