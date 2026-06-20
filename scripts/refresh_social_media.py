#!/usr/bin/env python3
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
KOL_WATCHLIST_PATH = REPORTS_DIR / "social_kol_watchlist.json"
SOCIAL_POSTS_PATH = REPORTS_DIR / "social_media_posts.json"

try:
    import certifi

    HTTPS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    HTTPS_CONTEXT = ssl.create_default_context()


def load_dotenv() -> None:
    env_path = ROOT / ".env.local"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_handle(value: object) -> str:
    return str(value or "").strip().replace("@", "").split("/")[-1]


def normalize_tracker(item: object, index: int) -> dict | None:
    if not isinstance(item, dict):
        return None
    handle = normalize_handle(item.get("handle") or item.get("id"))
    if not handle:
        return None
    return {
        "id": str(item.get("id") or f"kol-{index}-{handle}"),
        "name": str(item.get("name") or handle).strip(),
        "handle": handle,
        "platform": str(item.get("platform") or "X").strip(),
        "enabled": item.get("enabled") is not False,
    }


def request_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout, context=HTTPS_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout, context=HTTPS_CONTEXT) as response:
        return response.read().decode("utf-8", errors="replace")


def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    replacements = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_recent_tweets_x_api(handle: str, since_dt: datetime, bearer_token: str) -> list[dict]:
    query = f"from:{handle} -is:retweet"
    params = urllib.parse.urlencode(
        {
            "query": query,
            "max_results": "10",
            "tweet.fields": "created_at,lang,entities",
            "start_time": since_dt.isoformat().replace("+00:00", "Z"),
        }
    )
    payload = request_json(
        f"https://api.twitter.com/2/tweets/search/recent?{params}",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "stockkiller-social-refresh",
        },
    )
    out = []
    for item in payload.get("data") or []:
        tweet_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        created_at = str(item.get("created_at") or "").strip()
        if not tweet_id or not text:
            continue
        out.append(
            {
                "id": tweet_id,
                "text": text,
                "createdAt": created_at,
                "url": f"https://x.com/{handle}/status/{tweet_id}",
                "source": "x-api",
            }
        )
    return out


def fetch_recent_tweets_rss(handle: str, since_dt: datetime, base_urls: list[str]) -> list[dict]:
    out = []
    headers = {"User-Agent": "Mozilla/5.0 stockkiller-social-refresh"}
    for base_url in base_urls:
        base = base_url.rstrip("/")
        if not base:
            continue
        try:
            xml_text = request_text(f"{base}/{urllib.parse.quote(handle)}/rss", headers=headers, timeout=20)
            root = ET.fromstring(xml_text)
        except Exception as exc:
            print(f"RSS fetch failed for @{handle} via {base_url}: {exc}", file=sys.stderr)
            continue
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            created_at = parse_dt(pub_date)
            if created_at and created_at < since_dt:
                continue
            title = strip_html(item.findtext("title") or "")
            description = strip_html(item.findtext("description") or "")
            text = description or title
            if not text:
                continue
            tweet_id_match = re.search(r"/status(?:es)?/(\d+)", link)
            tweet_id = tweet_id_match.group(1) if tweet_id_match else f"rss-{hashlib.sha1((link or text).encode('utf-8')).hexdigest()[:16]}"
            out.append(
                {
                    "id": tweet_id,
                    "text": text,
                    "createdAt": created_at.isoformat() if created_at else "",
                    "url": f"https://x.com/{handle}/status/{tweet_id}" if tweet_id.isdigit() else link,
                    "source": f"rss:{base_url}",
                }
            )
        if out:
            break
    return out


def get_json_from_model_text(text: str) -> dict:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S | re.I)
    candidate = fenced.group(1).strip() if fenced else cleaned
    return json.loads(candidate)


def normalize_list(values: object, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        text = str(value or "").strip()
        if text and text != "[object Object]":
            out.append(text)
        if len(out) >= limit:
            break
    return out


def call_xai(api_key: str, model: str, kol: str, handle: str, tweet: dict) -> dict:
    system_prompt = "\n".join(
        [
            "你是A股社交媒体情绪与观点提炼助手。",
            "请从用户提供的X推文原文中提取结构化关键信息，并翻译成中文。",
            "不要编造不存在的信息，无法确认就留空或给空数组。",
            "summary 必须用中文概括推文核心观点。",
            "translatedText 必须是推文原文的中文翻译；如果原文已经是中文，就原样整理为中文。",
            "target/targets 填涉及的股票、公司、行业主题或资产；没有明确提及时 target 填“未提及”。",
            "只输出一个JSON对象，不要输出任何额外说明。",
            'JSON结构必须为：{"kol":"","handle":"","platform":"X","target":"","targets":[],"industry":"","date":"","summary":"","translatedText":"","stance":"偏多|中性|偏谨慎","tags":[],"catalysts":[],"risks":[]}',
        ]
    )
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"KOL/来源：{kol} (@{handle})\n"
                    f"链接：{tweet.get('url', '')}\n"
                    f"时间：{tweet.get('createdAt', '')}\n\n"
                    f"X推文原文如下：\n{tweet.get('text', '')}"
                ),
            },
        ],
    }
    request = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "stockkiller-social-refresh",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=HTTPS_CONTEXT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xAI request failed: {exc.code} {detail}") from exc
    content = str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("xAI returned empty content")
    structured = get_json_from_model_text(content)
    summary = str(structured.get("summary") or "").strip()
    if not summary:
        raise RuntimeError("xAI output missing summary")
    targets = normalize_list(structured.get("targets"), 12)
    target = str(structured.get("target") or (targets[0] if targets else "未提及")).strip()
    return {
        "id": f"social-{tweet.get('id') or int(time.time() * 1000)}",
        "kol": str(structured.get("kol") or kol or handle).strip(),
        "handle": str(structured.get("handle") or handle).replace("@", "").strip(),
        "platform": "X",
        "target": target or "未提及",
        "targets": targets if targets else ([] if target == "未提及" else [target]),
        "industry": str(structured.get("industry") or "").strip(),
        "content": summary,
        "summary": summary,
        "rawText": str(tweet.get("text") or "").strip(),
        "translatedText": str(structured.get("translatedText") or structured.get("translation") or "").strip(),
        "sourceUrl": str(tweet.get("url") or "").strip(),
        "tweetUrl": str(tweet.get("url") or "").strip(),
        "stance": str(structured.get("stance") or "").strip(),
        "tags": normalize_list(structured.get("tags"), 8),
        "catalysts": normalize_list(structured.get("catalysts"), 6),
        "risks": normalize_list(structured.get("risks"), 6),
        "date": str(structured.get("date") or tweet.get("createdAt") or "").strip(),
        "createdAt": int(time.time() * 1000),
    }


def merge_posts(existing: list[dict], generated: list[dict], max_posts: int) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in existing + generated:
        if not isinstance(item, dict):
            continue
        key = str(item.get("sourceUrl") or item.get("tweetUrl") or item.get("id") or "").strip()
        if not key:
            key = str(item.get("rawText") or item.get("summary") or "")
        if not key:
            continue
        merged[key] = item
    out = list(merged.values())
    out.sort(key=lambda item: (str(item.get("date") or ""), int(item.get("createdAt") or 0)), reverse=True)
    return out[:max_posts]


def main() -> int:
    load_dotenv()
    trackers_raw = read_json(KOL_WATCHLIST_PATH, [])
    trackers = [
        tracker
        for idx, item in enumerate(trackers_raw, start=1)
        if (tracker := normalize_tracker(item, idx)) and tracker["enabled"]
    ]
    if not trackers:
        print("No enabled social KOL trackers; skipping social refresh.")
        return 0

    xai_api_key = (os.environ.get("XAI_API_KEY") or "").strip()
    if not xai_api_key:
        print("Missing XAI_API_KEY; skipping social refresh.", file=sys.stderr)
        return 0

    now = parse_dt(os.environ.get("SOCIAL_REFRESH_NOW", "")) or datetime.now(timezone.utc)
    since_dt = now - timedelta(hours=int(os.environ.get("SOCIAL_LOOKBACK_HOURS", "24")))
    max_per_kol = int(os.environ.get("SOCIAL_MAX_TWEETS_PER_KOL", "3"))
    max_posts = int(os.environ.get("SOCIAL_MAX_POSTS", "200"))
    model = (os.environ.get("XAI_MODEL") or "grok-3-mini").strip()
    bearer_token = (os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN") or "").strip()
    rss_bases = [
        item.strip()
        for item in os.environ.get(
            "SOCIAL_NITTER_BASE_URLS",
            "https://nitter.net,https://nitter.poast.org,https://nitter.privacydev.net",
        ).split(",")
        if item.strip()
    ]

    generated: list[dict] = []
    existing = read_json(SOCIAL_POSTS_PATH, [])
    existing_urls = {
        str(item.get("sourceUrl") or item.get("tweetUrl") or "").strip()
        for item in existing
        if isinstance(item, dict)
    }

    for tracker in trackers:
        handle = tracker["handle"]
        tweets: list[dict] = []
        try:
            if bearer_token:
                tweets = fetch_recent_tweets_x_api(handle, since_dt, bearer_token)
            if not tweets:
                tweets = fetch_recent_tweets_rss(handle, since_dt, rss_bases)
        except Exception as exc:
            print(f"Fetch failed for @{handle}: {exc}", file=sys.stderr)
            continue
        for tweet in tweets[:max_per_kol]:
            if tweet.get("url") in existing_urls:
                continue
            try:
                generated.append(call_xai(xai_api_key, model, tracker["name"], handle, tweet))
            except Exception as exc:
                print(f"Analyze failed for @{handle} {tweet.get('url', '')}: {exc}", file=sys.stderr)

    if not generated:
        print("No new social posts generated.")
        return 0

    write_json(SOCIAL_POSTS_PATH, merge_posts(existing, generated, max_posts))
    print(f"Generated {len(generated)} social posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
