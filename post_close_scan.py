#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests

QUOTE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class StockRow:
    code: str
    name: str
    price: float
    pct: float
    volume_hand: float
    amount: float
    market_cap: float
    news_title: str = ""
    news_link: str = ""
    news_time: str = ""


def fetch_all_quotes(timeout: int = 20) -> list[dict]:
    rows: list[dict] = []
    base_params = {
        "reportName": "RPT_DMSK_TS_STOCKNEW",
        "columns": "ALL",
        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "CHANGE_RATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
        "quoteColumns": (
            "f3~01~SECURITY_CODE~CHANGE_RATE,"
            "f5~01~SECURITY_CODE~VOLUME,"
            "f20~01~SECURITY_CODE~TOTAL_MARKET_CAP"
        ),
    }

    first = requests.get(QUOTE_URL, params=base_params, headers={"User-Agent": UA}, timeout=timeout)
    first.raise_for_status()
    first_json = first.json()
    pages = int(((first_json.get("result") or {}).get("pages")) or 1)

    for page in range(1, pages + 1):
        params = dict(base_params)
        params["pageNumber"] = str(page)
        resp = requests.get(QUOTE_URL, params=params, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        part = ((payload.get("result") or {}).get("data") or [])
        if not part:
            break
        rows.extend(part)
    return rows


def filter_rows(raw_rows: list[dict], min_pct: float, min_mcap: float) -> list[StockRow]:
    out: list[StockRow] = []
    for r in raw_rows:
        try:
            pct = float(r.get("f3") if r.get("f3") is not None else r.get("CHANGE_RATE"))
            mcap = float(r.get("f20") if r.get("f20") is not None else r.get("TOTAL_MARKET_CAP"))
            price = float(r.get("f2") if r.get("f2") is not None else r.get("CLOSE_PRICE"))
            vol = float(r.get("f5") if r.get("f5") is not None else r.get("VOLUME"))
            amt = float(r.get("f6") if r.get("f6") is not None else 0.0)
            code = str(r.get("f12") or r.get("SECURITY_CODE") or "").strip()
            name = str(r.get("f14") or r.get("SECURITY_NAME_ABBR") or "").strip()
        except (TypeError, ValueError):
            continue
        if not code or not name:
            continue
        if pct > min_pct and mcap > min_mcap:
            out.append(
                StockRow(
                    code=code,
                    name=name,
                    price=price,
                    pct=pct,
                    volume_hand=vol,
                    amount=amt,
                    market_cap=mcap,
                )
            )
    out.sort(key=lambda x: (x.pct, x.market_cap), reverse=True)
    return out


def fetch_news_one(name: str, code: str, timeout: int = 12) -> tuple[str, str, str]:
    query = urllib.parse.quote(f"A股 {name} {code} 最新 新闻")
    url = f"https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        item = root.find("./channel/item")
        if item is None:
            return "", "", ""
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        return title, link, pub
    except Exception:
        return "", "", ""


def fmt_money_yi(v: float) -> str:
    return f"{v / 1e8:,.2f}亿"


def build_markdown(rows: list[StockRow], date_str: str) -> str:
    lines = [
        f"# A股收盘筛选结果（{date_str}）",
        "",
        "筛选条件：当日涨幅 > 5%，总市值 > 100亿",
        "",
        "| 代码 | 名称 | 涨幅 | 成交量(手) | 总市值 | 相关新闻 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        news = f"[{r.news_title}]({r.news_link})" if r.news_title and r.news_link else "暂无"
        lines.append(
            f"| {r.code} | {r.name} | {r.pct:.2f}% | {r.volume_hand:,.0f} | {fmt_money_yi(r.market_cap)} | {news} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-close A-share screener with news")
    parser.add_argument("--min-pct", type=float, default=5.0)
    parser.add_argument("--min-mcap", type=float, default=1e10, help="Total market cap in CNY")
    parser.add_argument("--news", action="store_true", help="Fetch latest news for each stock")
    parser.add_argument("--sleep-ms", type=int, default=120, help="Sleep between news requests")
    parser.add_argument("--output", type=str, default="post_close_result.md")
    args = parser.parse_args()

    raw = fetch_all_quotes()
    rows = filter_rows(raw, args.min_pct, args.min_mcap)

    if args.news:
        for i, row in enumerate(rows, start=1):
            title, link, pub = fetch_news_one(row.name, row.code)
            row.news_title = title
            row.news_link = link
            row.news_time = pub
            if i < len(rows):
                time.sleep(max(args.sleep_ms, 0) / 1000.0)

    today = dt.datetime.now().strftime("%Y-%m-%d")
    md = build_markdown(rows, today)
    out = Path(args.output)
    out.write_text(md, encoding="utf-8")

    print(f"saved: {out.resolve()}")
    print(f"matched: {len(rows)}")
    preview = rows[:10]
    for r in preview:
        print(f"{r.code} {r.name} {r.pct:.2f}% vol={r.volume_hand:,.0f} mcap={fmt_money_yi(r.market_cap)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
