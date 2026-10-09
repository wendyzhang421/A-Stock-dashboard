#!/usr/bin/env python3
"""Build and send the recurring WendysAlpha follow-account digest."""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from PIL import Image, ImageDraw, ImageFont
import requests


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
CHANNEL = "wendysalpha"
PREVIEW_URL = f"https://t.me/s/{CHANNEL}"
RESPONSES_URL = "https://api.x.ai/v1/responses"
X_HANDLE_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?:[/?#]|$)",
    re.I,
)
FOLLOW_RE = re.compile(r"^(.+?)\s+关注了\s+(.+?)\s*$")
MUTUAL_RE = re.compile(r"你关注的\s*(\d+)\s*个用户")
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
FONT_REGULAR = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
)
FONT_BOLD = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)
NAVY = "#17365D"
HEADER_BLUE = "#4F81BD"
TEXT = "#1F1F1F"
MUTED = "#5B6573"
PROJECT_FILL = "#E2F0D9"
PERSON_FILL = "#DDEBF7"
ROW_BLUE = "#EAF2F8"
ROW_WHITE = "#FFFFFF"
GRID = "#D9E2F3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成并发送 @wendysalpha 新关注账号汇总")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--output-dir", default="reports/wendysalpha")
    parser.add_argument("--state-file", default=".state/wendysalpha.json")
    parser.add_argument("--seed-file", default="config/wendysalpha_state_seed.json")
    parser.add_argument("--now", help="ISO 时间；默认当前时间")
    parser.add_argument("--dry-run", action="store_true", help="生成文件但不发送，也不推进状态")
    parser.add_argument(
        "--offline-classify",
        action="store_true",
        help="仅用于本地布局测试；正式运行必须使用 xAI 公开资料研究",
    )
    parser.add_argument("--max-pages", type=int, default=30)
    return parser.parse_args()


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(timezone.utc)


def now_utc(value: str | None) -> datetime:
    return parse_iso(value) if value else datetime.now(timezone.utc)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取状态文件 {path}: {exc}") from exc
    return data if isinstance(data, dict) else copy(default)


def load_state(state_path: Path, seed_path: Path) -> dict[str, Any]:
    base = {
        "last_message_id": 0,
        "seen_handles": [],
        "updated_at_utc": "",
    }
    seed = load_json(seed_path, base)
    state = load_json(state_path, seed)
    state["last_message_id"] = int(state.get("last_message_id") or 0)
    state["seen_handles"] = sorted(
        {
            normalize_handle(item)
            for item in state.get("seen_handles", [])
            if normalize_handle(item)
        }
    )
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = os.getenv("ASTOCK_TRUST_ENV", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
    )
    return session


def parse_preview_page(html_text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    messages: list[dict[str, Any]] = []
    for wrapper in soup.select(".tgme_widget_message_wrap"):
        message_node = wrapper.select_one(".js-widget_message")
        time_node = wrapper.select_one("time[datetime]")
        if message_node is None or time_node is None:
            continue
        post_ref = str(message_node.get("data-post") or "")
        if "/" not in post_ref:
            continue
        try:
            message_id = int(post_ref.rsplit("/", 1)[-1])
            published_at = parse_iso(str(time_node.get("datetime") or ""))
        except (ValueError, TypeError):
            continue
        text_node = wrapper.select_one(".tgme_widget_message_text")
        message_text = text_node.get_text("\n", strip=True) if text_node else ""
        links: list[str] = []
        for anchor in wrapper.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if href.startswith("http") and href not in links:
                links.append(href)
        messages.append(
            {
                "message_id": message_id,
                "published_at_utc": published_at,
                "post_url": f"https://t.me/{post_ref}",
                "text": message_text,
                "links": links,
            }
        )
    return messages


def fetch_messages(
    session: requests.Session,
    last_message_id: int,
    cutoff: datetime,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int]:
    collected: dict[int, dict[str, Any]] = {}
    before: int | None = None
    newest_id = last_message_id
    for _ in range(max_pages):
        params = {"before": before} if before else None
        response = session.get(PREVIEW_URL, params=params, timeout=45)
        response.raise_for_status()
        page = parse_preview_page(response.text)
        if not page:
            break
        for item in page:
            collected[item["message_id"]] = item
            newest_id = max(newest_id, int(item["message_id"]))
        oldest = min(int(item["message_id"]) for item in page)
        oldest_time = min(item["published_at_utc"] for item in page)
        if last_message_id and oldest <= last_message_id:
            break
        if not last_message_id and oldest_time < cutoff:
            break
        if before == oldest:
            break
        before = oldest
    messages = [
        item
        for item in collected.values()
        if int(item["message_id"]) > last_message_id
    ]
    messages.sort(key=lambda item: int(item["message_id"]))
    return messages, newest_id


def normalize_handle(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    handle = text if text.startswith("@") else f"@{text}"
    if not re.fullmatch(r"@[A-Za-z0-9_]{1,15}", handle):
        return ""
    return handle.lower()


def display_handle(value: object) -> str:
    text = str(value or "").strip()
    return text if text.startswith("@") else f"@{text}"


def handle_from_message(message: dict[str, Any]) -> str:
    candidates = list(message.get("links") or [])
    candidates.extend(X_HANDLE_RE.findall(str(message.get("text") or "")))
    for candidate in candidates:
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", str(candidate)):
            return display_handle(candidate)
        match = X_HANDLE_RE.search(str(candidate))
        if match:
            return display_handle(match.group(1))
    return ""


def extract_bio(lines: list[str]) -> str:
    capture: list[str] = []
    active = False
    for line in lines:
        if line.startswith("用户简介:") or line.startswith("用户简介："):
            active = True
            value = re.split(r"用户简介[:：]", line, maxsplit=1)[-1].strip()
            if value:
                capture.append(value)
            continue
        if not active:
            continue
        if MUTUAL_RE.search(line) or X_HANDLE_RE.search(line):
            break
        capture.append(line)
    return "\n".join(capture).strip()


def parse_follow_message(message: dict[str, Any]) -> dict[str, Any] | None:
    raw = str(message.get("text") or "").strip()
    handle = handle_from_message(message)
    if not raw or not handle:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    follower = ""
    name = handle.lstrip("@")
    for line in lines:
        match = FOLLOW_RE.match(line)
        if match:
            follower = match.group(1).strip()
            name = match.group(2).strip()
            break
    if not follower:
        return None
    mutual_match = MUTUAL_RE.search(raw)
    x_url = f"https://x.com/{handle.lstrip('@')}"
    return {
        "message_id": int(message["message_id"]),
        "post_url": str(message["post_url"]),
        "published_at_utc": message["published_at_utc"],
        "raw": raw,
        "handle": handle,
        "handle_key": normalize_handle(handle),
        "follower": follower,
        "name": name,
        "bio": extract_bio(lines),
        "mutual": int(mutual_match.group(1)) if mutual_match else None,
        "x_url": x_url,
    }


def collect_new_accounts(
    messages: list[dict[str, Any]],
    seen_handles: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    duplicates: list[dict[str, Any]] = []
    for message in messages:
        record = parse_follow_message(message)
        if record is None:
            continue
        key = record["handle_key"]
        if key in seen_handles:
            duplicates.append({**record, "duplicate_reason": "跨轮次重复"})
            continue
        by_handle[key].append(record)
    accounts: list[dict[str, Any]] = []
    for records in by_handle.values():
        records.sort(key=lambda item: int(item["message_id"]))
        primary = records[0]
        primary["duplicate_count"] = len(records)
        primary["all_message_ids"] = [int(item["message_id"]) for item in records]
        accounts.append(primary)
        for repeated in records[1:]:
            duplicates.append({**repeated, "duplicate_reason": "本轮重复"})
    accounts.sort(key=lambda item: int(item["message_id"]))
    duplicates.sort(key=lambda item: int(item["message_id"]))
    return accounts, duplicates


def contains_chinese_summary(value: object, minimum: int = 8) -> bool:
    """Return whether a model summary contains enough Chinese to publish."""
    text = str(value or "").strip()
    return len(CHINESE_RE.findall(text)) >= minimum


def fallback_project_intro(account: dict[str, Any]) -> str:
    """Turn public profile signals into a short Chinese project explanation.

    This path is deliberately extractive rather than generative: it never pastes
    an English bio into the Telegram image.  It gives the reader a useful Chinese
    category-level explanation when external AI research is unavailable.
    """
    text = f"{account.get('name', '')} {account.get('bio', '')}".lower()
    categories: tuple[tuple[tuple[str, ...], str], ...] = (
        (
            ("stablecoin", "payments", "payment", "payfi", "remittance"),
            "一个加密支付或稳定币项目，提供链上结算、转账及相关金融服务。",
        ),
        (
            ("rwa", "real world asset", "tokenized", "tokenization"),
            "一个现实资产代币化项目，将传统资产引入链上发行、交易或管理。",
        ),
        (
            ("perpetual", "perps", "derivatives", "prediction market"),
            "一个链上交易项目，提供衍生品、永续合约或预测市场等交易服务。",
        ),
        (
            ("defi", "dex", "swap", "lending", "borrow", "yield", "liquidity", "amm"),
            "一个去中心化金融项目，提供交易、借贷、收益或流动性相关服务。",
        ),
        (
            ("wallet", "custody", "account abstraction", "smart account"),
            "一个加密钱包或账户基础设施项目，帮助用户管理资产并完成链上交互。",
        ),
        (
            ("security", "audit", "exploit", "bug bounty", "threat"),
            "一个区块链安全项目，提供审计、风险监控或漏洞防护服务。",
        ),
        (
            ("privacy", "zero knowledge", "zero-knowledge", "zkp", "zk "),
            "一个隐私与零知识技术项目，为链上应用提供隐私保护或扩容能力。",
        ),
        (
            ("oracle", "analytics", "onchain data", "on-chain data", "data platform", "terminal"),
            "一个链上数据项目，提供数据查询、分析、行情或研究工具。",
        ),
        (
            ("artificial intelligence", " ai ", "ai agent", "agents", "machine learning", "llm"),
            "一个人工智能项目，提供智能代理、模型应用或相关开发工具。",
        ),
        (
            ("developer", "developers", "sdk", "api", "infrastructure", "infra", "rpc", "node", "open source"),
            "一个区块链开发基础设施项目，为开发者提供接口、工具或底层服务。",
        ),
        (
            ("layer 2", "layer2", "rollup", "blockchain", "network", "modular", "protocol"),
            "一个区块链网络或协议项目，提供链上基础设施、扩容或生态服务。",
        ),
        (
            ("exchange", "trading", "trade", "marketplace", "broker"),
            "一个数字资产交易平台，提供交易、市场撮合或相关金融工具。",
        ),
        (
            ("nft", "collectible", "gaming", "game", "metaverse"),
            "一个链游或数字收藏项目，围绕游戏体验、虚拟资产和社区运营展开。",
        ),
        (
            ("memecoin", "meme coin", "meme token", " meme "),
            "一个加密迷因项目，主要围绕代币叙事与社区传播运营。",
        ),
        (
            ("venture", "capital", "investment fund", "accelerator", "incubator", "launchpad"),
            "一个投资或项目孵化机构，主要支持早期科技与加密项目。",
        ),
        (
            ("media", "news", "newsletter", "podcast", "community", "creator"),
            "一个行业媒体或社区项目，主要提供资讯、研究内容与社群服务。",
        ),
        (
            ("social", "consumer app", "messaging", "identity"),
            "一个面向用户的链上应用，侧重社交、身份或消费级产品体验。",
        ),
    )
    for keywords, intro in categories:
        if any(keyword in text for keyword in keywords):
            return intro
    return "一个加密行业项目或组织，围绕其产品、社区或生态开展服务，具体业务仍需进一步核验。"


def fallback_person_intro(account: dict[str, Any]) -> str:
    """Create a Chinese professional/content-direction summary for a person."""
    text = f"{account.get('name', '')} {account.get('bio', '')}".lower()
    categories: tuple[tuple[tuple[str, ...], str], ...] = (
        (
            ("founder", "co-founder", "building ", "builder", "entrepreneur"),
            "创业者或项目建设者，主要分享产品建设、行业观察与个人动态。",
        ),
        (
            ("engineer", "developer", "software", "cto ", "coding", "programmer"),
            "软件开发者或工程师，主要分享技术开发、加密应用与项目建设内容。",
        ),
        (
            ("researcher", "research", "analyst", "economist", "scientist"),
            "研究员或分析师，主要关注加密市场、技术趋势与行业研究。",
        ),
        (
            ("investor", "venture", "partner @", "capital", "portfolio"),
            "投资人或机构从业者，主要关注早期项目、市场趋势与投资观点。",
        ),
        (
            ("trader", "trading", "market thoughts", "markets", "macro"),
            "交易者或市场观察者，主要分享加密市场、交易与宏观观点。",
        ),
        (
            ("designer", "design", "artist", "creative"),
            "设计或创意从业者，主要分享产品设计、数字艺术与创作内容。",
        ),
        (
            ("growth", "marketing", "community", "ecosystem", "bd "),
            "市场、增长或社区从业者，主要分享项目运营与生态发展内容。",
        ),
        (
            ("journalist", "writer", "i write", "newsletter", "podcast", "media"),
            "媒体或内容创作者，主要分享行业资讯、访谈与个人观点。",
        ),
    )
    for keywords, intro in categories:
        if any(keyword in text for keyword in keywords):
            return intro
    return "个人账号，主要分享其工作经历、行业观点与日常动态，具体职业定位仍需进一步核验。"


def fallback_classification(account: dict[str, Any]) -> dict[str, Any]:
    text = f"{account.get('name', '')} {account.get('bio', '')}".lower()
    display_name = str(account.get("name") or "").strip()
    personal_terms = (
        "founder",
        "co-founder",
        "builder",
        "engineer",
        "researcher",
        "investor",
        "trader",
        "designer",
        "developer",
        "ceo ",
        "cto ",
        "i build",
        "i write",
        "partner @",
        "building ",
        "professional ",
        "opinions are my own",
        "views are my own",
        "head of ",
        "intern",
        "survivor",
        "former:",
        "prev:",
        "founding team",
        "market thoughts",
        "eir ",
        "个人",
    )
    project_terms = (
        "official",
        "protocol",
        "platform",
        "network",
        "labs",
        "studio",
        "foundation",
        "community",
        "marketplace",
        "app",
        "token",
        "coin",
        "defi",
        "dao",
        "powered by",
        "open source",
    )
    personal_score = sum(term in text for term in personal_terms)
    if any(
        term in text
        for term in (
            "partner @",
            "founder @",
            "co-founder @",
            "opinions are my own",
            "views are my own",
            "head of ",
            "intern",
            "survivor",
            "former:",
            "prev:",
            "founding team",
            "market thoughts",
            "eir ",
        )
    ):
        personal_score += 3
    if re.match(r"^(?:dr\.?|mr\.?|ms\.?|mrs\.?)\s+", display_name, re.I):
        personal_score += 4
    if not str(account.get("bio") or "").strip() and len(display_name.split()) >= 2:
        personal_score += 2
    project_score = sum(term in text for term in project_terms)
    account_type = "个人" if personal_score > project_score else "项目"
    if account_type == "个人":
        intro = fallback_person_intro(account)
        basis = "依据账号名称与公开简介进行初步判断。"
    else:
        intro = fallback_project_intro(account)
        basis = "依据品牌化账号名称与公开简介进行初步判断。"
    return {
        "handle": account["handle"],
        "type": account_type,
        "intro": intro,
        "basis": basis,
        "confidence": "低",
        "risk": "仅基于公开简介进行初步判断，尚未完成外部资料交叉核验。",
        "sources": [account["x_url"]],
    }


def response_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "output_text" and isinstance(value.get("text"), str):
                parts.append(value["text"])
                return
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload.get("output", []))
    if not parts and isinstance(payload.get("output_text"), str):
        parts.append(payload["output_text"])
    return "\n".join(parts).strip()


def parse_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^\x60\x60\x60(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\x60\x60\x60$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end < start:
        raise ValueError("模型输出中没有 JSON 数组")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, list):
        raise ValueError("模型输出不是 JSON 数组")
    return [item for item in value if isinstance(item, dict)]


def classify_batch(
    session: requests.Session,
    batch: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> dict[str, dict[str, Any]]:
    inputs = [
        {
            "handle": item["handle"],
            "display_name": item["name"],
            "bio": item["bio"],
            "telegram_post": item["post_url"],
        }
        for item in batch
    ]
    prompt = f"""
你是一名谨慎的公开资料研究员。请研究下面这些 X 账号，判断账号主体是“个人”还是“项目”。

要求：
1. 优先使用 X Search 核验账号近期内容和个人资料，再用 Web Search 核验官网、文档、GitHub、机构页面等公开来源。
2. 项目包括产品、协议、代币、媒体、社区、公司和组织。个人是以自然人为主体的账号。
3. intro 必须用自然、简洁的简体中文重新归纳：项目用一句话说明它具体做什么；个人用一句话概括内容方向或职业定位。
4. intro 建议 20–55 个汉字；不得复制或粘贴英文 bio，不得以“公开简介显示”开头，也不得只做逐字翻译。
5. 不要把账号自述当作已证实事实。资料不足时降低置信度并写明风险。
6. 只返回 JSON 数组，不要 Markdown。每项字段必须为：
   handle, type, intro, basis, confidence, risk, sources
   type 只能是“个人”或“项目”；confidence 只能是“高”“中”“低”；sources 是 URL 数组。

待研究账号：
{json.dumps(inputs, ensure_ascii=False, indent=2)}
""".strip()
    tools: list[dict[str, Any]] = [
        {
            "type": "x_search",
            "allowed_x_handles": [item["handle"].lstrip("@") for item in batch],
        },
        {"type": "web_search"},
    ]
    response = session.post(
        RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": prompt,
            "tools": tools,
            "max_output_tokens": 6000,
        },
        timeout=240,
    )
    if not response.ok:
        if response.status_code in {402, 403}:
            raise RuntimeError(
                f"xAI research unavailable (HTTP {response.status_code}: credits or spending limit)"
            )
        detail = response.text[:600].replace(api_key, "[redacted]")
        raise RuntimeError(f"xAI 研究请求失败（HTTP {response.status_code}）：{detail}")
    parsed = parse_json_array(response_output_text(response.json()))
    result: dict[str, dict[str, Any]] = {}
    for item in parsed:
        key = normalize_handle(item.get("handle"))
        if not key:
            continue
        account_type = str(item.get("type") or "").strip()
        confidence = str(item.get("confidence") or "").strip()
        sources = item.get("sources")
        if not isinstance(sources, list):
            sources = []
        result[key] = {
            "handle": display_handle(item.get("handle")),
            "type": account_type if account_type in {"个人", "项目"} else "项目",
            "intro": str(item.get("intro") or "").strip(),
            "basis": str(item.get("basis") or "").strip(),
            "confidence": confidence if confidence in {"高", "中", "低"} else "低",
            "risk": str(item.get("risk") or "").strip(),
            "sources": [
                str(url).strip()
                for url in sources
                if str(url).strip().startswith(("http://", "https://"))
            ],
        }
    return result


def classify_accounts(
    session: requests.Session,
    accounts: list[dict[str, Any]],
    offline: bool,
) -> list[dict[str, Any]]:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    model = os.getenv("XAI_MODEL", "grok-4.7").strip()
    if not offline and not api_key:
        print(
            "warning: XAI_API_KEY is unavailable; using low-confidence fallback",
            file=sys.stderr,
        )
        offline = True
    classifications: dict[str, dict[str, Any]] = {}
    if not offline:
        for start in range(0, len(accounts), 15):
            batch = accounts[start : start + 15]
            try:
                classifications.update(classify_batch(session, batch, api_key, model))
            except RuntimeError as exc:
                print(
                    f"warning: xAI research unavailable; using low-confidence fallback: {exc}",
                    file=sys.stderr,
                )
    combined: list[dict[str, Any]] = []
    for account in accounts:
        key = account["handle_key"]
        classification = classifications.get(key) or fallback_classification(account)
        if not contains_chinese_summary(classification.get("intro")):
            fallback = fallback_classification(account)
            classification = {
                **classification,
                "intro": (
                    fallback_project_intro(account)
                    if classification.get("type") == "项目"
                    else fallback_person_intro(account)
                ),
                "confidence": "低",
                "risk": "模型未返回合格的中文归纳，已改用公开资料关键词进行中文概括。",
            }
        sources = classification.get("sources") or []
        if account["x_url"] not in sources:
            sources.insert(0, account["x_url"])
        combined.append({**account, **classification, "sources": sources})
    combined.sort(
        key=lambda item: (
            0 if item["type"] == "项目" else 1,
            int(item["message_id"]),
        )
    )
    return combined


def window_text(start: datetime, end: datetime) -> str:
    start_local = start.astimezone(SHANGHAI)
    end_local = end.astimezone(SHANGHAI)
    if start_local.date() == end_local.date():
        return f"{start_local:%Y-%m-%d %H:%M}–{end_local:%H:%M}"
    return f"{start_local:%Y-%m-%d %H:%M}–{end_local:%Y-%m-%d %H:%M}"


def add_table_style(sheet: Any, reference: str, name: str) -> None:
    table = Table(displayName=name, ref=reference)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)


def create_workbook(
    records: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    output_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "判定结果"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A9"
    project_count = sum(item["type"] == "项目" for item in records)
    person_count = sum(item["type"] == "个人" for item in records)
    title = "@wendysalpha 新关注账号汇总"
    scope = (
        f"时间窗口：{window_text(start, end)}（上海）｜"
        f"本轮新增 {len(records)} 个 X 账号"
    )
    note = (
        "项目包括产品、协议、代币、媒体与组织账号。判断基于公开简介和初步研究，"
        "不构成项目背书或投资建议。"
    )
    sheet.merge_cells("A1:L1")
    sheet["A1"] = title
    sheet["A1"].font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor=NAVY.replace("#", ""))
    sheet["A1"].alignment = Alignment(horizontal="left", vertical="center")
    sheet.row_dimensions[1].height = 28
    sheet.merge_cells("A2:L2")
    sheet["A2"] = scope
    sheet["A2"].font = Font(name="Arial", size=10, italic=True, color=MUTED.replace("#", ""))
    sheet["A2"].alignment = Alignment(horizontal="left")
    sheet.merge_cells("A3:L3")
    sheet["A3"] = note
    sheet["A3"].font = Font(name="Arial", size=10, color=MUTED.replace("#", ""))
    sheet["A3"].alignment = Alignment(wrap_text=True)
    summary = [
        ("新增账号", len(records)),
        ("项目", project_count),
        ("个人", person_count),
        ("去重消息", len(duplicates)),
    ]
    for index, (label, value) in enumerate(summary):
        col = 1 + index * 2
        sheet.cell(5, col, label)
        sheet.cell(5, col + 1, value)
        sheet.cell(5, col).fill = PatternFill("solid", fgColor="D9EAF7")
        sheet.cell(5, col).font = Font(name="Arial", bold=True, color=NAVY.replace("#", ""))
        sheet.cell(5, col + 1).font = Font(name="Arial", bold=True)
        sheet.cell(5, col + 1).alignment = Alignment(horizontal="center")
    headers = [
        "X账号",
        "类型",
        "项目简介 / 账号定位",
        "判断依据",
        "置信度",
        "风险 / 备注",
        "时间（上海）",
        "关注发起账号",
        "新关注账号",
        "共同关注",
        "原始消息",
        "研究来源",
    ]
    header_row = 8
    for col, value in enumerate(headers, 1):
        cell = sheet.cell(header_row, col, value)
        cell.fill = PatternFill("solid", fgColor=HEADER_BLUE.replace("#", ""))
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color=GRID.replace("#", ""))
    for row_index, record in enumerate(records, header_row + 1):
        published = record["published_at_utc"].astimezone(SHANGHAI)
        values = [
            record["handle"],
            record["type"],
            record["intro"],
            record["basis"],
            record["confidence"],
            record["risk"],
            published.strftime("%Y-%m-%d %H:%M:%S"),
            record["follower"],
            record["name"],
            record["mutual"] if record["mutual"] is not None else "",
            record["raw"],
            " ; ".join(record["sources"]),
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row_index, col, value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(
                horizontal="left",
                vertical="top",
                wrap_text=col in {3, 4, 6, 11, 12},
            )
            cell.border = Border(bottom=thin)
        sheet.cell(row_index, 1).fill = PatternFill(
            "solid",
            fgColor=(PROJECT_FILL if record["type"] == "项目" else PERSON_FILL).replace("#", ""),
        )
        sheet.cell(row_index, 2).alignment = Alignment(horizontal="center", vertical="center")
        sheet.cell(row_index, 5).alignment = Alignment(horizontal="center", vertical="center")
        sheet.row_dimensions[row_index].height = 58
    widths = [20, 9, 46, 38, 10, 38, 22, 22, 24, 11, 60, 58]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[chr(64 + index)].width = width
    if records:
        add_table_style(sheet, f"A{header_row}:L{header_row + len(records)}", "WendysAlphaResults")
    else:
        sheet.auto_filter.ref = f"A{header_row}:L{header_row}"

    dedupe = workbook.create_sheet("去重记录")
    dedupe.sheet_view.showGridLines = False
    dedupe.freeze_panes = "A5"
    dedupe.merge_cells("A1:H1")
    dedupe["A1"] = "去重记录"
    dedupe["A1"].font = Font(name="Arial", size=14, bold=True, color="FFFFFF")
    dedupe["A1"].fill = PatternFill("solid", fgColor=NAVY.replace("#", ""))
    dedupe.merge_cells("A2:H2")
    dedupe["A2"] = "记录跨轮次重复账号及本轮同账号重复通知。"
    dedupe["A2"].font = Font(name="Arial", italic=True, color=MUTED.replace("#", ""))
    dedupe_headers = [
        "消息ID",
        "时间（上海）",
        "X账号",
        "重复类型",
        "关注发起账号",
        "新关注账号",
        "原始消息",
        "帖子链接",
    ]
    for col, value in enumerate(dedupe_headers, 1):
        cell = dedupe.cell(4, col, value)
        cell.fill = PatternFill("solid", fgColor=HEADER_BLUE.replace("#", ""))
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row_index, record in enumerate(duplicates, 5):
        values = [
            record["message_id"],
            record["published_at_utc"].astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            record["handle"],
            record["duplicate_reason"],
            record["follower"],
            record["name"],
            record["raw"],
            record["post_url"],
        ]
        for col, value in enumerate(values, 1):
            cell = dedupe.cell(row_index, col, value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=col == 7)
            cell.border = Border(bottom=thin)
        dedupe.row_dimensions[row_index].height = 42
    for index, width in enumerate([12, 22, 20, 16, 22, 24, 60, 38], 1):
        dedupe.column_dimensions[chr(64 + index)].width = width
    if duplicates:
        add_table_style(dedupe, f"A4:H{4 + len(duplicates)}", "WendysAlphaDuplicates")
    else:
        dedupe.auto_filter.ref = "A4:H4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    verify_workbook(output_path, len(records))


def verify_workbook(path: Path, expected_rows: int) -> None:
    workbook = load_workbook(path, read_only=True, data_only=False)
    required_sheets = {"判定结果", "去重记录"}
    if not required_sheets.issubset(set(workbook.sheetnames)):
        raise RuntimeError("Excel 验证失败：缺少工作表")
    sheet = workbook["判定结果"]
    headers = [sheet.cell(8, col).value for col in range(1, 13)]
    if headers[:3] != ["X账号", "类型", "项目简介 / 账号定位"]:
        raise RuntimeError("Excel 验证失败：主表列名不正确")
    actual_rows = sum(bool(sheet.cell(row, 1).value) for row in range(9, sheet.max_row + 1))
    if actual_rows != expected_rows:
        raise RuntimeError(
            f"Excel 验证失败：预期 {expected_rows} 行，实际 {actual_rows} 行"
        )
    workbook.close()


def font_path(candidates: tuple[Path, ...]) -> Path:
    for path in candidates:
        if path.exists():
            return path
    raise RuntimeError("找不到可显示中文的字体")


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "—").splitlines() or ["—"]:
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current.rstrip())
                current = character.lstrip()
            else:
                current = candidate
        lines.append(current or " ")
    return lines


def center_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        (
            left + (right - left - width) / 2,
            top + (bottom - top - height) / 2 - bounds[1],
        ),
        text,
        font=font,
        fill=fill,
    )


def render_category(
    rows: list[dict[str, Any]],
    title: str,
    scope: str,
    output_path: Path,
    category: str,
) -> None:
    width = 1800
    margin = 54
    title_height = 92
    subtitle_height = 58
    table_header_height = 70
    footer_height = 54
    col_widths = [370, width - margin * 2 - 370]
    regular = font_path(FONT_REGULAR)
    bold = font_path(FONT_BOLD)
    title_font = ImageFont.truetype(str(bold), 42)
    subtitle_font = ImageFont.truetype(str(regular), 24)
    header_font = ImageFont.truetype(str(bold), 28)
    body_font = ImageFont.truetype(str(regular), 27)
    footer_font = ImageFont.truetype(str(regular), 21)
    scratch = Image.new("RGB", (width, 100), "white")
    scratch_draw = ImageDraw.Draw(scratch)
    prepared: list[tuple[dict[str, Any], list[str], int]] = []
    for row in rows:
        intro_lines = wrap_text(
            scratch_draw,
            str(row.get("intro") or "—"),
            body_font,
            col_widths[1] - 42,
        )
        row_height = max(94, max(1, len(intro_lines)) * 39 + 34)
        prepared.append((row, intro_lines, row_height))
    empty_height = 150 if not prepared else 0
    height = (
        margin
        + title_height
        + subtitle_height
        + 26
        + table_header_height
        + sum(item[2] for item in prepared)
        + empty_height
        + footer_height
        + margin
    )
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    x0, x1 = margin, width - margin
    y = margin
    draw.rounded_rectangle((x0, y, x1, y + title_height), radius=14, fill=NAVY)
    draw.text((x0 + 28, y + 18), f"@wendysalpha  {title}", font=title_font, fill="white")
    y += title_height
    draw.rectangle((x0, y, x1, y + subtitle_height), fill=ROW_BLUE)
    draw.text((x0 + 22, y + 12), scope, font=subtitle_font, fill=MUTED)
    y += subtitle_height + 26
    header_top = y
    draw.rectangle((x0, y, x1, y + table_header_height), fill=HEADER_BLUE)
    cursor = x0
    for name, col_width in zip(("X账号", "项目简介"), col_widths):
        center_text(
            draw,
            (cursor, y, cursor + col_width, y + table_header_height),
            name,
            header_font,
            "white",
        )
        cursor += col_width
    y += table_header_height
    for index, (row, intro_lines, row_height) in enumerate(prepared):
        fill = ROW_BLUE if index % 2 == 0 else ROW_WHITE
        draw.rectangle((x0, y, x1, y + row_height), fill=fill)
        handle_fill = PROJECT_FILL if category == "项目" else PERSON_FILL
        draw.rectangle((x0, y, x0 + col_widths[0], y + row_height), fill=handle_fill)
        center_text(
            draw,
            (x0, y, x0 + col_widths[0], y + row_height),
            str(row["handle"]),
            body_font,
            "#1F4E78",
        )
        intro_x = x0 + col_widths[0] + 22
        intro_y = y + (row_height - len(intro_lines) * 39) // 2
        for line in intro_lines:
            draw.text((intro_x, intro_y), line, font=body_font, fill=TEXT)
            intro_y += 39
        draw.line((x0, y + row_height, x1, y + row_height), fill=GRID, width=2)
        y += row_height
    if not prepared:
        draw.rectangle((x0, y, x1, y + empty_height), fill=ROW_BLUE)
        center_text(
            draw,
            (x0, y, x1, y + empty_height),
            f"本期无新增{category}账号",
            body_font,
            MUTED,
        )
        y += empty_height
    table_bottom = y
    draw.line(
        (x0 + col_widths[0], header_top, x0 + col_widths[0], table_bottom),
        fill=GRID,
        width=2,
    )
    draw.rectangle((x0, header_top, x1, table_bottom), outline=GRID, width=2)
    y += 18
    draw.text(
        (x0, y),
        f"共 {len(rows)} 个账号 · 公开资料初步判断",
        font=footer_font,
        fill=MUTED,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)


def send_photo(
    session: requests.Session,
    token: str,
    channel: str,
    file_path: Path,
    caption: str,
) -> dict[str, Any]:
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    with file_path.open("rb") as photo:
        response = session.post(
            endpoint,
            data={"chat_id": channel, "caption": caption},
            files={"photo": (file_path.name, photo, "image/png")},
            timeout=90,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram 返回非 JSON（HTTP {response.status_code}）") from exc
    if not response.ok or payload.get("ok") is not True:
        description = str(payload.get("description") or f"HTTP {response.status_code}")
        raise RuntimeError(f"Telegram 图片发送失败：{description}")
    return payload


def post_url(channel: str, payload: dict[str, Any]) -> str:
    message_id = ((payload.get("result") or {}).get("message_id"))
    slug = channel.lstrip("@")
    return f"https://t.me/{slug}/{message_id}"


def main() -> None:
    args = parse_args()
    end = now_utc(args.now)
    start = end - timedelta(hours=args.hours)
    output_dir = resolve_path(args.output_dir)
    state_path = resolve_path(args.state_file)
    seed_path = resolve_path(args.seed_file)
    state = load_state(state_path, seed_path)
    seen_handles = set(state["seen_handles"])
    session = build_session()
    messages, newest_id = fetch_messages(
        session,
        int(state["last_message_id"]),
        start,
        args.max_pages,
    )
    accounts, duplicates = collect_new_accounts(messages, seen_handles)
    if not accounts:
        if not args.dry_run:
            state["last_message_id"] = newest_id
            state["updated_at_utc"] = end.isoformat()
            save_state(state_path, state)
        print(
            json.dumps(
                {
                    "status": "no_new_accounts",
                    "message_count": len(messages),
                    "last_message_id": newest_id,
                },
                ensure_ascii=False,
            )
        )
        return
    records = classify_accounts(session, accounts, args.offline_classify)
    timestamp = end.astimezone(SHANGHAI).strftime("%Y-%m-%d_%H%M")
    workbook_path = output_dir / f"wendysalpha_{timestamp}.xlsx"
    project_image = output_dir / f"wendysalpha_{timestamp}_projects.png"
    person_image = output_dir / f"wendysalpha_{timestamp}_people.png"
    create_workbook(records, duplicates, start, end, workbook_path)
    projects = [item for item in records if item["type"] == "项目"]
    people = [item for item in records if item["type"] == "个人"]
    scope = f"时间窗口：{window_text(start, end)}（上海）"
    render_category(projects, "项目账号汇总", scope, project_image, "项目")
    render_category(people, "个人账号汇总", scope, person_image, "个人")
    urls: list[str] = []
    if not args.dry_run:
        token = os.getenv("WENDYSALPHA_TELEGRAM_BOT_TOKEN", "").strip()
        channel = os.getenv("WENDYSALPHA_TELEGRAM_CHANNEL", "@wendysalpha").strip()
        if not token:
            raise RuntimeError("缺少 WENDYSALPHA_TELEGRAM_BOT_TOKEN")
        project_result = send_photo(
            session,
            token,
            channel,
            project_image,
            f"{scope}｜项目账号新增 {len(projects)} 个",
        )
        person_result = send_photo(
            session,
            token,
            channel,
            person_image,
            f"{scope}｜个人账号新增 {len(people)} 个",
        )
        urls = [post_url(channel, project_result), post_url(channel, person_result)]
        state["last_message_id"] = newest_id
        state["seen_handles"] = sorted(
            seen_handles | {item["handle_key"] for item in accounts}
        )
        state["updated_at_utc"] = end.isoformat()
        save_state(state_path, state)
    print(
        json.dumps(
            {
                "status": "dry_run" if args.dry_run else "sent",
                "new_accounts": len(records),
                "projects": len(projects),
                "people": len(people),
                "workbook": str(workbook_path.relative_to(ROOT)),
                "images": [
                    str(project_image.relative_to(ROOT)),
                    str(person_image.relative_to(ROOT)),
                ],
                "posts": urls,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
