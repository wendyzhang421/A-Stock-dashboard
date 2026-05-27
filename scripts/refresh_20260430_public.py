#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as dashboard


AS_OF = date(2026, 4, 30)
PREV_DATE = date(2026, 4, 29)


def market_prefix(code: str) -> str:
    raw = code.split(".")[0]
    return "sh" if raw.startswith(("5", "6", "9")) else "sz"


def fetch_public_kline(code: str) -> list[list[float | str]]:
    symbol = f"{market_prefix(code)}{code.split('.')[0]}"
    resp = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,2026-03-15,2026-04-30,640,qfq"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = (((payload.get("data") or {}).get(symbol) or {}).get("qfqday") or [])
    rows: list[list[float | str]] = []
    for row in data:
        if len(row) < 6:
            continue
        rows.append(
            [
                str(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[4]),
                float(row[3]),
                float(row[5]),
            ]
        )
    return rows


def update_row_from_public(row: dict) -> dict:
    code = row["code"]
    prev_close = float(row.get("latestClose") or 0.0)
    prev_total_cap = float(row.get("totalMarketCap") or 0.0)
    prev_float_cap = float(row.get("floatMarketCap") or 0.0)
    total_shares = (prev_total_cap / prev_close) if prev_close else 0.0
    float_shares = (prev_float_cap / prev_close) if prev_close else 0.0
    snapshot = dashboard.fetch_quote_snapshot(code)
    public_kline = fetch_public_kline(code)
    old_amount_by_date = {
        k[0]: k[6]
        for k in row.get("kline", [])
        if isinstance(k, list) and len(k) >= 7
    }
    kline = []
    for item in public_kline[-30:]:
        trade_date, open_px, close_px, low_px, high_px, volume = item
        amount = old_amount_by_date.get(trade_date, 0)
        if trade_date == AS_OF.isoformat():
            amount = snapshot.get("todayAmount") or amount or 0
        kline.append([trade_date, open_px, close_px, low_px, high_px, volume, amount])

    if kline:
        row["kline"] = kline
        row["latestClose"] = float(kline[-1][2])
        row["todayVolume"] = float(kline[-1][5])
        row["todayAmount"] = snapshot.get("todayAmount") or float(kline[-1][6]) or 0.0
        prev_close = float(kline[-2][2]) if len(kline) >= 2 else None
        row["todayPct"] = ((row["latestClose"] / prev_close - 1.0) * 100.0) if prev_close else None
        last5 = []
        prev = None
        for item in kline[-5:]:
            pct = None if prev is None else (float(item[2]) / prev - 1.0) * 100.0
            last5.append(
                {
                    "date": item[0],
                    "close": float(item[2]),
                    "volume": float(item[5]),
                    "amount": float(item[6]),
                    "pct": pct,
                }
            )
            prev = float(item[2])
        row["last5"] = last5

    if snapshot.get("latestClose"):
        row["latestClose"] = snapshot["latestClose"]
    if snapshot.get("todayAmount") is not None:
        row["todayAmount"] = snapshot["todayAmount"]
    if snapshot.get("turnoverRate") is not None:
        row["turnoverRate"] = snapshot["turnoverRate"]
    if float_shares and row.get("latestClose"):
        row["floatMarketCap"] = float_shares * float(row["latestClose"])
    elif snapshot.get("floatMarketCap"):
        row["floatMarketCap"] = snapshot["floatMarketCap"]
    if total_shares and row.get("latestClose"):
        row["totalMarketCap"] = total_shares * float(row["latestClose"])
    elif snapshot.get("totalMarketCap"):
        row["totalMarketCap"] = snapshot["totalMarketCap"]
    elif row.get("floatMarketCap"):
        row["totalMarketCap"] = row["floatMarketCap"]
    return row


def build_strong_rows(watch_map: dict[str, dict]) -> list[dict]:
    strong_rows = dashboard.fetch_strong_stocks(AS_OF)
    out = []
    for row in strong_rows:
        full = dict(watch_map.get(row["code"], row))
        full.update(row)
        full.setdefault("research", dashboard.build_research_payload(row["code"], full.get("research", {})))
        full.setdefault("marginFinancing", {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None})
        full.setdefault("topHolders", {"reportDate": "", "totalRatio": None, "holders": []})
        full.setdefault("businessSegments", {"reportDate": "", "category": "", "items": []})
        full.setdefault("topCustomers", {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []})
        full.setdefault("orderBook", {"time": "", "asks": [], "bids": []})
        full.setdefault("kline", [])
        full.setdefault("last5", [])
        if row["code"] not in watch_map:
            full = update_row_from_public(full)
        out.append(full)
    return out


def main() -> int:
    prev_json = ROOT / "reports" / f"watchlist_dashboard_{PREV_DATE.isoformat()}.json"
    out_json = ROOT / "reports" / f"watchlist_dashboard_{AS_OF.isoformat()}.json"
    out_html = ROOT / "reports" / f"watchlist_dashboard_{AS_OF.isoformat()}.html"
    out_strong = ROOT / "reports" / f"watchlist_strong_stocks_{AS_OF.isoformat()}.json"

    dataset = json.loads(prev_json.read_text(encoding="utf-8"))
    dataset = [update_row_from_public(dict(row)) for row in dataset]
    dataset = dashboard.sort_watchlist_dataset(dashboard.backfill_market_caps(dashboard.hydrate_cached_dataset(dataset)))
    watch_map = {row["code"]: row for row in dataset}
    strong_rows = build_strong_rows(watch_map)

    out_json.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    out_strong.write_text(json.dumps(strong_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    out_html.write_text(dashboard.build_html(dataset, strong_rows), encoding="utf-8")
    print(out_html)
    print(out_json)
    print(out_strong)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
