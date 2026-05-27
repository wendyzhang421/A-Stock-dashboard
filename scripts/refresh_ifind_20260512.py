#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as b


def main() -> int:
    as_of_raw = sys.argv[1] if len(sys.argv) > 1 else b.AS_OF.isoformat()
    as_of = date.fromisoformat(as_of_raw)
    out_dir = Path("reports")
    prev_date = as_of
    prev_watch_path = None
    prev_strong_path = None
    while prev_date > date(2026, 1, 1):
        prev_date = prev_date.fromordinal(prev_date.toordinal() - 1)
        watch_path = out_dir / f"watchlist_dashboard_{prev_date.isoformat()}.json"
        strong_path = out_dir / f"watchlist_strong_stocks_{prev_date.isoformat()}.json"
        if watch_path.exists() and strong_path.exists():
            prev_watch_path = watch_path
            prev_strong_path = strong_path
            break
    if prev_watch_path is None or prev_strong_path is None:
        raise FileNotFoundError(f"No previous dashboard baseline found before {as_of.isoformat()}")
    prev_watch = b.hydrate_cached_dataset(json.loads(prev_watch_path.read_text(encoding="utf-8")))
    prev_strong = b.hydrate_cached_strong_stocks(json.loads(prev_strong_path.read_text(encoding="utf-8")))
    print("loaded", len(prev_watch), len(prev_strong), flush=True)

    access = b.get_access_token()
    print("token", flush=True)

    watch_codes = [row["code"] for row in prev_watch]
    history_map = b.fetch_history(access, watch_codes)
    print("history", len(history_map), flush=True)
    basic_map = b.fetch_basic(access, watch_codes)
    print("basic", len(basic_map), flush=True)
    realtime_map = b.fetch_realtime_map(access, watch_codes, as_of)
    print("realtime", len(realtime_map), flush=True)
    pe_map = b.fetch_pe_ratios(access, watch_codes)
    print("pe", len(pe_map), flush=True)

    prev_by_code = {row["code"]: row for row in prev_watch}
    watch = []
    for code in watch_codes:
        prev = dict(prev_by_code[code])
        history = history_map.get(code) or {}
        table = history.get("table") or {}
        times = (history.get("time") or [])[-30:]
        open_list = (table.get("open") or [])[-30:]
        high_list = (table.get("high") or [])[-30:]
        low_list = (table.get("low") or [])[-30:]
        close_list = (table.get("close") or [])[-30:]
        volume_list = (table.get("volume") or [])[-30:]
        amount_list = (table.get("amount") or [])[-30:]
        basic = basic_map.get(code, {})
        close_field = basic.get("ths_close_price_stock") or []
        total_field = basic.get("ths_total_shares_stock") or []
        float_field = basic.get("ths_free_float_shares_stock") or []
        latest_close = float((close_field[0] if close_field else close_list[-1]) or close_list[-1]) if close_list else float(prev.get("latestClose") or 0)
        total_shares = float((total_field[0] if total_field else 0) or 0)
        free_float_shares = float((float_field[0] if float_field else 0) or 0)
        if times and close_list and len(times) == len(close_list):
            prev["kline"] = [[times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]] for i in range(len(times))]
            last5 = []
            p = None
            for i in range(max(0, len(times) - 5), len(times)):
                pct = None if p is None else (close_list[i] / p - 1.0) * 100.0
                last5.append({"date": times[i], "close": close_list[i], "volume": volume_list[i], "amount": amount_list[i], "pct": pct})
                p = close_list[i]
            prev["last5"] = last5
            prev["todayVolume"] = volume_list[-1]
            prev["todayAmount"] = amount_list[-1]
            prev["todayPct"] = None if len(close_list) < 2 or not close_list[-2] else (close_list[-1] / close_list[-2] - 1.0) * 100.0
        realtime = realtime_map.get(code) or {}
        rt_latest = float(((realtime.get("latest") or [0])[-1]) or 0) if realtime.get("latest") else None
        rt_volume_hands = float(((realtime.get("volume") or [0])[-1]) or 0) if realtime.get("volume") else 0.0
        rt_amount = float(((realtime.get("amount") or [0])[-1]) or 0) if realtime.get("amount") else 0.0
        rt_pct = b.to_float(((realtime.get("changeRatio") or [None])[-1])) if realtime.get("changeRatio") else None
        if rt_latest:
            latest_close = rt_latest
        prev["latestClose"] = latest_close
        if rt_amount:
            prev["todayAmount"] = rt_amount
        if rt_pct is not None:
            prev["todayPct"] = rt_pct
        if rt_volume_hands:
            prev["todayVolume"] = rt_volume_hands * 100.0
        prev["turnoverRate"] = (rt_volume_hands * 100.0 / free_float_shares * 100.0) if (free_float_shares and rt_volume_hands) else ((prev.get("todayVolume") or 0) / free_float_shares * 100.0 if free_float_shares else None)
        prev["totalMarketCap"] = total_shares * latest_close if total_shares else (prev.get("floatMarketCap") or 0)
        prev["floatMarketCap"] = free_float_shares * latest_close if free_float_shares else (prev.get("floatMarketCap") or 0)
        prev["peRatio"] = pe_map.get(code, prev.get("peRatio"))
        watch.append(prev)
    watch = b.sort_watchlist_dataset(b.backfill_market_caps(watch))
    print("watch-built", len(watch), flush=True)

    resolved_trade_date, candidates = b.resolve_strong_stock_candidates(as_of)
    print("strong-candidates", resolved_trade_date.isoformat(), len(candidates), flush=True)
    prev_strong_map = {row["code"]: row for row in prev_strong}
    watch_map = {row["code"]: row for row in watch}
    candidate_codes = [row["code"] for row in candidates]
    candidate_history_map = b.fetch_history(access, candidate_codes) if candidate_codes else {}
    candidate_basic_map = b.fetch_basic(access, candidate_codes) if candidate_codes else {}
    candidate_news_map = b.fetch_latest_news_map([(row["name"], row["code"]) for row in candidates]) if candidates else {}

    def build_candidate_row(candidate: dict) -> dict | None:
        code = candidate["code"]
        row = dict(watch_map.get(code) or prev_strong_map.get(code) or {})
        history = candidate_history_map.get(code) or {}
        table = history.get("table") or {}
        times = (history.get("time") or [])[-30:]
        open_list = (table.get("open") or [])[-30:]
        high_list = (table.get("high") or [])[-30:]
        low_list = (table.get("low") or [])[-30:]
        close_list = (table.get("close") or [])[-30:]
        volume_list = (table.get("volume") or [])[-30:]
        amount_list = (table.get("amount") or [])[-30:]
        basic = candidate_basic_map.get(code, {})
        close_field = basic.get("ths_close_price_stock") or []
        total_field = basic.get("ths_total_shares_stock") or []
        float_field = basic.get("ths_free_float_shares_stock") or []
        latest_close = float(
            (close_field[0] if close_field else None)
            or (close_list[-1] if close_list else None)
            or row.get("latestClose")
            or 0.0
        )
        total_shares = float((total_field[0] if total_field else 0) or 0)
        free_float_shares = float((float_field[0] if float_field else 0) or 0)
        today_amount = amount_list[-1] if amount_list else float(row.get("todayAmount") or 0.0)
        today_volume = volume_list[-1] if volume_list else float(row.get("todayVolume") or 0.0)
        turnover_rate = (
            (today_volume / free_float_shares * 100.0) if today_volume and free_float_shares else row.get("turnoverRate")
        )
        total_market_cap = total_shares * latest_close if total_shares else float(row.get("totalMarketCap") or 0.0)
        float_market_cap = free_float_shares * latest_close if free_float_shares else float(row.get("floatMarketCap") or 0.0)
        if total_market_cap <= 0 and float_market_cap > 0:
            total_market_cap = float_market_cap
        built = row
        built["code"] = code
        built["name"] = candidate["name"]
        built["mainBusiness"] = built.get("mainBusiness") or b.MAIN_BUSINESS_MAP.get(code) or "-"
        built["latestClose"] = latest_close
        built["totalMarketCap"] = total_market_cap
        built["floatMarketCap"] = float_market_cap
        built["todayAmount"] = today_amount
        built["todayVolume"] = today_volume
        built["turnoverRate"] = turnover_rate
        built["todayPct"] = candidate["todayPct"]
        built["latestNews"] = candidate_news_map.get(code) or prev_strong_map.get(code, {}).get("latestNews") or built.get("latestNews") or {"time": "", "summary": "", "title": "", "link": ""}
        built["topHolders"] = prev_strong_map.get(code, {}).get("topHolders") or built.get("topHolders") or {"reportDate": "", "totalRatio": None, "holders": []}
        built["businessSegments"] = prev_strong_map.get(code, {}).get("businessSegments") or built.get("businessSegments") or {"reportDate": "", "category": "", "items": []}
        built["topCustomers"] = prev_strong_map.get(code, {}).get("topCustomers") or built.get("topCustomers") or {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []}
        built["research"] = built.get("research") or b.empty_research_payload()
        built["peRatio"] = built.get("peRatio")
        built["marginFinancing"] = built.get("marginFinancing") or {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None}
        if times and close_list and len(times) == len(close_list):
            built["kline"] = [[times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]] for i in range(len(times))]
            last5 = []
            prev_close = None
            for i in range(max(0, len(times) - 5), len(times)):
                current_close = close_list[i]
                pct = None if prev_close in (None, 0) or current_close in (None, 0) else (current_close / prev_close - 1.0) * 100.0
                last5.append({"date": times[i], "close": current_close, "volume": volume_list[i], "amount": amount_list[i], "pct": pct})
                prev_close = current_close if current_close not in (None, 0) else prev_close
            built["last5"] = last5
        return built

    strong = []
    for candidate in candidates:
        row = build_candidate_row(candidate)
        if not row:
            continue
        if b.strong_stock_passes_final_filters(row):
            strong.append(row)
    strong = b.finalize_strong_stocks(strong, set(watch_map))
    strong = b.hydrate_cached_strong_stocks(strong)
    print("strong-built", len(strong), flush=True)

    overview = b.fetch_market_overview(access)
    institution_holdings = b.fetch_institution_holdings(access)
    print("overview", len(overview.get("indices") or []), flush=True)

    json_path = out_dir / f"watchlist_dashboard_{as_of.isoformat()}.json"
    strong_path = out_dir / f"watchlist_strong_stocks_{as_of.isoformat()}.json"
    institution_path = out_dir / f"institution_holdings_{as_of.isoformat()}.json"
    html_path = out_dir / f"watchlist_dashboard_{as_of.isoformat()}.html"
    json_path.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_path.write_text(json.dumps(strong, ensure_ascii=False, indent=2), encoding="utf-8")
    institution_path.write_text(json.dumps(institution_holdings, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(b.build_html(watch, strong, overview, institution_holdings), encoding="utf-8")
    (out_dir / "share_dashboard" / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=../watchlist_dashboard_{as_of.isoformat()}.html">',
        encoding="utf-8",
    )
    print(html_path, flush=True)
    print(json_path, flush=True)
    print(strong_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
