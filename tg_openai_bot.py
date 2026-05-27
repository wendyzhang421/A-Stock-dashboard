#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

from telegram_read_updates import extract_message, fetch_updates, load_offset, save_offset


DEFAULT_OFFSET_FILE = Path(".telegram_bot_offset")
DEFAULT_MEMORY_FILE = Path(".telegram_bot_memory.json")
OPENAI_API_URL = "https://api.openai.com/v1/responses"
DEFAULT_SYSTEM_PROMPT = (
    "You are a concise, helpful assistant chatting with the user on Telegram. "
    "Answer in Chinese unless the user asks for another language. "
    "Keep replies clear and practical."
)


def load_memory(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, list[dict[str, str]]] = {}
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        turns: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                turns.append({"role": role, "content": content})
        if turns:
            cleaned[str(key)] = turns
    return cleaned


def save_memory(path: Path, memory: dict[str, list[dict[str, str]]]) -> None:
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_text_from_response(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                value = str(content.get("text") or "").strip()
                if value:
                    parts.append(value)
    return "\n".join(parts).strip()


def build_input(system_prompt: str, history: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
    items = [{"role": "system", "content": system_prompt}]
    items.extend(history)
    items.append({"role": "user", "content": user_text})
    return items


def ask_openai(
    api_key: str,
    model: str,
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
    timeout: int,
) -> str:
    payload = {
        "model": model,
        "input": build_input(system_prompt, history, user_text),
    }
    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text = extract_text_from_response(data)
    if not text:
        raise RuntimeError(f"OpenAI returned no text: {data}")
    return text


def send_telegram(
    token: str,
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if thread_id:
        payload["message_thread_id"] = thread_id
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok", False):
        raise RuntimeError(f"telegram sendMessage failed: {data}")


def send_typing(token: str, chat_id: str | int, thread_id: int | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            json=payload,
            timeout=10,
        )
    except requests.RequestException:
        return


def trim_history(history: list[dict[str, str]], max_turns: int) -> list[dict[str, str]]:
    if max_turns <= 0:
        return []
    return history[-max_turns * 2 :]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram <-> OpenAI 自动回复机器人")
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token，默认读取 TELEGRAM_BOT_TOKEN",
    )
    parser.add_argument(
        "--openai-api-key",
        default=os.getenv("OPENAI_API_KEY", ""),
        help="OpenAI API Key，默认读取 OPENAI_API_KEY",
    )
    parser.add_argument(
        "--allowed-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="只回复这个 chat_id；默认读取 TELEGRAM_CHAT_ID",
    )
    parser.add_argument(
        "--telegram-thread-id",
        type=int,
        default=int(os.getenv("TELEGRAM_THREAD_ID", "0") or 0),
        help="Telegram 话题 ID，可选",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-5"),
        help="OpenAI model，默认 gpt-5，可用 OPENAI_MODEL 覆盖",
    )
    parser.add_argument(
        "--system-prompt",
        default=os.getenv("TG_BOT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        help="系统提示词，可用 TG_BOT_SYSTEM_PROMPT 覆盖",
    )
    parser.add_argument(
        "--offset-file",
        type=Path,
        default=DEFAULT_OFFSET_FILE,
        help="保存 Telegram update offset 的文件",
    )
    parser.add_argument(
        "--memory-file",
        type=Path,
        default=DEFAULT_MEMORY_FILE,
        help="保存对话上下文的 JSON 文件",
    )
    parser.add_argument("--timeout", type=int, default=20, help="Telegram 长轮询秒数")
    parser.add_argument("--limit", type=int, default=100, help="每次读取的 update 上限")
    parser.add_argument(
        "--openai-timeout", type=int, default=120, help="OpenAI 请求超时秒数"
    )
    parser.add_argument(
        "--max-turns", type=int, default=8, help="每个 chat 保留的历史轮数，默认 8"
    )
    parser.add_argument("--follow", action="store_true", help="持续轮询并自动回复")
    parser.add_argument("--skip-history", action="store_true", help="首次运行跳过历史消息")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不回 Telegram")
    return parser.parse_args()


def should_reply(msg: dict[str, Any], allowed_chat_id: str) -> bool:
    sender = msg.get("from") or {}
    if sender.get("is_bot"):
        return False
    chat = msg.get("chat") or {}
    if allowed_chat_id and str(chat.get("id")) != str(allowed_chat_id):
        return False
    if not str(msg.get("text") or "").strip():
        return False
    return True


def handle_command(
    text: str,
    chat_key: str,
    memory: dict[str, list[dict[str, str]]],
) -> str | None:
    cmd = text.strip().lower()
    if cmd == "/reset":
        memory.pop(chat_key, None)
        return "上下文已清空，我们可以重新开始。"
    if cmd in {"/start", "/help"}:
        return "直接发消息给我就行。发送 /reset 可以清空当前聊天上下文。"
    return None


def process_updates(args: argparse.Namespace) -> int:
    if not args.telegram_token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")
    if not args.openai_api_key and not args.dry_run:
        raise RuntimeError("missing OPENAI_API_KEY")

    offset = load_offset(args.offset_file)
    if offset is None and args.skip_history:
        latest = fetch_updates(args.telegram_token, None, timeout=1, limit=args.limit)
        if latest:
            offset = max(int(x["update_id"]) for x in latest) + 1
            save_offset(args.offset_file, offset)
            print(f"initialized offset={offset}, history skipped")
        else:
            print("no history to skip")
        return 0

    memory = load_memory(args.memory_file)

    while True:
        updates = fetch_updates(args.telegram_token, offset, timeout=args.timeout, limit=args.limit)
        if not updates:
            if not args.follow:
                print("no new updates")
                break
            continue

        for upd in updates:
            update_id = int(upd["update_id"])
            extracted = extract_message(upd)
            if not extracted:
                offset = update_id + 1
                continue

            _, msg = extracted
            offset = update_id + 1
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            chat_key = str(chat_id)
            text = str(msg.get("text") or "").strip()

            if not should_reply(msg, args.allowed_chat_id):
                continue

            reply = handle_command(text, chat_key, memory)
            if reply is None:
                history = trim_history(memory.get(chat_key, []), args.max_turns)
                if not args.dry_run:
                    send_typing(
                        args.telegram_token,
                        chat_id,
                        thread_id=args.telegram_thread_id if args.telegram_thread_id > 0 else None,
                    )
                    reply = ask_openai(
                        args.openai_api_key,
                        args.model,
                        args.system_prompt,
                        history,
                        text,
                        timeout=args.openai_timeout,
                    )
                else:
                    reply = f"[dry-run] 收到: {text}"
                history.extend(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": reply},
                    ]
                )
                memory[chat_key] = trim_history(history, args.max_turns)

            print(f"reply chat:{chat_id} | {reply[:120]}")
            if not args.dry_run:
                send_telegram(
                    args.telegram_token,
                    chat_id,
                    reply,
                    reply_to_message_id=msg.get("message_id"),
                    thread_id=args.telegram_thread_id if args.telegram_thread_id > 0 else None,
                )

        save_offset(args.offset_file, offset)
        save_memory(args.memory_file, memory)

        if not args.follow:
            break

    return 0


def main() -> int:
    args = parse_args()
    try:
        return process_updates(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else str(exc)
        print(f"[ERROR] HTTP error: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
