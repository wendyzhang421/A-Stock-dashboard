#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import requests

API_URL = "https://datacenter-web.eastmoney.com/api/data/get"
QUOTE_API_URL = "https://push2.eastmoney.com/api/qt/stock/get"


def fetch_json(params: dict[str, Any], timeout: int = 15, retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"API error: {data}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0 * attempt)
                continue
            raise
    raise RuntimeError(f"request failed: {last_error}")


def get_latest_trade_date() -> str:
    params = {
        "type": "RPT_DMSK_TS_FUNDFLOWHIS",
        "sty": "ALL",
        "source": "SECURITIES",
        "client": "APP",
        "st": "TRADE_DATE",
        "sr": "-1",
        "p": "1",
        "ps": "1",
    }
    data = fetch_json(params)
    row = (data.get("result") or {}).get("data", [])[0]
    if not row or "TRADE_DATE" not in row:
        raise RuntimeError("failed to get latest trade date")
    return str(row["TRADE_DATE"]).split(" ")[0]


def fetch_rows_by_filter(filter_expr: str) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    page = 1
    page_size = 5000
    max_pages: int | None = None
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
        data = fetch_json(params)
        result = data.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        all_rows.extend(rows)
        if max_pages is None:
            count = int(result.get("count") or 0)
            if count > 0:
                max_pages = (count + page_size - 1) // page_size
        if max_pages is not None and page >= max_pages:
            break
        page += 1
    return all_rows


def get_recent_trade_dates(start_date: str, end_date: str, probe_code: str = "000001") -> list[str]:
    filter_expr = (
        f'(SECURITY_CODE="{probe_code}")'
        f"(TRADE_DATE>='{start_date}')"
        f"(TRADE_DATE<='{end_date}')"
    )
    rows = fetch_rows_by_filter(filter_expr)
    dates = sorted(
        {
            str(r.get("TRADE_DATE") or "").split(" ")[0]
            for r in rows
            if str(r.get("TRADE_DATE") or "").strip()
        },
        reverse=True,
    )
    return dates


def fetch_rows_for_day(day: str) -> list[dict[str, Any]]:
    return fetch_rows_by_filter(f"(TRADE_DATE>='{day}')(TRADE_DATE<='{day}')")


def code_to_secid(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_market_cap(code: str, timeout: int = 6, retries: int = 2) -> float | None:
    params = {
        "invt": "2",
        "fltt": "2",
        "fields": "f57,f58,f116",
        "secid": code_to_secid(code),
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(QUOTE_API_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") or {}
            value = data.get("f116")
            if value in (None, "", "-"):
                return None
            market_cap = float(value)
            if market_cap <= 0:
                return None
            return market_cap
        except (requests.RequestException, ValueError, TypeError):
            if attempt < retries:
                time.sleep(0.2 * attempt)
                continue
            return None
    return None


def top_by_net_inflow(rows: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        name = str(row.get("SECURITY_NAME_ABBR") or "").strip()
        net_inflow = row.get("NET_INFLOW")
        if not code or not name or net_inflow in (None, ""):
            continue
        try:
            value = float(net_inflow)
        except (TypeError, ValueError):
            continue
        items.append(
            {
                "code": code,
                "name": name,
                "net_inflow": value,
            }
        )
    return sorted(items, key=lambda x: x["net_inflow"], reverse=True)[:top_n]


def attach_ratio(daily: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    codes = sorted({item["code"] for rows in daily.values() for item in rows})
    with ThreadPoolExecutor(max_workers=10) as executor:
        caps = list(executor.map(fetch_market_cap, codes))
    cap_map = {code: cap for code, cap in zip(codes, caps)}

    out: dict[str, list[dict[str, Any]]] = {}
    for day, rows in daily.items():
        enriched: list[dict[str, Any]] = []
        for item in rows:
            market_cap = cap_map.get(item["code"])
            ratio = (item["net_inflow"] / market_cap) if market_cap else None
            x = dict(item)
            x["market_cap"] = market_cap
            x["ratio"] = ratio
            enriched.append(x)

        out[day] = sorted(
            enriched,
            key=lambda x: (
                x["ratio"] is not None,
                x["ratio"] if x["ratio"] is not None else -1.0,
            ),
            reverse=True,
        )
    return out


def fmt_money_yi(value: float) -> str:
    return f"{value / 1e8:,.2f}亿"


def fmt_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "N/A"
    return f"{ratio * 100:.2f}%"


def build_day_message(day: str, rows: list[dict[str, Any]], latest_date: str) -> str:
    lines = [
        f"A股近一周每日资金净流入Top{len(rows)}（按占比重排）",
        f"交易日: {day} | 最新交易日: {latest_date}",
        "规则: 先按当日净流入取Top40，再按(净流入/总市值)降序",
        "",
    ]
    for idx, row in enumerate(rows, start=1):
        cap_text = fmt_money_yi(float(row["market_cap"])) if row["market_cap"] else "N/A"
        lines.append(
            f"{idx:>2}. {row['code']} {row['name']}  占比 {fmt_ratio(row['ratio'])}  "
            f"净流入 {fmt_money_yi(row['net_inflow'])}  总市值 {cap_text}"
        )
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str, thread_id: int | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise RuntimeError(f"telegram error: {data}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A股近一周每日净流入Top40按净流入/市值占比降序推送")
    parser.add_argument("--days", type=int, default=7, help="统计窗口天数，默认7")
    parser.add_argument("--top", type=int, default=40, help="每日TopN，默认40")
    parser.add_argument("--proxy", type=str, default=os.getenv("HTTPS_PROXY", ""), help="HTTPS代理")
    parser.add_argument(
        "--telegram-token",
        type=str,
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=str,
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="Telegram Chat ID",
    )
    parser.add_argument(
        "--telegram-thread-id",
        type=int,
        default=int(os.getenv("TELEGRAM_THREAD_ID", "0") or 0),
        help="Telegram 话题ID，可选",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()

    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    latest_date = datetime.strptime(get_latest_trade_date(), "%Y-%m-%d").date()
    start_date = (latest_date - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    end_date = latest_date.strftime("%Y-%m-%d")

    trade_dates = get_recent_trade_dates(start_date, end_date)
    daily_top: dict[str, list[dict[str, Any]]] = {}
    for day in trade_dates:
        rows = fetch_rows_for_day(day)
        daily_top[day] = top_by_net_inflow(rows, top_n=args.top)

    if not daily_top:
        raise RuntimeError("no data in selected window")

    daily_with_ratio = attach_ratio(daily_top)
    ordered_days = sorted(daily_with_ratio.keys(), reverse=True)

    messages = [build_day_message(day, daily_with_ratio[day], end_date) for day in ordered_days]
    for message in messages:
        print(message)
        print("\n" + "-" * 80 + "\n")

    if args.dry_run:
        return 0

    if not args.telegram_token or not args.telegram_chat_id:
        raise RuntimeError("missing telegram token/chat_id")

    thread_id = args.telegram_thread_id if args.telegram_thread_id > 0 else None
    for message in messages:
        send_telegram(args.telegram_token, args.telegram_chat_id, message, thread_id=thread_id)
        time.sleep(0.4)
    print("Telegram 推送成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
