#!/usr/bin/env python3
"""A-share intraday alert tool.

Features:
1) Alert stocks with intraday gain > 5%
2) Alert stocks with turnover > 100 million CNY

Data source: Eastmoney quote API
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

STATE_FILE = Path(".alert_state.json")


class AlertPusher:
    def __init__(
        self,
        webhook: str | None = None,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        telegram_thread_id: int | None = None,
        dry_run: bool = False,
    ) -> None:
        self.webhook = webhook
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.telegram_thread_id = telegram_thread_id
        self.dry_run = dry_run

    def push(self, title: str, lines: Iterable[str]) -> None:
        lines = list(lines)
        if not lines:
            return

        text = f"{title}\n" + "\n".join(lines)
        print("\n" + "=" * 80)
        print(text)
        print("=" * 80 + "\n")

        if self.dry_run:
            return

        if self.webhook:
            payload = {
                "msgtype": "text",
                "text": {
                    "content": text,
                },
            }
            try:
                import requests

                resp = requests.post(self.webhook, json=payload, timeout=8)
                resp.raise_for_status()
            except Exception as exc:
                print(f"[WARN] webhook push failed: {exc}", file=sys.stderr)

        if self.telegram_token and self.telegram_chat_id:
            tg_url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            tg_payload = {
                "chat_id": self.telegram_chat_id,
                "text": text,
            }
            if self.telegram_thread_id is not None:
                tg_payload["message_thread_id"] = self.telegram_thread_id
            try:
                import requests

                resp = requests.post(tg_url, json=tg_payload, timeout=8)
                resp.raise_for_status()
                result = resp.json()
                if not result.get("ok", False):
                    print(f"[WARN] telegram push failed: {result}", file=sys.stderr)
            except Exception as exc:
                print(f"[WARN] telegram push failed: {exc}", file=sys.stderr)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def in_trading_window() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False

    hhmm = now.hour * 100 + now.minute
    return (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)


def fetch_spot_df():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "missing dependency: requests. Please run `pip install -r requirements.txt`."
        ) from exc

    url = "https://82.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": 6000,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f6",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, params=params, headers=headers, timeout=8)
    resp.raise_for_status()
    payload = resp.json()
    items = ((payload.get("data") or {}).get("diff") or [])
    rows: list[dict] = []
    for it in items:
        code = str(it.get("f12") or "").strip()
        name = str(it.get("f14") or "").strip()
        price = it.get("f2")
        pct = it.get("f3")
        amount = it.get("f6")
        if not code or not name:
            continue
        if price in (None, "-") or pct in (None, "-") or amount in (None, "-"):
            continue
        try:
            rows.append(
                {
                    "代码": code,
                    "名称": name,
                    "最新价": float(price),
                    "涨跌幅": float(pct),
                    "成交额": float(amount),
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def filter_rules(
    df,
    min_pct: float,
    min_amount: float,
):
    up_df = sorted([r for r in df if r["涨跌幅"] > min_pct], key=lambda x: x["涨跌幅"], reverse=True)
    amount_df = sorted(
        [r for r in df if r["成交额"] > min_amount], key=lambda x: x["成交额"], reverse=True
    )
    return up_df, amount_df


def stock_rows(df, amount_as_wan: bool = True, top_n: int = 30) -> list[str]:
    lines: list[str] = []
    for row in df[:top_n]:
        code = row["代码"]
        name = row["名称"]
        price = row["最新价"]
        pct = row["涨跌幅"]
        amount = row["成交额"]
        if amount_as_wan:
            amount_text = f"{amount / 1e4:,.2f} 万元"
        else:
            amount_text = f"{amount:,.0f} 元"
        lines.append(f"{code} {name} | 价: {price} | 涨跌幅: {pct:.2f}% | 成交额: {amount_text}")
    return lines


def dedupe_by_state(df, state_set: set[str]):
    if not df:
        return df
    return [r for r in df if str(r["代码"]) not in state_set]


def run_once(args: argparse.Namespace, pusher: AlertPusher, state: dict) -> None:
    key = today_key()
    state.setdefault(key, {"up": [], "amount": []})

    try:
        df = fetch_spot_df()
    except Exception as exc:
        print(f"[ERROR] fetch market data failed: {exc}", file=sys.stderr)
        return

    up_df, amount_df = filter_rules(df, args.min_pct, args.min_amount)

    up_old = set(state[key].get("up", []))
    amt_old = set(state[key].get("amount", []))

    up_new = dedupe_by_state(up_df, up_old)
    amt_new = dedupe_by_state(amount_df, amt_old)

    if up_new:
        title = f"[A股提醒] 日内涨幅 > {args.min_pct:.2f}% ({datetime.now():%H:%M:%S})"
        pusher.push(title, stock_rows(up_new, top_n=args.max_push))
        state[key]["up"] = sorted(up_old.union({str(r["代码"]) for r in up_new}))

    if amt_new:
        title = f"[A股提醒] 成交额 > {args.min_amount:,.0f} 元 ({datetime.now():%H:%M:%S})"
        pusher.push(title, stock_rows(amt_new, top_n=args.max_push))
        state[key]["amount"] = sorted(amt_old.union({str(r["代码"]) for r in amt_new}))

    if not up_new and not amt_new:
        print(f"[{datetime.now():%H:%M:%S}] no new alerts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A股实时条件推送")
    parser.add_argument("--min-pct", type=float, default=5.0, help="涨跌幅阈值(%%)，默认5")
    parser.add_argument(
        "--min-amount",
        type=float,
        default=1e8,
        help="成交额阈值(元)，默认1e8",
    )
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔(秒)，默认60")
    parser.add_argument("--max-push", type=int, default=30, help="每次最多推送条数，默认30")
    parser.add_argument(
        "--webhook",
        type=str,
        default=os.getenv("WECOM_WEBHOOK", ""),
        help="企业微信机器人Webhook，不填则仅终端输出",
    )
    parser.add_argument(
        "--telegram-token",
        type=str,
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token，不填则不走 Telegram 推送",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=str,
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="Telegram Chat ID，不填则不走 Telegram 推送",
    )
    parser.add_argument(
        "--telegram-thread-id",
        type=int,
        default=int(os.getenv("TELEGRAM_THREAD_ID", "0") or 0),
        help="Telegram 话题 message_thread_id，可选",
    )
    parser.add_argument("--once", action="store_true", help="只执行一次")
    parser.add_argument("--dry-run", action="store_true", help="仅终端输出，不调用webhook")
    parser.add_argument(
        "--ignore-trading-hours",
        action="store_true",
        help="忽略交易时段限制，任何时间都轮询",
    )
    return parser.parse_args()


def cleanup_state(state: dict) -> dict:
    # Keep only recent 7 days to avoid unbounded growth
    keys = sorted(state.keys(), reverse=True)
    keep = set(keys[:7])
    return {k: v for k, v in state.items() if k in keep}


def main() -> int:
    args = parse_args()
    tg_thread_id = args.telegram_thread_id if args.telegram_thread_id > 0 else None
    pusher = AlertPusher(
        webhook=args.webhook or None,
        telegram_token=args.telegram_token or None,
        telegram_chat_id=args.telegram_chat_id or None,
        telegram_thread_id=tg_thread_id,
        dry_run=args.dry_run,
    )
    state = cleanup_state(load_state(STATE_FILE))

    if args.once:
        run_once(args, pusher, state)
        save_state(STATE_FILE, state)
        return 0

    print("A股监控启动... 按 Ctrl+C 停止")
    print(
        f"规则: 涨幅>{args.min_pct:.2f}% | 成交额>{args.min_amount:,.0f}元 | 间隔{args.interval}s"
    )

    try:
        while True:
            if args.ignore_trading_hours or in_trading_window():
                run_once(args, pusher, state)
                save_state(STATE_FILE, state)
            else:
                print(f"[{datetime.now():%H:%M:%S}] 非交易时段，跳过")
            time.sleep(max(args.interval, 3))
    except KeyboardInterrupt:
        print("\n已停止")
        save_state(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
