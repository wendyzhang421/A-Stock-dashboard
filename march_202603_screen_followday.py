#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

import requests

FUNDFLOW_API = "https://datacenter-web.eastmoney.com/api/data/get"
QUOTE_API = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def to_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def code_to_secid(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def code_to_tencent_symbol(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def fetch_json(
    url: str,
    params: dict[str, Any],
    timeout: int = 12,
    retries: int = 2,
    use_proxy: bool = True,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if use_proxy:
                response = requests.get(url, params=params, timeout=timeout)
            else:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)
                continue
            raise RuntimeError(f"request failed: {url} {params} err={last_error}") from exc
    raise RuntimeError(f"request failed: {url} {params} err={last_error}")


def fetch_fundflow_rows_by_filter(filter_expr: str, page_size: int = 500) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    page = 1

    while True:
        params = {
            "type": "RPT_DMSK_TS_FUNDFLOWHIS",
            "sty": "ALL",
            "source": "SECURITIES",
            "client": "APP",
            "st": "TRADE_DATE",
            "sr": "-1",
            "p": str(page),
            "ps": str(page_size),
            "filter": filter_expr,
        }
        payload = fetch_json(FUNDFLOW_API, params)
        if not payload.get("success"):
            code = int(payload.get("code") or 0)
            message = str(payload.get("message") or "")
            if code == 9201 or "为空" in message:
                return []
            raise RuntimeError(f"fundflow api error: {payload}")
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        all_rows.extend(rows)
        pages = int(result.get("pages") or 0)
        if pages > 0 and page >= pages:
            break
        page += 1
    return all_rows


def calendar_days(start_day: date, end_day: date) -> list[str]:
    days: list[str] = []
    current = start_day
    while current <= end_day:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days


def fetch_market_cap(code: str) -> float | None:
    params = {
        "invt": "2",
        "fltt": "2",
        "fields": "f57,f58,f116",
        "secid": code_to_secid(code),
    }
    try:
        payload = fetch_json(QUOTE_API, params, timeout=6, retries=1)
        data = payload.get("data") or {}
        cap = to_float(data.get("f116"))
        if cap is None or cap <= 0:
            return None
        return cap
    except Exception:
        return None


def fetch_daily_amounts_for_code(code: str, beg: str, end: str) -> dict[str, float]:
    start = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}"
    finish = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    symbol = code_to_tencent_symbol(code)
    params = {
        "param": f"{symbol},day,{start},{finish},640,",
    }
    try:
        response = requests.get(TENCENT_KLINE_API, params=params, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}

    data = (payload.get("data") or {}).get(symbol) or {}
    klines = data.get("day") or data.get("qfqday") or []
    out: dict[str, float] = {}
    for line in klines:
        if not isinstance(line, list) or len(line) < 6:
            continue
        day = str(line[0]).strip()
        close_price = to_float(line[2])
        volume_lot = to_float(line[5])
        if not day or close_price is None or volume_lot is None:
            continue
        amount = close_price * volume_lot * 100.0
        out[day] = amount
    return out


def build_records(start: str, end: str, cap_threshold: float, amount_threshold: float) -> tuple[list[dict[str, Any]], list[str]]:
    start_day = datetime.strptime(start, "%Y-%m-%d").date()
    end_day = datetime.strptime(end, "%Y-%m-%d").date()

    day_rows: dict[str, list[dict[str, Any]]] = {}
    print(f"[1/4] 拉取 {start} ~ {end} 每日资金流数据...", flush=True)
    for day in calendar_days(start_day, end_day):
        rows = fetch_fundflow_rows_by_filter(f"(TRADE_DATE>='{day}')(TRADE_DATE<='{day}')")
        if rows:
            day_rows[day] = rows

    trading_days = sorted(day_rows.keys())
    print(f"[1/4] 交易日数量: {len(trading_days)}", flush=True)
    if not trading_days:
        return [], []

    rows_by_day_code: dict[str, dict[str, dict[str, Any]]] = {}
    candidates: list[dict[str, Any]] = []
    for day in trading_days:
        rows_by_day_code[day] = {}
        for row in day_rows[day]:
            code = str(row.get("SECURITY_CODE") or "").strip()
            name = str(row.get("SECURITY_NAME_ABBR") or "").strip()
            chg = to_float(row.get("CHANGE_RATE"))
            if not code or not name:
                continue
            rows_by_day_code[day][code] = row
            if chg is None:
                continue
            if chg > 5:
                candidates.append(
                    {
                        "trade_date": day,
                        "code": code,
                        "name": name,
                        "day_chg_pct": chg,
                    }
                )

    if not candidates:
        return [], trading_days

    candidate_codes = sorted({item["code"] for item in candidates})
    print(f"[2/4] 候选记录: {len(candidates)}，候选股票: {len(candidate_codes)}，先拉取日K成交额...", flush=True)
    beg = start_day.strftime("%Y%m%d")
    end_plus = (end_day + timedelta(days=7)).strftime("%Y%m%d")
    print("[3/4] 拉取候选股票日K成交额...", flush=True)
    with ThreadPoolExecutor(max_workers=36) as executor:
        amount_maps = list(executor.map(lambda c: fetch_daily_amounts_for_code(c, beg, end_plus), candidate_codes))
    amount_by_code = {code: amount_map for code, amount_map in zip(candidate_codes, amount_maps)}
    sample_amount_stats = [(code, len(amount_by_code.get(code) or {})) for code in candidate_codes[:10]]
    print(f"[3/4] 成交额样例天数: {sample_amount_stats}", flush=True)

    amount_qualified_candidates: list[dict[str, Any]] = []
    amount_qualified_codes: set[str] = set()
    for item in candidates:
        day = item["trade_date"]
        code = item["code"]
        amount = (amount_by_code.get(code) or {}).get(day)
        if amount is None or amount <= amount_threshold:
            continue
        amount_qualified_candidates.append(item)
        amount_qualified_codes.add(code)

    print(
        f"[3/4] 成交额>{amount_threshold/1e8:.0f}亿 记录数: {len(amount_qualified_candidates)}，股票数: {len(amount_qualified_codes)}",
        flush=True,
    )
    if not amount_qualified_candidates:
        return [], trading_days

    print(f"[4/4] 拉取市值并做最终筛选...", flush=True)
    amount_qualified_codes_list = sorted(amount_qualified_codes)
    with ThreadPoolExecutor(max_workers=48) as executor:
        cap_values = list(executor.map(fetch_market_cap, amount_qualified_codes_list))
    cap_map = {code: cap for code, cap in zip(amount_qualified_codes_list, cap_values)}

    qualified_codes = [code for code in amount_qualified_codes_list if (cap_map.get(code) or 0.0) > cap_threshold]
    print(f"[4/4] 市值>{cap_threshold/1e8:.0f}亿 股票数: {len(qualified_codes)}", flush=True)
    if not qualified_codes:
        return [], trading_days

    next_day_map: dict[str, str] = {}
    for i, day in enumerate(trading_days[:-1]):
        next_day_map[day] = trading_days[i + 1]

    qualified_code_set = set(qualified_codes)
    records: list[dict[str, Any]] = []
    for item in amount_qualified_candidates:
        day = item["trade_date"]
        code = item["code"]
        if code not in qualified_code_set:
            continue
        cap = cap_map.get(code)
        if cap is None or cap <= cap_threshold:
            continue
        amount = (amount_by_code.get(code) or {}).get(day)
        if amount is None or amount <= amount_threshold:
            continue
        next_day = next_day_map.get(day)
        next_day_chg: float | None = None
        if next_day:
            next_row = (rows_by_day_code.get(next_day) or {}).get(code)
            if next_row:
                next_day_chg = to_float(next_row.get("CHANGE_RATE"))
        records.append(
            {
                "trade_date": day,
                "code": code,
                "name": item["name"],
                "day_chg_pct": item["day_chg_pct"],
                "day_amount_yi": amount / 1e8,
                "market_cap_yi": cap / 1e8,
                "next_trade_date": next_day or "",
                "next_day_chg_pct": next_day_chg,
            }
        )

    records.sort(key=lambda x: (x["trade_date"], x["day_chg_pct"]), reverse=False)
    print(f"[4/4] 筛选完成，记录数: {len(records)}", flush=True)
    return records, trading_days


def save_csv(path: str, records: list[dict[str, Any]]) -> None:
    headers = [
        "trade_date",
        "code",
        "name",
        "day_chg_pct",
        "day_amount_yi",
        "market_cap_yi",
        "next_trade_date",
        "next_day_chg_pct",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in records:
            out = dict(row)
            out["day_chg_pct"] = f"{float(row['day_chg_pct']):.4f}"
            out["day_amount_yi"] = f"{float(row['day_amount_yi']):.4f}"
            out["market_cap_yi"] = f"{float(row['market_cap_yi']):.4f}"
            if row.get("next_day_chg_pct") is None:
                out["next_day_chg_pct"] = ""
            else:
                out["next_day_chg_pct"] = f"{float(row['next_day_chg_pct']):.4f}"
            writer.writerow(out)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    with_next = [r for r in records if r.get("next_day_chg_pct") is not None]
    positive = sum(1 for r in with_next if float(r["next_day_chg_pct"]) > 0)
    negative = sum(1 for r in with_next if float(r["next_day_chg_pct"]) < 0)
    flat = sum(1 for r in with_next if float(r["next_day_chg_pct"]) == 0)
    avg = (sum(float(r["next_day_chg_pct"]) for r in with_next) / len(with_next)) if with_next else None
    return {
        "total_signals": len(records),
        "with_next_day": len(with_next),
        "up_count": positive,
        "down_count": negative,
        "flat_count": flat,
        "avg_next_day_chg": avg,
    }


def day_stats(records: list[dict[str, Any]]) -> list[tuple[str, int, float | None]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        by_day[row["trade_date"]].append(row)
    out: list[tuple[str, int, float | None]] = []
    for day in sorted(by_day.keys()):
        rows = by_day[day]
        vals = [float(r["next_day_chg_pct"]) for r in rows if r.get("next_day_chg_pct") is not None]
        avg = (sum(vals) / len(vals)) if vals else None
        out.append((day, len(rows), avg))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="筛选2026年3月涨幅>5%、成交额>1亿、市值>100亿，并统计后一日涨跌")
    parser.add_argument("--start", default="2026-03-01")
    parser.add_argument("--end", default="2026-03-31")
    parser.add_argument("--amount-threshold", type=float, default=1e8, help="成交额阈值（元）")
    parser.add_argument("--cap-threshold", type=float, default=1e10, help="总市值阈值（元）")
    parser.add_argument(
        "--output",
        default="march2026_gt5_amt1e8_cap100e_followday.csv",
        help="输出CSV路径",
    )
    parser.add_argument("--proxy", default=os.getenv("HTTPS_PROXY", ""))
    args = parser.parse_args()

    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    records, trading_days = build_records(
        start=args.start,
        end=args.end,
        cap_threshold=args.cap_threshold,
        amount_threshold=args.amount_threshold,
    )
    save_csv(args.output, records)
    stats = summarize(records)

    print(f"trading_days: {len(trading_days)} -> {trading_days}")
    print(
        f"signals={stats['total_signals']}, with_next={stats['with_next_day']}, "
        f"up={stats['up_count']}, down={stats['down_count']}, flat={stats['flat_count']}, "
        f"avg_next={stats['avg_next_day_chg']}"
    )
    print("day_breakdown:")
    for day, cnt, avg in day_stats(records):
        avg_text = "N/A" if avg is None else f"{avg:.4f}%"
        print(f"  {day}: count={cnt}, next_day_avg={avg_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
