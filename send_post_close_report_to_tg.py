#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_MESSAGE_LEN = 3500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发送收盘强势股日报到 Telegram")
    parser.add_argument(
        "--input",
        default="post_close_result_2026-04-03.md",
        help="要发送的 Markdown 文件路径",
    )
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token，默认读取环境变量 TELEGRAM_BOT_TOKEN",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="Telegram Chat ID，默认读取环境变量 TELEGRAM_CHAT_ID",
    )
    parser.add_argument(
        "--telegram-thread-id",
        type=int,
        default=int(os.getenv("TELEGRAM_THREAD_ID", "0") or 0),
        help="Telegram 话题 ID，可选",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不实际发送")
    return parser.parse_args()


def format_news_cell(cell: str) -> str:
    cell = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1\n\2", cell)
    return cell.replace("；", "\n")


def markdown_to_text(content: str) -> str:
    lines: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("# "):
            lines.append(line[2:].strip())
            continue
        if line.startswith("## "):
            lines.append(line[3:].strip())
            continue
        if set(line) <= {"|", "-", ":", " "}:
            continue
        if line.startswith("|") and line.endswith("|"):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if parts and parts[0] == "股票代码":
                continue
            if len(parts) >= 7:
                lines.append(
                    f"{parts[0]} {parts[1]} | 涨跌幅: {parts[2]} | 成交量: {parts[3]} | 成交额: {parts[4]} | 总市值: {parts[5]}"
                )
                lines.append(format_news_cell(parts[6]))
                lines.append("")
                continue
        lines.append(line.replace("**", ""))
    return "\n".join(lines).strip()


def split_chunks(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        piece = block.strip()
        if not piece:
            continue
        piece_len = len(piece) + (2 if current else 0)
        if current and current_len + piece_len > max_len:
            chunks.append("\n\n".join(current))
            current = [piece]
            current_len = len(piece)
        else:
            current.append(piece)
            current_len += piece_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def send_telegram(token: str, chat_id: str, text: str, thread_id: int | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok", False):
        raise RuntimeError(f"telegram error: {data}")


def main() -> int:
    args = parse_args()
    if not args.dry_run and (not args.telegram_token or not args.telegram_chat_id):
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    path = Path(args.input)
    if not path.exists():
        raise FileNotFoundError(f"report not found: {path}")

    content = path.read_text(encoding="utf-8")
    text = markdown_to_text(content)
    chunks = split_chunks(text)

    for idx, chunk in enumerate(chunks, start=1):
        title = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        message = f"{title}{chunk}"
        if args.dry_run:
            print(message)
            print("\n" + "=" * 80 + "\n")
            continue
        try:
            send_telegram(
                args.telegram_token,
                args.telegram_chat_id,
                message,
                thread_id=args.telegram_thread_id if args.telegram_thread_id > 0 else None,
            )
        except urllib.error.URLError as exc:
            raise RuntimeError(f"telegram request failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
