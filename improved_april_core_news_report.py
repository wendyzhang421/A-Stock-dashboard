#!/usr/bin/env python3
from __future__ import annotations

import csv
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
NEWS_TMPL = "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
TENCENT_KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
AS_OF = datetime(2026, 4, 2, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
NEWS_LOOKBACK_DAYS = 3

POSITIVE_KEYWORDS = {
    "公告": 50,
    "年报": 45,
    "季报": 45,
    "业绩": 40,
    "预增": 35,
    "预减": 30,
    "快报": 20,
    "中标": 40,
    "订单": 40,
    "合同": 35,
    "签约": 28,
    "获批": 45,
    "批准": 35,
    "临床": 42,
    "回购": 36,
    "增持": 30,
    "减持": 24,
    "问询": 35,
    "监管": 35,
    "风险提示": 38,
    "停牌": 35,
    "复牌": 35,
    "量产": 32,
    "新品": 24,
    "发布": 20,
    "合作": 24,
    "战略": 20,
    "项目": 18,
    "募投": 20,
}

NEGATIVE_KEYWORDS = {
    "主力资金": -60,
    "异动快报": -60,
    "股票行情快报": -60,
    "涨停分析": -55,
    "龙虎榜": -45,
    "复盘": -45,
    "盘中": -30,
    "收盘": -30,
    "ETF": -35,
    "概念": -25,
    "资金净买入": -50,
    "资金净卖出": -50,
    "牛股": -35,
    "熊股": -35,
    "行业十大": -25,
    "个股点评": -25,
    "股市要闻": -30,
    "主力研究": -35,
    "基金重仓": -40,
    "抢筹": -35,
    "成交额前十大": -45,
    "风潮": -25,
    "龙头": -15,
    "牛散": -20,
    "涨停": -20,
    "收跌": -20,
    "收涨": -20,
}

MIN_ACCEPTABLE_NEWS_SCORE = 35

STOCKS = [
    ("CPO", "铭普光磁", "002902"),
    ("CPO", "天孚通信", "300394"),
    ("CPO", "长飞光纤", "601869"),
    ("CPO", "杭电股份", "603618"),
    ("CPO", "永鼎股份", "600105"),
    ("CPO", "瑞斯康达", "603803"),
    ("CPO", "智立方", "301312"),
    ("CPO", "中际旭创", "300308"),
    ("CPO", "亨通光电", "600487"),
    ("CPO", "新易盛", "300502"),
    ("创新药", "万邦德", "002082"),
    ("创新药", "九安医疗", "002432"),
    ("创新药", "津药药业", "600488"),
    ("创新药", "凯莱英", "002821"),
    ("创新药", "联环药业", "600513"),
    ("创新药", "睿智医药", "300149"),
    ("创新药", "广生堂", "300436"),
    ("创新药", "美诺华", "603538"),
    ("创新药", "海特生物", "300683"),
    ("创新药", "昭衍新药", "603127"),
    ("商业航天", "神剑股份", "002361"),
    ("商业航天", "再升科技", "603601"),
    ("商业航天", "西部材料", "002149"),
    ("商业航天", "航天发展", "000547"),
    ("商业航天", "广联航空", "300900"),
    ("商业航天", "江顺科技", "001400"),
    ("商业航天", "巨力索具", "002342"),
    ("商业航天", "中衡设计", "603017"),
    ("商业航天", "通宇通讯", "002792"),
    ("商业航天", "顺灏股份", "002565"),
    ("算力", "奥瑞德", "600666"),
    ("算力", "美利云", "000815"),
    ("算力", "恒润股份", "603985"),
    ("算力", "宏景科技", "301396"),
    ("算力", "佳力图", "603912"),
    ("算力", "光环新网", "300383"),
    ("算力", "电光科技", "002730"),
    ("算力", "数据港", "603881"),
    ("算力", "顺网科技", "300113"),
    ("算力", "网宿科技", "300017"),
    ("算电协同", "协鑫能科", "002015"),
    ("算电协同", "新中港", "605162"),
    ("算电协同", "东南网架", "002135"),
    ("算电协同", "锡华科技", "603248"),
    ("算电协同", "金开新能", "600821"),
    ("算电协同", "东阳光", "600673"),
    ("算电协同", "中国能建", "601868"),
    ("算电协同", "远光软件", "002063"),
    ("算电协同", "易事特", "300376"),
    ("算电协同", "九洲集团", "300040"),
    ("年报/业绩", "源杰科技", "688498"),
    ("年报/业绩", "赛诺医疗", "688108"),
    ("年报/业绩", "先导智能", "300450"),
    ("年报/业绩", "新强联", "300850"),
    ("年报/业绩", "通达股份", "002560"),
    ("年报/业绩", "神通科技", "605228"),
    ("年报/业绩", "北摩高科", "002985"),
    ("年报/业绩", "寒武纪", "688256"),
    ("年报/业绩", "博杰股份", "002975"),
    ("年报/业绩", "南网能源", "003035"),
    ("锂电池", "天际股份", "002759"),
    ("锂电池", "诺德股份", "600110"),
    ("锂电池", "大东南", "002263"),
    ("锂电池", "孚日股份", "002083"),
    ("锂电池", "圣阳股份", "002580"),
    ("锂电池", "铜冠铜箔", "301217"),
    ("锂电池", "杉杉股份", "600884"),
    ("锂电池", "多氟多", "002407"),
    ("锂电池", "丽岛新材", "603937"),
    ("锂电池", "鹏欣资源", "600490"),
    ("PCB", "宏和科技", "603256"),
    ("PCB", "沪电股份", "002463"),
    ("PCB", "中京电子", "002579"),
    ("PCB", "鹏鼎控股", "002938"),
    ("PCB", "山东玻纤", "605006"),
    ("PCB", "大族激光", "002008"),
    ("PCB", "国际复材", "301526"),
    ("PCB", "本川智能", "300964"),
    ("PCB", "深南电路", "002916"),
    ("PCB", "胜宏科技", "300476"),
]


@dataclass
class NewsItem:
    title: str
    link: str
    pub_dt: datetime | None
    source: str
    score: int


def code_to_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = title.replace("｜", "-").replace("|", "-").replace("_", "-")
    return title


def summarize_title(title: str, name: str) -> str:
    cleaned = clean_title(title)
    cleaned = re.sub(rf"^{re.escape(name)}[（(]?\d{{6}}[)）]?", "", cleaned).strip(" -:")
    cleaned = re.sub(r"\s*-\s*(证券之星|中金在线|Sohu|新浪财经|东方财富|中财网|财联社|同花顺财经|界面新闻|第一财经).*$", "", cleaned)
    return cleaned or clean_title(title)


def score_title(title: str, pub_dt: datetime | None) -> int:
    score = 0
    for keyword, value in POSITIVE_KEYWORDS.items():
        if keyword in title:
            score += value
    for keyword, value in NEGATIVE_KEYWORDS.items():
        if keyword in title:
            score += value
    if pub_dt is not None:
        age_hours = max((AS_OF - pub_dt).total_seconds() / 3600.0, 0)
        if age_hours <= 24:
            score += 40
        elif age_hours <= 48:
            score += 26
        elif age_hours <= 72:
            score += 14
        else:
            score -= 50
    return score


def parse_pub_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt.astimezone(AS_OF.tzinfo)
    except Exception:
        return None


def fetch_news_candidates(name: str, code: str, session: requests.Session) -> list[NewsItem]:
    query = urllib.parse.quote(f"A股 {name} {code}")
    url = NEWS_TMPL.format(query=query)
    resp = session.get(url, headers={"User-Agent": UA}, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    items: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        title = clean_title((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        pub = parse_pub_date((item.findtext("pubDate") or "").strip())
        source = (item.findtext("source") or "").strip()
        if not title or not link:
            continue
        if name not in title and code not in title:
            continue
        if pub is not None and pub < AS_OF - timedelta(days=NEWS_LOOKBACK_DAYS):
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                pub_dt=pub,
                source=source,
                score=score_title(title, pub),
            )
        )
    items.sort(key=lambda x: (x.score, x.pub_dt or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return items


def pick_best_news(name: str, code: str, session: requests.Session) -> dict[str, str]:
    try:
        candidates = fetch_news_candidates(name, code, session)
    except Exception:
        return {
            "news_time": "",
            "news_title": "",
            "news_summary": "",
            "news_link": "",
            "news_score": "",
        }

    if not candidates:
        return {
            "news_time": "",
            "news_title": "",
            "news_summary": "",
            "news_link": "",
            "news_score": "",
        }

    best = candidates[0]
    if best.score < MIN_ACCEPTABLE_NEWS_SCORE:
        return {
            "news_time": "",
            "news_title": "",
            "news_summary": "",
            "news_link": "",
            "news_score": str(best.score),
        }
    news_time = best.pub_dt.strftime("%Y-%m-%d %H:%M:%S %z") if best.pub_dt else ""
    return {
        "news_time": news_time,
        "news_title": best.title,
        "news_summary": summarize_title(best.title, name),
        "news_link": best.link,
        "news_score": str(best.score),
    }


def fetch_5day(code: str, session: requests.Session) -> list[dict[str, str | float | None]]:
    start = (AS_OF.date() - timedelta(days=20)).strftime("%Y-%m-%d")
    end = AS_OF.date().strftime("%Y-%m-%d")
    symbol = code_to_symbol(code)
    params = {"param": f"{symbol},day,{start},{end},640,"}
    try:
        resp = session.get(TENCENT_KLINE_API, params=params, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []

    data = (payload.get("data") or {}).get(symbol) or {}
    series = data.get("day") or data.get("qfqday") or []
    rows: list[dict[str, str | float | None]] = []
    for item in series:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            day = str(item[0])
            close = float(item[2])
            volume_hand = float(item[5])
        except Exception:
            continue
        rows.append({"date": day, "close": close, "volume_hand": volume_hand, "pct_chg": None})

    rows.sort(key=lambda x: str(x["date"]))
    for i in range(1, len(rows)):
        prev_close = float(rows[i - 1]["close"])
        curr_close = float(rows[i]["close"])
        rows[i]["pct_chg"] = (curr_close / prev_close - 1.0) * 100.0 if prev_close else None
    return rows[-5:]


def fetch_one(stock: tuple[str, str, str]) -> dict[str, str]:
    group, name, code = stock
    session = requests.Session()
    news = pick_best_news(name, code, session)
    perf = fetch_5day(code, session)
    record: dict[str, str] = {"group": group, "name": name, "code": code, **news}
    for idx in range(5):
        key = idx + 1
        if idx < len(perf):
            row = perf[idx]
            record[f"d{key}_date"] = str(row["date"])
            pct = row["pct_chg"]
            record[f"d{key}_pct"] = "" if pct is None else f"{pct:.2f}"
            record[f"d{key}_vol_hand"] = f"{float(row['volume_hand']):.0f}"
        else:
            record[f"d{key}_date"] = ""
            record[f"d{key}_pct"] = ""
            record[f"d{key}_vol_hand"] = ""
    return record


def build_markdown(rows: list[dict[str, str]]) -> str:
    lines = [
        "# 四月份八大核心赛道梳理：高价值新闻与近五日量价（2026-04-02）",
        "",
        "规则：只保留近 3 天新闻，优先公告/业绩/订单/产品/监管，弱化异动快报和资金流稿。",
        "",
        "| 板块 | 股票 | 代码 | 高价值新闻摘要 | 近五日涨跌/成交量 |",
        "|---|---|---:|---|---|",
    ]
    for row in rows:
        news = row["news_summary"] or "近3天未筛到高价值新闻"
        if row["news_link"]:
            news = f"[{news}]({row['news_link']})"
        perf = []
        for idx in range(1, 6):
            day = row[f"d{idx}_date"]
            if not day:
                continue
            pct = row[f"d{idx}_pct"]
            vol = row[f"d{idx}_vol_hand"]
            pct_text = "-" if pct == "" else f"{float(pct):+.2f}%"
            vol_text = "-" if vol == "" else f"{int(vol):,}手"
            perf.append(f"{day}: {pct_text}, {vol_text}")
        lines.append(
            f"| {row['group']} | {row['name']} | {row['code']} | {news} | {'<br>'.join(perf)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_one, stock) for stock in STOCKS]
        for future in as_completed(futures):
            rows.append(future.result())

    order = {(group, name, code): idx for idx, (group, name, code) in enumerate(STOCKS)}
    rows.sort(key=lambda x: order[(x["group"], x["name"], x["code"])])

    csv_path = out_dir / "april_core_stocks_news_5day_2026-04-02_high_signal.csv"
    fieldnames = ["group", "name", "code", "news_time", "news_title", "news_summary", "news_link", "news_score"]
    for idx in range(1, 6):
        fieldnames.extend([f"d{idx}_date", f"d{idx}_pct", f"d{idx}_vol_hand"])

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    md_path = out_dir / "april_core_stocks_news_5day_2026-04-02_high_signal.md"
    md_path.write_text(build_markdown(rows), encoding="utf-8")

    print(csv_path)
    print(md_path)
    print(len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
