#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import email.utils
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NEW_YORK = ZoneInfo("America/New_York")
EXCHANGES = ("nasdaq", "nyse", "amex")
MIN_PCT = 5.0
MIN_MARKET_CAP = 30_000_000_000.0
MAX_MARKET_CAP = 200_000_000_000.0
MIN_DOLLAR_VOLUME = 70_000_000.0
MIN_TURNOVER = 1.0
MAX_TURNOVER = 15.0
DISPLAY_LIMIT = 20
FONT_CANDIDATES = [
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
]


def load_local_env() -> None:
    path = ROOT / ".env.local"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="筛选美股当日强势股并推送 Telegram")
    parser.add_argument("--output-dir", default="reports/us_strong_stocks")
    parser.add_argument("--dry-run", action="store_true", help="只生成文件，不发送 Telegram")
    return parser.parse_args()


def number(value: object) -> float | None:
    text = str(value or "").replace("$", "").replace(",", "").replace("%", "").strip()
    if not text or text in {"N/A", "--", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = os.getenv("ASTOCK_TRUST_ENV", "0").lower() in {"1", "true", "yes", "on"}
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
        }
    )
    return session


def external_request_kwargs() -> dict:
    proxy = os.getenv("ASTOCK_EXTERNAL_PROXY", "").strip() or os.getenv("HTTPS_PROXY", "").strip()
    return {"proxies": {"http": proxy, "https": proxy}} if proxy else {}


def is_common_stock(row: dict) -> bool:
    name = str(row.get("name") or "")
    symbol = str(row.get("symbol") or "")
    rejected = (
        "Warrant", "Right", "Units", "Unit ", "Preferred", "Depositary Share",
        "Acquisition Corp", "ETF", "ETN", "Notes due",
    )
    if any(word.lower() in name.lower() for word in rejected):
        return False
    if re.search(r"(?:W|R|U)$", symbol) and len(symbol) >= 5:
        return False
    return bool(symbol and re.fullmatch(r"[A-Z.\-]+", symbol))


def fetch_exchange(session: requests.Session, exchange: str) -> tuple[str, list[dict]]:
    response = session.get(
        NASDAQ_SCREENER_URL,
        params={"tableonly": "false", "limit": "10000", "exchange": exchange, "download": "true"},
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if ((payload.get("status") or {}).get("rCode")) != 200:
        raise RuntimeError(f"Nasdaq screener returned an error for {exchange}: {payload.get('status')}")
    data = payload.get("data") or {}
    rows = ((data.get("table") or {}).get("rows") or data.get("rows") or [])
    return str(data.get("asof") or data.get("asOf") or ""), rows


def score(row: dict) -> float:
    turnover = float(row["turnoverRate"])
    turnover_score = 18 if 3 <= turnover <= 8 else 10 if 2 <= turnover < 3 or 8 < turnover <= 12 else 4
    cap = float(row["marketCap"])
    cap_score = 8 if 50e9 <= cap <= 130e9 else 4
    return round(min(float(row["dollarVolume"]) / 14_000_000, 120) * 0.45 + min(float(row["pctChange"]), 20) * 2.8 + turnover_score + cap_score, 4)


def select_rows(raw_rows: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for raw in raw_rows:
        price = number(raw.get("lastsale"))
        pct = number(raw.get("pctchange"))
        volume = number(raw.get("volume"))
        market_cap = number(raw.get("marketCap"))
        if None in (price, pct, volume, market_cap) or not is_common_stock(raw):
            continue
        assert price is not None and pct is not None and volume is not None and market_cap is not None
        dollar_volume = price * volume
        shares = market_cap / price if price > 0 else 0
        turnover = volume / shares * 100 if shares > 0 else 0
        if not (pct > MIN_PCT and MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
            continue
        if dollar_volume < MIN_DOLLAR_VOLUME or not (MIN_TURNOVER < turnover <= MAX_TURNOVER):
            continue
        row = {
            "symbol": raw["symbol"], "name": raw.get("name") or raw["symbol"],
            "exchange": raw.get("exchange") or "", "sector": raw.get("sector") or "-",
            "industry": raw.get("industry") or "-", "lastPrice": price, "pctChange": pct,
            "volume": volume, "marketCap": market_cap, "dollarVolume": dollar_volume,
            "turnoverRate": turnover,
        }
        row["strongScore"] = score(row)
        selected.append(row)
    selected.sort(key=lambda x: (x["strongScore"], x["dollarVolume"], x["pctChange"]), reverse=True)
    return selected[:DISPLAY_LIMIT]


def fetch_company_context(session: requests.Session, row: dict) -> dict:
    symbol = row["symbol"]
    company_name = re.sub(
        r"\s+(?:Class [A-Z] |Ordinary |Common )?(?:Common Stock|Ordinary Shares?|Class [A-Z] common stock)$",
        "",
        str(row.get("name") or symbol),
        flags=re.I,
    ).strip()
    description = ""
    try:
        response = requests.get(
            f"https://api.nasdaq.com/api/company/{symbol}/company-profile",
            headers=dict(session.headers),
            timeout=25,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        description = str(((data.get("CompanyDescription") or {}).get("value")) or "").strip()
    except Exception:
        pass
    headlines: list[dict] = []
    try:
        query = quote_plus(f'"{company_name}" {symbol} stock when:3d')
        # Keep news transport separate: local Nasdaq access may be direct while Google/Telegram require a proxy.
        response = requests.get(
            f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
            headers={"User-Agent": session.headers.get("User-Agent", "Mozilla/5.0")},
            timeout=25,
            **external_request_kwargs(),
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        distinctive = [word.lower() for word in re.findall(r"[A-Za-z]{4,}", company_name) if word.lower() not in {"common", "stock", "class", "corporation", "incorporated", "company", "ordinary", "shares"}]
        for item in root.findall("./channel/item")[:10]:
            title = str(item.findtext("title") or "").strip()
            published = str(item.findtext("pubDate") or "").strip()
            source_node = item.find("source")
            source = str(source_node.text or "").strip() if source_node is not None else ""
            link = str(item.findtext("link") or "").strip()
            title_lower = title.lower()
            if title and (symbol.lower() in title_lower or any(word in title_lower for word in distinctive[:3])):
                headlines.append({"title": title, "published": published, "source": source, "link": link})
            if len(headlines) >= 5:
                break
    except Exception:
        pass
    return {"description": description, "headlines": headlines}


def fallback_business(row: dict, description: str) -> str:
    if description:
        first = re.split(r"(?<=[.!?])\s+", description)[0]
        return first[:110]
    return f"{row.get('sector') or '-'}：{row.get('industry') or '-'}"


def fetch_stocknews_signals(rows: list[dict]) -> dict[str, list[dict]]:
    api_key = os.getenv("STOCKNEWS_API_KEY", "").strip()
    if not api_key or not rows:
        return {}
    symbols = ",".join(row["symbol"] for row in rows)
    response = requests.get(
        "https://stocknews.ai/api/news",
        headers={"x-api-key": api_key, "User-Agent": "AStock-US-Strong-Stocks/1.0"},
        params={"symbol": symbols, "limit": max(10, len(rows) * 3)},
        timeout=35,
        **external_request_kwargs(),
    )
    response.raise_for_status()
    signals: dict[str, list[dict]] = {row["symbol"]: [] for row in rows}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    for item in response.json().get("data") or []:
        symbol = str(item.get("symbol") or "").upper()
        if symbol not in signals:
            continue
        try:
            published = datetime.fromisoformat(str(item.get("publishedAt") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if published < cutoff or float(item.get("relevanceScore") or 0) < 70:
            continue
        title = str(item.get("signalTitle") or item.get("title") or "").strip()
        if not title:
            continue
        signals[symbol].append({
            "title": title,
            "published": str(item.get("publishedAt") or ""),
            "source": str(item.get("source") or "StockNews.AI"),
            "link": str(item.get("url") or ""),
            "importanceScore": item.get("importanceScore"),
            "relevanceScore": item.get("relevanceScore"),
            "impact": str(item.get("stockImpactReasoning") or ""),
            "provider": "StockNews.AI",
        })
    for items in signals.values():
        items.sort(key=lambda x: (float(x.get("relevanceScore") or 0), float(x.get("importanceScore") or 0), x["published"]), reverse=True)
    return signals


def enrich_rows(session: requests.Session, rows: list[dict]) -> None:
    contexts: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(rows)))) as executor:
        futures = {executor.submit(fetch_company_context, session, row): row["symbol"] for row in rows}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                contexts[symbol] = future.result()
            except Exception:
                contexts[symbol] = {"description": "", "headlines": []}
    try:
        stocknews = fetch_stocknews_signals(rows)
        for row in rows:
            symbol = row["symbol"]
            # Structured, exact-symbol signals lead; Google RSS remains a fallback.
            contexts[symbol]["headlines"] = stocknews.get(symbol, []) + contexts[symbol]["headlines"]
    except Exception as exc:
        print(f"warning: StockNews.AI enrichment failed: {exc}")
    api_key = os.getenv("XAI_API_KEY", "").strip()
    enriched: dict[str, dict] = {}
    if api_key and rows:
        evidence = [
            {
                "symbol": row["symbol"], "name": row["name"], "sector": row["sector"],
                "industry": row["industry"], "pctChange": row["pctChange"],
                "dollarVolume": row["dollarVolume"], **contexts[row["symbol"]],
            }
            for row in rows
        ]
        prompt = (
            "你是严谨的美股收盘编辑。根据给定公司简介和最近72小时新闻，为每只股票输出简体中文JSON。"
            "格式必须是数组，每项仅含symbol、business、reason、evidenceIndex。business不超过32个汉字，说明主营业务；"
            "reason不超过42个汉字。只有最近新闻标题明确解释上涨时才能写具体催化，并将evidenceIndex设为该股票headlines中的"
            "零基索引。优先采用provider为StockNews.AI、相关度较高且发布时间最接近交易日的证据；只能忠实压缩标题和impact，"
            "不得补充证据中没有的事实。不能确认因果时reason写‘未查到可核验的当日催化’，evidenceIndex设为-1。不得把涨幅本身、旧闻、"
            "泛泛的资金推动或猜测写成原因。证据如下：\n" + json.dumps(evidence, ensure_ascii=False)
        )
        try:
            response = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": os.getenv("XAI_MODEL", "grok-3-mini"), "messages": [{"role": "user", "content": prompt}], "temperature": 0},
                timeout=90,
                **external_request_kwargs(),
            )
            response.raise_for_status()
            content = str(response.json().get("choices", [{}])[0].get("message", {}).get("content") or "")
            match = re.search(r"\[[\s\S]*\]", content)
            parsed = json.loads(match.group(0)) if match else []
            enriched = {str(item.get("symbol") or ""): item for item in parsed if isinstance(item, dict)}
        except Exception as exc:
            print(f"warning: xAI enrichment failed: {exc}")
    for row in rows:
        item = enriched.get(row["symbol"], {})
        context = contexts[row["symbol"]]
        row["businessIntro"] = str(item.get("business") or fallback_business(row, context["description"])).strip()
        evidence_index = item.get("evidenceIndex", -1)
        try:
            evidence_index = int(evidence_index)
        except (TypeError, ValueError):
            evidence_index = -1
        if 0 <= evidence_index < len(context["headlines"]):
            evidence_item = context["headlines"][evidence_index]
            row["riseReason"] = str(item.get("reason") or "未查到可核验的当日催化").strip()
            row["reasonSource"] = evidence_item
        else:
            row["riseReason"] = "未查到可核验的当日催化"
            row["reasonSource"] = {}
        row["recentHeadlines"] = context["headlines"]


def find_font() -> Path:
    custom = os.getenv("ASTOCK_CJK_FONT", "").strip()
    for path in ([Path(custom)] if custom else []) + FONT_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("No CJK font found; install fonts-noto-cjk or set ASTOCK_CJK_FONT")


def compact_usd(value: float) -> str:
    return f"${value / 1e9:.2f}B" if value >= 1e9 else f"${value / 1e6:.1f}M"


def render(rows: list[dict], market_date: str, output: Path) -> None:
    font_path = find_font()
    width, margin, row_h, header_h = 2130, 48, 92, 62
    height = 285 + header_h + len(rows) * row_h + 80
    image = Image.new("RGB", (width, height), "#F5F7FA")
    draw = ImageDraw.Draw(image)

    def face(size: int, bold: bool = False):
        try:
            return ImageFont.truetype(str(font_path), size=size, index=1 if bold else 0)
        except OSError:
            return ImageFont.truetype(str(font_path), size=size)

    title, subtitle, head, cell, small = face(58, True), face(28), face(21, True), face(19), face(17)
    draw.rounded_rectangle((40, 35, width - 40, 220), radius=28, fill="#2457C5")
    draw.text((margin + 25, 62), "美股当日强势股", font=title, fill="#FFFFFF")
    draw.text((margin + 27, 145), f"{market_date or '最新交易日'}   共 {len(rows)} 只   数据源 Nasdaq", font=subtitle, fill="#DDE8FF")
    columns = [("#", 60), ("股票", 190), ("公司业务", 500), ("上涨原因", 650), ("价格", 140), ("市值", 180), ("成交额", 190), ("涨幅", 140)]
    y = 255
    draw.rectangle((margin, y, width - margin, y + header_h), fill="#253246")
    x = margin
    for label, col_w in columns:
        draw.text((x + (col_w - draw.textlength(label, font=head)) / 2, y + 18), label, font=head, fill="#FFFFFF")
        x += col_w
    y += header_h
    for index, row in enumerate(rows, 1):
        draw.rectangle((margin, y, width - margin, y + row_h), fill="#FFFFFF" if index % 2 else "#F8FAFC")
        reason = row.get("riseReason") or "未查到可核验的当日催化"
        source = (row.get("reasonSource") or {}).get("source") or ""
        if source and reason != "未查到可核验的当日催化":
            reason = f"{reason}（{source}）"
        values = [str(index), row["symbol"], row.get("businessIntro") or "-", reason, f"${row['lastPrice']:.2f}", compact_usd(row["marketCap"]), compact_usd(row["dollarVolume"]), f"+{row['pctChange']:.2f}%"]
        x = margin
        for col_index, ((_, col_w), value) in enumerate(zip(columns, values)):
            use_font = head if col_index == 1 else cell
            lines = [value]
            if col_index in {2, 3} and draw.textlength(value, font=use_font) > col_w - 24:
                cut = max(1, int(len(value) * (col_w - 24) / draw.textlength(value, font=use_font)))
                lines = [value[:cut], value[cut:]]
            lines = [line if draw.textlength(line, font=use_font) <= col_w - 24 else line[:max(3, len(line) - 2)] + "…" for line in lines[:2]]
            color = "#D93A32" if col_index == 7 else "#2457C5" if col_index == 1 else "#44505E"
            for line_index, line in enumerate(lines):
                draw.text((x + (col_w - draw.textlength(line, font=use_font)) / 2, y + 21 + line_index * 28), line, font=use_font, fill=color)
            x += col_w
        y += row_h
    note = "筛选：涨幅>5%，市值$30B–$200B，成交额>$70M；上涨原因仅采用最近72小时且可对应公司的新闻证据，无证据则明确标注。"
    draw.text((margin, height - 52), note, font=small, fill="#6B7580")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, optimize=True)


def send_telegram(session: requests.Session, image_path: Path, caption: str) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    data = {"chat_id": chat_id, "caption": caption}
    thread_id = os.getenv("TELEGRAM_US_THREAD_ID", "").strip()
    if thread_id:
        data["message_thread_id"] = thread_id
    with image_path.open("rb") as image_file:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=data,
            files={"document": (image_path.name, image_file, "image/png")},
            timeout=120,
            **external_request_kwargs(),
        )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {payload.get('description', 'unknown error')}")
    return int(payload["result"]["message_id"])


def main() -> int:
    args = parse_args()
    load_local_env()
    session = build_session()
    raw_rows: list[dict] = []
    as_of_values: list[str] = []
    for exchange in EXCHANGES:
        as_of, exchange_rows = fetch_exchange(session, exchange)
        for row in exchange_rows:
            row["exchange"] = exchange.upper()
        raw_rows.extend(exchange_rows)
        if as_of:
            as_of_values.append(as_of)
    rows = select_rows(raw_rows)
    enrich_rows(session, rows)
    market_label = as_of_values[0] if as_of_values else datetime.now(NEW_YORK).date().isoformat()
    date_match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", market_label)
    market_date = datetime.strptime(date_match.group(1), "%b %d, %Y").date() if date_match else datetime.now(NEW_YORK).date()
    output_dir = ROOT / args.output_dir
    json_path = output_dir / f"us_strong_stocks_{market_date.isoformat()}.json"
    image_path = output_dir / f"us_strong_stocks_{market_date.isoformat()}.png"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps({"date": market_date.isoformat(), "sourceAsOf": market_label, "count": len(rows), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    render(rows, market_date.isoformat(), image_path)
    message_id = None if args.dry_run else send_telegram(session, image_path, f"{market_date.month}月{market_date.day}日 美股当日强势股")
    print(json.dumps({"status": "ok", "date": market_date.isoformat(), "count": len(rows), "json": str(json_path), "image": str(image_path), "message_id": message_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
