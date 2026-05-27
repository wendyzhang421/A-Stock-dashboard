#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


DEFAULT_STATE_FILE = Path(".telegram_updates_offset")


def load_offset(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return int(content)
    except (OSError, ValueError):
        return None


def save_offset(path: Path, offset: int) -> None:
    path.write_text(str(offset), encoding="utf-8")


def fetch_updates(token: str, offset: int | None, timeout: int, limit: int) -> list[dict[str, Any]]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params: dict[str, Any] = {
        "timeout": timeout,
        "limit": limit,
        "allowed_updates": json.dumps(
            ["message", "edited_message", "channel_post", "edited_channel_post"]
        ),
    }
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=timeout + 10)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"telegram getUpdates failed: {payload}")
    return payload.get("result") or []


def display_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "-"
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip()
    name = " ".join([x for x in [first, last] if x]).strip()
    if name and username:
        return f"{name}(@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return str(user.get("id") or "-")


def chat_name(chat: dict[str, Any] | None) -> str:
    if not chat:
        return "-"
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    return display_name(chat)


def extract_message(update: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        data = update.get(key)
        if isinstance(data, dict):
            return key, data
    return None


def to_local_time(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def text_of_message(msg: dict[str, Any]) -> str:
    text = msg.get("text") or msg.get("caption") or ""
    text = str(text).replace("\n", "\\n").strip()
    if not text:
        return "<non-text message>"
    if len(text) > 160:
        return text[:157] + "..."
    return text


def print_updates(updates: list[dict[str, Any]]) -> None:
    if not updates:
        print("no new updates")
        return

    for upd in updates:
        update_id = upd.get("update_id")
        extracted = extract_message(upd)
        if not extracted:
            print(f"[{update_id}] unsupported update type")
            continue
        kind, msg = extracted
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        when = to_local_time(msg.get("date"))
        chat_id = chat.get("id")
        c_name = chat_name(chat)
        sender_name = display_name(sender)
        text = text_of_message(msg)
        print(
            f"[{update_id}] {when} | {kind} | chat:{chat_id}({c_name}) | from:{sender_name} | {text}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读取 Telegram Bot 入站消息(getUpdates)")
    parser.add_argument(
        "--telegram-token",
        type=str,
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token，默认读取环境变量 TELEGRAM_BOT_TOKEN",
    )
    parser.add_argument("--timeout", type=int, default=20, help="getUpdates 长轮询秒数，默认20")
    parser.add_argument("--limit", type=int, default=100, help="每次最多读取条数，默认100")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="保存offset的文件路径，默认 .telegram_updates_offset",
    )
    parser.add_argument("--no-state", action="store_true", help="不读写 offset 文件")
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="首次运行时跳过历史消息，只从当前之后的新消息开始",
    )
    parser.add_argument("--follow", action="store_true", help="持续监听新消息")
    parser.add_argument(
        "--poll-interval", type=float, default=1.0, help="follow 模式下循环间隔秒数，默认1"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.telegram_token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")

    use_state = not args.no_state
    offset = load_offset(args.state_file) if use_state else None

    if use_state and offset is None and args.skip_history:
        latest = fetch_updates(args.telegram_token, None, timeout=1, limit=args.limit)
        if latest:
            offset = max(int(x["update_id"]) for x in latest) + 1
            save_offset(args.state_file, offset)
            print(f"initialized offset={offset}, history skipped")
        else:
            print("no history to skip")
        return 0

    while True:
        updates = fetch_updates(args.telegram_token, offset, timeout=args.timeout, limit=args.limit)
        print_updates(updates)

        if updates:
            offset = max(int(x["update_id"]) for x in updates) + 1
            if use_state:
                save_offset(args.state_file, offset)

        if not args.follow:
            break
        time.sleep(max(args.poll_interval, 0.1))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
