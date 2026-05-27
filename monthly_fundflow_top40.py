#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
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
                time.sleep(1.2 * attempt)
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


def fetch_range_rows(start_date: str, end_date: str) -> list[dict[str, Any]]:
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
            "filter": f"(TRADE_DATE>='{start_date}')(TRADE_DATE<='{end_date}')",
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
        pages = int(result.get("pages") or 0)
        if pages > 0 and pages < 100000 and page >= pages:
            break
        page += 1
    return all_rows


def summarize_top(rows: list[dict[str, Any]], top_n: int = 40) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    totals = defaultdict(float)
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "").strip()
        name = str(r.get("SECURITY_NAME_ABBR") or "").strip()
        net_inflow = r.get("NET_INFLOW")
        if not code or not name or net_inflow in (None, ""):
            continue
        try:
            value = float(net_inflow)
        except (TypeError, ValueError):
            continue
        totals[code] += value
        by_code[code] = {"code": code, "name": name}

    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:top_n]
    out: list[dict[str, Any]] = []
    for idx, (code, total) in enumerate(ranked, start=1):
        name = by_code[code]["name"]
        out.append({"rank": idx, "code": code, "name": name, "net_inflow": total})
    return out


def fmt_money_yi(value: float) -> str:
    return f"{value / 1e8:,.2f}亿"


def fmt_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def code_to_secid(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_market_cap(code: str, timeout: int = 6, retries: int = 2) -> float | None:
    params = {
        "invt": "2",
        "fltt": "2",
        "fields": "f57,f58,f116,f117",
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
                time.sleep(0.3 * attempt)
                continue
            return None
    return None


def attach_market_cap_ratio(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = [str(r["code"]) for r in rows]
    with ThreadPoolExecutor(max_workers=8) as executor:
        caps = list(executor.map(fetch_market_cap, codes))
    market_caps = {code: cap for code, cap in zip(codes, caps)}

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        market_cap = market_caps.get(item["code"])
        item["market_cap"] = market_cap
        item["ratio"] = (item["net_inflow"] / market_cap) if market_cap else None
        out.append(item)
    return out


def build_message(start_date: str, end_date: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"A股近1月资金净流入Top{len(rows)}", f"区间: {start_date} ~ {end_date}", ""]
    for r in rows:
        market_cap = r.get("market_cap")
        ratio = r.get("ratio")
        market_cap_text = fmt_money_yi(float(market_cap)) if market_cap else "N/A"
        ratio_text = fmt_percent(float(ratio)) if ratio is not None else "N/A"
        lines.append(
            f"{r['rank']:>2}. {r['code']} {r['name']}  净流入 {fmt_money_yi(r['net_inflow'])}  "
            f"总市值 {market_cap_text}  占比 {ratio_text}"
        )
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str, thread_id: int | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise RuntimeError(f"telegram error: {data}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A股近1月资金净流入Top40并推送Telegram")
    parser.add_argument("--days", type=int, default=30, help="统计窗口天数，默认30")
    parser.add_argument("--top", type=int, default=40, help="TopN，默认40")
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

    rows = fetch_range_rows(start_date, end_date)
    top_rows = summarize_top(rows, top_n=args.top)
    top_rows_with_ratio = attach_market_cap_ratio(top_rows)
    text = build_message(start_date, end_date, top_rows_with_ratio)

    print(text)
    if args.dry_run:
        return 0

    if not args.telegram_token or not args.telegram_chat_id:
        raise RuntimeError("missing telegram token/chat_id")

    thread_id = args.telegram_thread_id if args.telegram_thread_id > 0 else None
    send_telegram(args.telegram_token, args.telegram_chat_id, text, thread_id=thread_id)
    print("\nTelegram 推送成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
