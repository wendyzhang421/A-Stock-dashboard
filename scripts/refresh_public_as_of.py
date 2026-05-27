#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as dashboard


def market_prefix(code: str) -> str:
    raw = code.split(".")[0]
    return "sh" if raw.startswith(("5", "6", "9")) else "sz"


def latest_watchlist_before(as_of: date) -> Path:
    candidates = sorted(
        path for path in (ROOT / "reports").glob("watchlist_dashboard_*.json")
        if path.name != f"watchlist_dashboard_{as_of.isoformat()}.json"
    )
    if not candidates:
        raise FileNotFoundError("No prior watchlist dashboard json found")
    dated: list[tuple[date, Path]] = []
    for path in candidates:
        try:
            path_date = date.fromisoformat(path.stem.removeprefix("watchlist_dashboard_"))
        except ValueError:
            continue
        if path_date < as_of:
            dated.append((path_date, path))
    if not dated:
        raise FileNotFoundError(f"No prior watchlist dashboard json before {as_of.isoformat()}")
    return dated[-1][1]


def update_row_from_public(row: dict, as_of: date) -> dict:
    return dashboard.update_row_from_public_kline(row, as_of)


def latest_strong_before(as_of: date) -> Path:
    candidates = sorted(
        path for path in (ROOT / "reports").glob("watchlist_strong_stocks_*.json")
        if path.name != f"watchlist_strong_stocks_{as_of.isoformat()}.json"
    )
    if not candidates:
        raise FileNotFoundError("No prior strong-stock json found")
    dated: list[tuple[date, Path]] = []
    for path in candidates:
        try:
            path_date = date.fromisoformat(path.stem.removeprefix("watchlist_strong_stocks_"))
        except ValueError:
            continue
        if path_date < as_of:
            dated.append((path_date, path))
    if not dated:
        raise FileNotFoundError(f"No prior strong-stock json before {as_of.isoformat()}")
    return dated[-1][1]


def build_strong_rows(watch_map: dict[str, dict], as_of: date) -> list[dict]:
    strong_rows: list[dict] = []
    try:
        _, candidates = dashboard.resolve_strong_stock_candidates(as_of)
    except Exception:
        candidates = []
    for candidate in candidates:
        code = candidate["code"]
        base_row = dict(watch_map[code]) if code in watch_map else {
            "code": code,
            "name": candidate["name"],
            "mainBusiness": dashboard.MAIN_BUSINESS_MAP.get(code, "-"),
            "industry": "",
            "research": dashboard.build_research_payload(code, {}),
            "marginFinancing": {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None},
            "topHolders": {"reportDate": "", "totalRatio": None, "holders": []},
            "businessSegments": {"reportDate": "", "category": "", "items": []},
            "topCustomers": {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []},
            "orderBook": {"time": "", "asks": [], "bids": []},
            "kline": [],
            "last5": [],
        }
        full = update_row_from_public(base_row, as_of) if code not in watch_map else base_row
        full["code"] = code
        full["name"] = candidate["name"]
        full["todayPct"] = candidate.get("todayPct", full.get("todayPct"))
        if dashboard.strong_stock_passes_final_filters(full):
            strong_rows.append(full)
    if not strong_rows:
        prior_rows: list[dict] = []
        try:
            prior_path = latest_strong_before(as_of)
            prior_rows = json.loads(prior_path.read_text(encoding="utf-8"))
        except Exception:
            prior_rows = []
        fallback_rows = [dict(row) for row in prior_rows]
        fallback_rows.extend(dict(row) for row in watch_map.values())
        seen_codes: set[str] = set()
        for row in fallback_rows:
            code = row.get("code")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            full = dict(watch_map.get(code) or update_row_from_public(dict(row), as_of))
            if dashboard.strong_stock_passes_final_filters(full):
                strong_rows.append(full)
    news_map = dashboard.fetch_latest_news_map([(row["name"], row["code"]) for row in strong_rows]) if strong_rows else {}
    out = []
    for row in strong_rows:
        full = dict(row)
        full["research"] = dashboard.build_research_payload(row["code"], full.get("research", {}))
        full["latestNews"] = news_map.get(row["code"]) or full.get("latestNews") or {
            "time": "",
            "summary": "",
            "title": "",
            "link": "",
            "isRecent": False,
        }
        full.setdefault("marginFinancing", {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None})
        full.setdefault("topHolders", {"reportDate": "", "totalRatio": None, "holders": []})
        full.setdefault("businessSegments", {"reportDate": "", "category": "", "items": []})
        full.setdefault("topCustomers", {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []})
        full.setdefault("orderBook", {"time": "", "asks": [], "bids": []})
        full.setdefault("kline", [])
        full.setdefault("last5", [])
        out.append(full)
    return dashboard.finalize_strong_stocks(out, set(watch_map))


def main() -> int:
    as_of_raw = sys.argv[1] if len(sys.argv) > 1 else dashboard.AS_OF.isoformat()
    as_of = date.fromisoformat(as_of_raw)
    prev_json = latest_watchlist_before(as_of)
    out_json = ROOT / "reports" / f"watchlist_dashboard_{as_of.isoformat()}.json"
    out_html = ROOT / "reports" / f"watchlist_dashboard_{as_of.isoformat()}.html"
    out_strong = ROOT / "reports" / f"watchlist_strong_stocks_{as_of.isoformat()}.json"
    out_institution = ROOT / "reports" / f"institution_holdings_{as_of.isoformat()}.json"
    out_index = ROOT / "reports" / "share_dashboard" / "index.html"

    dataset = json.loads(prev_json.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        dataset = list(executor.map(lambda row: update_row_from_public(dict(row), as_of), dataset))
    dataset = dashboard.sort_watchlist_dataset(dashboard.backfill_market_caps(dashboard.hydrate_cached_dataset(dataset)))
    watch_map = {row["code"]: row for row in dataset}
    strong_rows = build_strong_rows(watch_map, as_of)
    strong_rows = dashboard.hydrate_cached_strong_stocks(strong_rows)
    institution_holdings = dashboard.fetch_institution_holdings(None)

    out_json.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    out_strong.write_text(json.dumps(strong_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    out_institution.write_text(json.dumps(institution_holdings, ensure_ascii=False, indent=2), encoding="utf-8")
    out_html.write_text(dashboard.build_html(dataset, strong_rows, None, institution_holdings), encoding="utf-8")
    out_index.write_text(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=../watchlist_dashboard_{as_of.isoformat()}.html">',
        encoding="utf-8",
    )
    print(out_html)
    print(out_json)
    print(out_strong)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
