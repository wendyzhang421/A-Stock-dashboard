#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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


def fetch_json(url: str, params: dict[str, Any], timeout: int = 12, retries: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if i + 1 < retries:
                time.sleep(0.6 * (i + 1))
                continue
            break
    raise RuntimeError(f"request failed: {url} {params} err={last_error}")


def fetch_candidates(start: str, end: str) -> list[dict[str, Any]]:
    page = 1
    page_size = 500
    out: list[dict[str, Any]] = []
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
            "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')(CHANGE_RATE>5)",
        }
        payload = fetch_json(FUNDFLOW_API, params, timeout=12, retries=3)
        if not payload.get("success"):
            raise RuntimeError(f"fundflow api error: {payload}")
        result = payload.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "").strip()
            name = str(row.get("SECURITY_NAME_ABBR") or "").strip()
            trade_date = str(row.get("TRADE_DATE") or "").split(" ")[0]
            chg = to_float(row.get("CHANGE_RATE"))
            if not code or not name or not trade_date or chg is None:
                continue
            out.append(
                {
                    "trade_date": trade_date,
                    "code": code,
                    "name": name,
                    "day_chg_pct": chg,
                }
            )
        pages = int(result.get("pages") or 0)
        if pages > 0 and page >= pages:
            break
        if not rows:
            break
        page += 1
    return out


def fetch_kline_metrics(code: str, start: str, end: str) -> tuple[str, dict[str, float], dict[str, str], dict[str, float]]:
    symbol = code_to_tencent_symbol(code)
    params = {
        "param": f"{symbol},day,{start},{end},640,",
    }
    try:
        payload = fetch_json(TENCENT_KLINE_API, params, timeout=12, retries=2)
    except Exception:
        return code, {}, {}, {}

    data = (payload.get("data") or {}).get(symbol) or {}
    series = data.get("day") or data.get("qfqday") or []
    if not isinstance(series, list) or not series:
        return code, {}, {}, {}

    amounts: dict[str, float] = {}
    closes: dict[str, float] = {}
    days: list[str] = []
    for item in series:
        if not isinstance(item, list) or len(item) < 6:
            continue
        day = str(item[0]).strip()
        close = to_float(item[2])
        vol_lot = to_float(item[5])
        if not day or close is None or vol_lot is None:
            continue
        amounts[day] = close * vol_lot * 100.0
        closes[day] = close
        days.append(day)

    days = sorted(set(days))
    next_date: dict[str, str] = {}
    next_chg: dict[str, float] = {}
    for i in range(len(days) - 1):
        d0 = days[i]
        d1 = days[i + 1]
        c0 = closes.get(d0)
        c1 = closes.get(d1)
        if c0 is None or c1 is None or c0 == 0:
            continue
        next_date[d0] = d1
        next_chg[d0] = (c1 / c0 - 1.0) * 100.0
    return code, amounts, next_date, next_chg


def fetch_market_cap(code: str) -> tuple[str, float | None]:
    params = {
        "invt": "2",
        "fltt": "2",
        "fields": "f57,f58,f116",
        "secid": code_to_secid(code),
    }
    try:
        payload = fetch_json(QUOTE_API, params, timeout=6, retries=2)
        data = payload.get("data") or {}
        cap = to_float(data.get("f116"))
        if cap is None or cap <= 0:
            return code, None
        return code, cap
    except Exception:
        return code, None


def save_csv(path: str, records: list[dict[str, Any]]) -> None:
    fields = [
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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in records:
            x = dict(row)
            x["day_chg_pct"] = f"{float(row['day_chg_pct']):.4f}"
            x["day_amount_yi"] = f"{float(row['day_amount_yi']):.4f}"
            x["market_cap_yi"] = f"{float(row['market_cap_yi']):.4f}"
            if row.get("next_day_chg_pct") is None:
                x["next_day_chg_pct"] = ""
            else:
                x["next_day_chg_pct"] = f"{float(row['next_day_chg_pct']):.4f}"
            writer.writerow(x)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    with_next = [r for r in records if r.get("next_day_chg_pct") is not None]
    up = sum(1 for r in with_next if float(r["next_day_chg_pct"]) > 0)
    down = sum(1 for r in with_next if float(r["next_day_chg_pct"]) < 0)
    flat = sum(1 for r in with_next if float(r["next_day_chg_pct"]) == 0)
    avg = None
    if with_next:
        avg = sum(float(r["next_day_chg_pct"]) for r in with_next) / len(with_next)
    return {
        "total": len(records),
        "with_next": len(with_next),
        "up": up,
        "down": down,
        "flat": flat,
        "avg_next": avg,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="2026年3月筛选：涨幅>5%、成交额>1亿、市值>100亿，统计后一日涨跌")
    parser.add_argument("--start", default="2026-03-01")
    parser.add_argument("--end", default="2026-03-31")
    parser.add_argument("--amount-threshold", type=float, default=1e8)
    parser.add_argument("--cap-threshold", type=float, default=1e10)
    parser.add_argument("--output", default="march2026_gt5_amt1e8_cap100e_followday.csv")
    args = parser.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").date()
    kline_end = (end_dt + timedelta(days=10)).strftime("%Y-%m-%d")

    print("[1/4] 拉取3月涨幅>5%候选...", flush=True)
    candidates = fetch_candidates(args.start, args.end)
    print(f"[1/4] 候选记录: {len(candidates)}", flush=True)
    if not candidates:
        save_csv(args.output, [])
        print("无候选数据")
        return 0

    codes = sorted({c["code"] for c in candidates})
    print(f"[2/4] 拉取候选股票日K成交额/次日涨跌，股票数: {len(codes)}", flush=True)
    with ThreadPoolExecutor(max_workers=40) as executor:
        metrics = list(executor.map(lambda c: fetch_kline_metrics(c, args.start, kline_end), codes))
    amount_map: dict[str, dict[str, float]] = {}
    next_date_map: dict[str, dict[str, str]] = {}
    next_chg_map: dict[str, dict[str, float]] = {}
    for code, amounts, next_dates, next_chg in metrics:
        amount_map[code] = amounts
        next_date_map[code] = next_dates
        next_chg_map[code] = next_chg

    amount_pass: list[dict[str, Any]] = []
    amount_pass_codes: set[str] = set()
    for row in candidates:
        code = row["code"]
        day = row["trade_date"]
        amount = (amount_map.get(code) or {}).get(day)
        if amount is None or amount <= args.amount_threshold:
            continue
        x = dict(row)
        x["amount"] = amount
        x["next_trade_date"] = (next_date_map.get(code) or {}).get(day, "")
        x["next_day_chg_pct"] = (next_chg_map.get(code) or {}).get(day)
        amount_pass.append(x)
        amount_pass_codes.add(code)
    print(f"[2/4] 成交额>1亿记录: {len(amount_pass)}，股票数: {len(amount_pass_codes)}", flush=True)
    if not amount_pass:
        save_csv(args.output, [])
        print("无满足成交额条件数据")
        return 0

    print(f"[3/4] 拉取总市值，股票数: {len(amount_pass_codes)}", flush=True)
    with ThreadPoolExecutor(max_workers=60) as executor:
        caps = list(executor.map(fetch_market_cap, sorted(amount_pass_codes)))
    cap_map = {code: cap for code, cap in caps}

    records: list[dict[str, Any]] = []
    for row in amount_pass:
        cap = cap_map.get(row["code"])
        if cap is None or cap <= args.cap_threshold:
            continue
        records.append(
            {
                "trade_date": row["trade_date"],
                "code": row["code"],
                "name": row["name"],
                "day_chg_pct": row["day_chg_pct"],
                "day_amount_yi": row["amount"] / 1e8,
                "market_cap_yi": cap / 1e8,
                "next_trade_date": row.get("next_trade_date", ""),
                "next_day_chg_pct": row.get("next_day_chg_pct"),
            }
        )
    records.sort(key=lambda x: (x["trade_date"], x["day_chg_pct"]), reverse=False)

    save_csv(args.output, records)
    stat = summarize(records)
    avg_text = "None" if stat["avg_next"] is None else f"{float(stat['avg_next']):.4f}%"
    print(
        "[4/4] 完成: "
        f"signals={stat['total']}, with_next={stat['with_next']}, up={stat['up']}, down={stat['down']}, flat={stat['flat']}, "
        f"avg_next={avg_text}",
        flush=True,
    )
    print(f"output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
