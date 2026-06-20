#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path

import requests
from pypdf import PdfReader

try:
    import efinance as ef
    from efinance.shared import session as efinance_session
except Exception:
    ef = None
    efinance_session = None

REFRESH_TOKEN = (
    "eyJzaWduX3RpbWUiOiIyMDI2LTA0LTAyIDEwOjIxOjMyIn0=."
    "eyJ1aWQiOiI4NTQ5NDc3MzYiLCJ1c2VyIjp7InJlZnJlc2hUb2tlbkV4cGlyZWRUaW1lIjoiMjAyNi0wNC0yNCAxMDozMTozNyIsInVzZXJJZCI6Ijg1NDk0NzczNiJ9fQ==."
    "B872475D7BE9A6E269993547F2ABABFC4F9488AA06D37A03114C925B8BE163AE"
)
BASE_URL = "https://quantapi.51ifind.com/api/v1"
FUNDFLOW_API = "https://datacenter-web.eastmoney.com/api/data/get"
LIMIT_UP_POOL_API = "https://push2ex.eastmoney.com/getTopicZTPool"
QUOTE_API = "https://push2.eastmoney.com/api/qt/stock/get"
OUT_DIR = Path("reports")
OUT_DIR.mkdir(exist_ok=True)
WATCHLIST_STATE_PATH = OUT_DIR / "watchlist_state.json"
RESEARCH_REPORTS_PATH = OUT_DIR / "research_reports.json"
SOCIAL_MEDIA_POSTS_PATH = OUT_DIR / "social_media_posts.json"
SOCIAL_KOL_WATCHLIST_PATH = OUT_DIR / "social_kol_watchlist.json"
TRUST_ENV = os.environ.get("ASTOCK_TRUST_ENV", "1").strip().lower() not in {"0", "false", "no", "off"}
AS_OF_OVERRIDE = os.environ.get("ASTOCK_AS_OF", "").strip()
AS_OF = date.fromisoformat(AS_OF_OVERRIDE) if AS_OF_OVERRIDE else datetime.now(timezone(timedelta(hours=8))).date()
DEEP_DIVE_CSV = OUT_DIR / "seven_stocks_deep_dive_2026-04-02.csv"
POST_CLOSE_REPORT = Path(f"post_close_result_{AS_OF.isoformat()}.md")
AS_OF_DT = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
NEWS_TMPL = "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
NEWS_LOOKBACK_DAYS = 3
MIN_ACCEPTABLE_NEWS_SCORE = 35
STRONG_MIN_PCT = 5.0
STRONG_MIN_TOTAL_CAP = 8e9
STRONG_MAX_TOTAL_CAP = 1.2e11
STRONG_MIN_AMOUNT = 5e8
STRONG_MIN_TURNOVER = 5.0
STRONG_MAX_TURNOVER = 25.0
STRONG_DISPLAY_LIMIT = 20
MARKET_INDEXES = [
    ("创业板指", "399006.SZ"),
    ("沪深300", "000300.SH"),
    ("科创50", "000688.SH"),
]
INDEX_DETAIL_MAP = {
    "399006.SZ": {
        "title": "创业板指详情",
        "note": "代表性权重股参考",
        "leaders": [
            {"name": "宁德时代", "code": "300750.SZ", "tag": "新能源权重核心"},
            {"name": "东方财富", "code": "300059.SZ", "tag": "平台型券商互联网龙头"},
            {"name": "迈瑞医疗", "code": "300760.SZ", "tag": "医疗器械龙头"},
            {"name": "汇川技术", "code": "300124.SZ", "tag": "工控自动化龙头"},
            {"name": "中际旭创", "code": "300308.SZ", "tag": "光模块核心权重"},
            {"name": "新易盛", "code": "300502.SZ", "tag": "高速光模块弹性"},
        ],
    },
    "000688.SH": {
        "title": "科创50详情",
        "note": "代表性权重股参考",
        "leaders": [
            {"name": "海光信息", "code": "688041.SH", "tag": "国产算力权重核心"},
            {"name": "寒武纪", "code": "688256.SH", "tag": "AI芯片高弹性龙头"},
            {"name": "中芯国际", "code": "688981.SH", "tag": "晶圆制造龙头"},
            {"name": "澜起科技", "code": "688008.SH", "tag": "服务器芯片/内存接口"},
            {"name": "金山办公", "code": "688111.SH", "tag": "软件平台型权重"},
            {"name": "传音控股", "code": "688036.SH", "tag": "消费电子出海核心"},
        ],
    },
}
REPORT_MODAL_NAME_MAP = {
    "江海股份": "002484.SZ",
    "江丰电子": "300666.SZ",
    "绿的谐波": "688017.SH",
}


def load_report_name_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for path in sorted(OUT_DIR.glob("watchlist_dashboard_*.json")) + sorted(OUT_DIR.glob("watchlist_strong_stocks_*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            code = row.get("code")
            name = row.get("name")
            if code and name and name not in index:
                index[str(name)] = str(code)
    for name, code in REPORT_MODAL_NAME_MAP.items():
        index.setdefault(name, code)
    return index


def load_watchlist_state_payload() -> dict[str, object]:
    if not WATCHLIST_STATE_PATH.exists():
        return {"watchlistStatus": {}, "strongJoinStatus": {}}
    try:
        payload = json.loads(WATCHLIST_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"watchlistStatus": {}, "strongJoinStatus": {}}
    return {
        "watchlistStatus": payload.get("watchlistStatus") or {},
        "strongJoinStatus": payload.get("strongJoinStatus") or {},
        "updatedAt": payload.get("updatedAt") or "",
    }


def normalize_target_label(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return "" if text == "[object Object]" else text
    if isinstance(value, dict):
        for key in (
            "name",
            "stockName",
            "shortName",
            "company",
            "target",
            "label",
            "title",
            "code",
            "symbol",
        ):
            text = normalize_target_label(value.get(key))
            if text:
                return text
        for nested in value.values():
            text = normalize_target_label(nested)
            if text:
                return text
    return ""


def normalize_target_list(values, limit: int = 12) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = normalize_target_label(value)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def load_research_report_entries() -> list[dict]:
    if not RESEARCH_REPORTS_PATH.exists():
        return []
    try:
        payload = json.loads(RESEARCH_REPORTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        target = normalize_target_label(item.get("target"))
        content = str(item.get("content") or "").strip()
        summary = str(item.get("summary") or content).strip()
        industry = str(item.get("industry") or "未分类").strip()
        targets = normalize_target_list(item.get("targets") or [])
        if not summary:
            continue
        if not targets and target:
            targets = [target]
        out.append(
            {
                "id": str(item.get("id") or f"report-{idx}"),
                "target": target or (targets[0] if targets else ""),
                "targets": targets,
                "content": content,
                "summary": summary,
                "industry": industry,
                "rawText": str(item.get("rawText") or item.get("content") or ""),
                "date": str(item.get("date") or ""),
                "createdAt": int(item.get("createdAt") or idx),
            }
        )
    return out


def load_social_media_entries() -> list[dict]:
    if not SOCIAL_MEDIA_POSTS_PATH.exists():
        return []
    try:
        payload = json.loads(SOCIAL_MEDIA_POSTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        kol = str(item.get("kol") or "").strip()
        content = str(item.get("content") or item.get("summary") or "").strip()
        summary = str(item.get("summary") or content).strip()
        industry = str(item.get("industry") or "").strip()
        targets = normalize_target_list(item.get("targets") or [])
        target = normalize_target_label(item.get("target")) or (targets[0] if targets else "")
        if not kol or not summary:
            continue
        source_url = str(item.get("sourceUrl") or item.get("tweetUrl") or item.get("url") or "").strip()
        out.append(
            {
                "id": str(item.get("id") or f"social-{idx}"),
                "kol": kol,
                "handle": str(item.get("handle") or "").strip(),
                "platform": str(item.get("platform") or "X"),
                "target": target,
                "content": content,
                "summary": summary,
                "industry": industry,
                "targets": targets if targets else ([target] if target else []),
                "rawText": str(item.get("rawText") or item.get("content") or ""),
                "translatedText": str(item.get("translatedText") or item.get("translation") or ""),
                "sourceUrl": source_url,
                "tweetUrl": source_url,
                "sourceNote": str(item.get("sourceNote") or ""),
                "date": str(item.get("date") or ""),
                "createdAt": int(item.get("createdAt") or idx),
            }
        )
    return out


def load_social_kol_watchlist() -> list[dict]:
    if not SOCIAL_KOL_WATCHLIST_PATH.exists():
        return []
    try:
        payload = json.loads(SOCIAL_KOL_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle") or item.get("id") or "").strip().lstrip("@")
        if not handle:
            continue
        out.append(
            {
                "id": str(item.get("id") or f"kol-{idx}"),
                "name": str(item.get("name") or handle),
                "handle": handle,
                "platform": str(item.get("platform") or "X"),
                "enabled": bool(item.get("enabled", True)),
                "createdAt": int(item.get("createdAt") or idx),
            }
        )
    return out


def score_report_modal_row(row: dict) -> int:
    score = 0
    if row.get("kline"):
        score += 5
    if row.get("latestNews", {}).get("summary"):
        score += 3
    if row.get("topHolders", {}).get("holders"):
        score += 3
    if row.get("businessSegments", {}).get("items"):
        score += 3
    if row.get("topCustomers", {}).get("customers"):
        score += 2
    if row.get("marginFinancing", {}).get("date"):
        score += 2
    if row.get("research"):
        score += 1
    if row.get("mainBusiness") not in (None, "", "-"):
        score += 1
    return score


def load_historical_report_row(code: str) -> dict | None:
    best_row: dict | None = None
    best_score = -1
    paths = sorted(OUT_DIR.glob("watchlist_dashboard_*.json")) + sorted(OUT_DIR.glob("watchlist_strong_stocks_*.json"))
    for path in paths:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("code") != code:
                continue
            candidate = dict(row)
            score = score_report_modal_row(candidate)
            if score >= best_score:
                best_score = score
                best_row = candidate
    return best_row

INSTITUTION_FUND_CODES = [
    "159915",
    "588000",
    "512480",
    "512660",
    "510300",
    "512170",
    "159995",
    "515790",
]
MAINLINE_KEYWORDS = (
    "CPO",
    "光模块",
    "光通信",
    "光芯片",
    "光器件",
    "PCB",
    "算力",
    "存储",
    "液冷",
    "机器人",
    "连接器",
    "铜缆",
    "高速光",
)
NEWS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

POSITIVE_NEWS_KEYWORDS = {
    "公告": 50,
    "年报": 45,
    "季报": 45,
    "业绩": 40,
    "预增": 35,
    "预减": 30,
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
    "项目": 18,
}

NEGATIVE_NEWS_KEYWORDS = {
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
    "涨停": -20,
    "收跌": -20,
    "收涨": -20,
}

BUSINESS_KEYWORD_MAP = [
    ("存储", "存储/存储芯片"),
    ("存储芯片", "存储/存储芯片"),
    ("内存", "存储/内存接口"),
    ("HBM", "存储/HBM"),
    ("CPO", "光通信/CPO"),
    ("光模块", "光通信/光模块"),
    ("光通信", "光通信"),
    ("铜缆", "铜缆连接"),
    ("服务器", "服务器/算力"),
    ("算力", "算力/智算"),
    ("液冷", "液冷"),
    ("PCB", "PCB"),
    ("芯片", "芯片"),
    ("半导体", "半导体"),
    ("软件", "软件开发"),
    ("广告", "广告营销"),
    ("营销", "广告营销"),
    ("AIGC", "AI应用/营销"),
    ("机器人", "机器人"),
    ("风电", "风电设备"),
    ("锂电", "锂电"),
    ("电池", "电池设备"),
    ("自动化", "自动化设备"),
    ("激光", "激光设备/器件"),
    ("光学", "光学器件"),
]

WATCHLIST_BASE = [
    ("美利云", "000815.SZ"),
    ("智立方", "301312.SZ"),
    ("德科立", "688205.SH"),
    ("福晶科技", "002222.SZ"),
    ("光迅科技", "002281.SZ"),
    ("赛微电子", "300456.SZ"),
    ("腾景科技", "688195.SH"),
    ("拉普拉斯", "688726.SH"),
    ("德明利", "001309.SZ"),
    ("奥瑞德", "600666.SH"),
    ("京运通", "601908.SH"),
    ("中天科技", "600522.SH"),
    ("亨通光电", "600487.SH"),
    ("金风科技", "002202.SZ"),
    ("华胜天成", "600410.SH"),
    ("电光科技", "002730.SZ"),
    ("太辰光", "300570.SZ"),
    ("澜起科技", "688008.SH"),
    ("中润光学", "688307.SH"),
    ("联特科技", "301205.SZ"),
    ("亚翔集成", "603929.SH"),
    ("中船科技", "600072.SH"),
    ("中瓷电子", "003031.SZ"),
    ("天通股份", "600330.SH"),
    ("长光华芯", "688048.SH"),
    ("杰普特", "688025.SH"),
    ("芯动联科", "688582.SH"),
    ("仕佳光子", "688313.SH"),
    ("易天股份", "300812.SZ"),
    ("致尚科技", "301486.SZ"),
    ("海光信息", "688041.SH"),
    ("利通电子", "603629.SH"),
    ("浪潮信息", "000977.SZ"),
    ("宏景科技", "301396.SZ"),
    ("光环新网", "300383.SZ"),
    ("光库科技", "300620.SZ"),
    ("华工科技", "000988.SZ"),
    ("协创数据", "300857.SZ"),
    ("信维通信", "300136.SZ"),
    ("德福科技", "301511.SZ"),
    ("金海通", "603061.SH"),
    ("沃格光电", "603773.SH"),
    ("科士达", "002518.SZ"),
    ("应流股份", "603308.SH"),
    ("汇川技术", "300124.SZ"),
    ("富创精密", "688409.SH"),
]

MAIN_BUSINESS_MAP = {
    "000815.SZ": "IDC/国资云",
    "301312.SZ": "光模块设备/CPO检测",
    "688205.SH": "高速光模块/CPO",
    "002222.SZ": "光学晶体/激光器件",
    "688726.SH": "光通信设备/激光装备",
    "001309.SZ": "存储主控/存储模组",
    "600666.SH": "算力租赁/智算服务",
    "601908.SH": "硅片/光伏电站",
    "600522.SH": "海缆/光通信",
    "600487.SH": "光通信/CPO/海缆",
    "002202.SZ": "风机整机/风电",
    "600410.SH": "算力平台/系统集成",
    "002821.SZ": "创新药CXO/CDMO",
    "603296.SH": "AI硬件ODM/服务器",
    "600105.SH": "光通信/铜缆连接",
    "600673.SH": "液冷材料/制冷剂",
    "688498.SH": "高速光芯片/CPO",
    "002796.SZ": "机柜/通信设备",
    "688195.SH": "光学元件/CPO",
    "300620.SZ": "光通信器件/激光器件",
    "300666.SZ": "半导体材料/高端靶材",
    "002281.SZ": "光模块/光器件/CPO",
    "300456.SZ": "MEMS/晶圆制造",
    "002730.SZ": "矿用防爆/算力电源配套",
    "300570.SZ": "高速光模块/光器件",
    "688008.SH": "内存接口芯片/服务器芯片",
    "688307.SH": "机器视觉/精密光学",
    "301205.SZ": "高速光模块/CPO",
    "603929.SH": "洁净工程/半导体厂务",
    "600072.SH": "船舶装备/新能源工程",
    "003031.SZ": "电子陶瓷/氮化镓外延",
    "600330.SH": "磁性材料/蓝宝石/衬底",
    "688048.SH": "高功率激光芯片",
    "688025.SH": "激光器/激光装备",
    "688582.SH": "高性能MEMS惯导",
    "688313.SH": "光芯片/PLC器件/CPO",
    "300812.SZ": "显示面板设备/消费电子设备",
    "301486.SZ": "精密连接器/光通信",
    "300395.SZ": "石英材料/半导体耗材",
    "301095.SZ": "EDA/芯片良率分析",
    "300001.SZ": "充电桩/电力设备",
    "300661.SZ": "模拟芯片/电源管理",
    "000591.SZ": "光伏电站/新能源运营",
    "688213.SH": "CIS图像传感器",
    "000767.SZ": "火电/电力运营",
    "000672.SZ": "水泥/骨料",
    "688150.SH": "OLED材料",
    "688785.SH": "算力服务器/液冷整机",
    "300285.SZ": "电子陶瓷/MLCC材料",
    "603920.SH": "PCB",
    "300058.SZ": "AI营销/出海营销",
    "300475.SZ": "存储分销/存储芯片",
    "300679.SZ": "连接器/射频器件",
    "301396.SZ": "算力/智算中心/数字化服务",
    "688322.SH": "3D视觉/机器视觉传感器",
    "002138.SZ": "电感/磁性元件",
    "600552.SH": "显示材料/UTG/电子玻璃",
    "300503.SZ": "主轴/机器人关节/直驱电机",
    "002896.SZ": "减速器/机器人零部件",
    "688777.SH": "工业软件/DCS/流程自动化",
    "600403.SH": "煤炭/能源开采",
    "301196.SZ": "精密注塑/汽车电子部件",
    "300260.SZ": "半导体洁净管阀/生物医药设备",
    "603516.SH": "显控系统/专业AV",
    "300811.SZ": "软磁材料/金属磁粉芯",
    "002792.SZ": "通信天线/卫星通信",
    "300139.SZ": "智能电表/黄金矿业",
    "301171.SZ": "出海营销/AIGC营销",
    "688125.SH": "消费电子自动化设备",
    "688337.SH": "测试测量/示波器",
    "688392.SH": "超声波焊接设备",
    "301018.SZ": "精密空调/液冷温控",
    "300085.SZ": "金融IT/互联网金融",
    "688270.SH": "卫星导航/射频芯片",
    "300166.SZ": "工业互联网/大数据",
    "300017.SZ": "CDN/边缘算力",
    "688676.SH": "干式变压器/储能",
    "301500.SZ": "再生金属/资源循环",
    "688702.SH": "交换芯片/网络芯片",
    "301200.SZ": "PCB专用设备",
    "300986.SZ": "铝模/装配式建筑",
    "300548.SZ": "光模块/光器件",
    "688158.SH": "云计算/智算云",
    "688343.SH": "AI芯片/边缘智能",
    "688535.SH": "半导体封装材料",
    "300113.SZ": "云游戏/算力云",
    "688396.SH": "功率半导体/IDM",
    "300170.SZ": "ERP/企业软件",
    "688603.SH": "PCB电子化学品",
    "300322.SZ": "天线/散热件",
    "688031.SH": "大数据基础软件",
    "688818.SH": "军工电子/卫星通信",
    "300846.SZ": "云计算/算力服务",
    "300785.SZ": "电商导购/AI内容",
    "301489.SZ": "散热材料/消费电子",
    "300364.SZ": "数字阅读/AI内容",
    "300738.SZ": "IDC/算力租赁",
    "688668.SH": "连接器/高速铜缆",
    "688183.SH": "PCB",
    "688258.SH": "信创固件/云服务",
    "300480.SZ": "半导体设备/传感器",
    "300857.SZ": "智能硬件/存储模组",
    "300136.SZ": "天线/射频器件/消费电子",
    "301511.SZ": "电子铜箔/锂电材料",
    "603061.SH": "半导体测试分选设备",
    "300499.SZ": "液冷/电力电子",
    "300383.SZ": "IDC/云计算/算力服务",
    "300762.SZ": "军工通信/卫星互联网",
    "002240.SZ": "锂矿/锂盐",
    "688536.SH": "模拟芯片/信号链芯片",
    "688220.SH": "蜂窝通信芯片/SoC",
    "300672.SZ": "存储芯片/AI视觉芯片",
    "688239.SH": "航空发动机锻件/军工材料",
    "688110.SH": "存储芯片/NAND",
    "688800.SH": "连接器/高速铜缆",
    "688449.SH": "存储主控/SSD主控",
    "688691.SH": "ASIC设计服务/芯片定制",
    "605376.SH": "金属粉体/MLCC材料",
    "002025.SZ": "军工连接器/继电器",
    "603119.SH": "云母绝缘材料/新能源汽车",
    "600246.SH": "IDC/卫星互联网",
    "300634.SZ": "企业短信/AI应用",
    "300184.SZ": "电子元器件分销",
    "300454.SZ": "网络安全/云计算",
    "688123.SH": "存储芯片/模拟芯片",
    "300602.SZ": "电磁屏蔽/散热件",
    "300900.SZ": "航空航天零部件",
    "300418.SZ": "AI应用/海外社交",
    "688226.SH": "母线/储能",
    "688141.SH": "模拟芯片/电源管理",
    "300975.SZ": "电子元器件分销",
    "688531.SH": "X射线检测设备",
    "301563.SZ": "电子元器件B2B/分销",
    "688041.SH": "国产CPU/DCU/算力芯片",
    "603629.SH": "显示结构件/算力服务器",
    "000977.SZ": "AI服务器/算力基础设施",
    "000988.SZ": "光模块/激光装备/传感器",
    "002407.SZ": "六氟磷酸锂/锂电材料",
    "300468.SZ": "金融IT/跨境支付",
    "300866.SZ": "消费电子/品牌出海",
    "300458.SZ": "SoC芯片/智能终端芯片",
    "300390.SZ": "锂电材料/氢氧化锂",
    "300438.SZ": "锂电池/储能电芯",
    "300118.SZ": "光伏组件/储能",
    "000762.SZ": "锂矿/盐湖提锂",
    "603026.SH": "电解液溶剂/新能源材料",
    "600345.SH": "轨交通信/北斗应用",
    "600338.SH": "锂盐/有色金属资源",
    "603773.SH": "玻璃基板/光电材料",
    "688275.SH": "磷酸铁锂/正极材料",
    "000967.SZ": "环卫装备/环保设备",
    "600166.SH": "商用车/重卡",
    "002083.SZ": "家纺/热电",
    "600499.SH": "建材机械/锂电装备",
    "000559.SZ": "汽车零部件/底盘系统",
    "603256.SH": "电子布/覆铜板材料",
    "300037.SZ": "电解液/氟化工",
    "688503.SH": "光伏银浆",
    "603893.SH": "SoC芯片/AIoT",
    "002104.SZ": "数字人民币/金融IT",
    "300769.SZ": "磷酸铁锂/正极材料",
    "002080.SZ": "风电叶片/玻纤",
    "002922.SZ": "电源/新能源变压器",
    "603659.SH": "负极材料/隔膜涂覆",
    "001203.SZ": "铁矿石/球团矿",
    "000612.SZ": "电解铝/铝加工",
    "603283.SH": "自动化设备/消费电子装备",
    "601778.SH": "光伏电站/绿电运营",
    "600759.SH": "油气开采",
    "601020.SH": "锑矿/有色金属",
    "688180.SH": "创新药/PD-1",
    "603950.SH": "发动机零部件",
    "603083.SH": "光模块/光通信/CPO",
    "688331.SH": "创新药/ADC",
    "603002.SH": "覆铜板/电子树脂",
    "601179.SH": "输变电设备/特高压",
    "603063.SH": "风电变流器/储能逆变器",
    "603444.SH": "网络游戏/游戏平台",
    "600550.SH": "输变电设备/特高压变压器",
    "300903.SZ": "PCB",
    "301358.SZ": "磷酸铁锂/正极材料",
    "002840.SZ": "生猪养殖/肉制品",
    "300681.SZ": "新能源汽车电驱/电控",
    "603890.SH": "消费电子结构件/笔电",
    "002437.SZ": "化学制药/创新药",
    "002519.SZ": "军工电子/智能机电",
    "688059.SH": "硬质合金刀具/数控刀具",
    "688390.SH": "光伏逆变器/储能",
    "688515.SH": "以太网芯片/车载通信芯片",
    "688717.SH": "光伏逆变器/储能系统",
    "600498.SH": "光通信设备/海缆",
    "688017.SH": "谐波减速器/机器人",
    "002428.SZ": "锗材料/光通信材料",
    "300258.SZ": "汽车齿轮/新能源车零部件",
    "001267.SZ": "园林生态/光伏绿电",
    "605358.SH": "硅片/功率半导体",
    "603667.SH": "轴承/机器人零部件",
    "688523.SH": "航空航天零部件/军工装备",
    "002491.SZ": "光通信线缆/网络设备",
    "002655.SZ": "声学器件/智能终端",
    "002929.SZ": "通信运维/算力服务",
    "002865.SZ": "光伏电池/Topcon",
    "600480.SH": "汽车零部件/轻量化",
    "300129.SZ": "海上风电/风电塔筒",
    "002156.SZ": "封测/存储封装",
    "300302.SZ": "存储/灾备软件",
    "688234.SH": "碳化硅衬底/第三代半导体",
    "688167.SH": "激光器/汽车激光雷达",
    "002179.SZ": "军工连接器/光电互连",
    "300693.SZ": "储能逆变器/充电桩",
    "688146.SH": "电子特气/半导体材料",
    "300782.SZ": "射频前端/滤波器",
    "688596.SH": "半导体厂务/特气供应系统",
    "300757.SZ": "光伏设备/自动化产线",
    "002600.SZ": "消费电子精密制造",
    "002506.SZ": "光伏组件/一体化制造",
    "002245.SZ": "锂电池/LED芯片",
    "688143.SH": "特种光纤/光器件",
    "300323.SZ": "LED芯片/Mini LED",
    "003022.SZ": "EVA/光伏材料",
    "002254.SZ": "芳纶/氨纶",
    "600509.SH": "电力/煤电铝一体化",
    "603052.SH": "消费电子功能材料",
    "002518.SZ": "UPS电源/储能",
    "000811.SZ": "制冷设备/工业冷冻",
    "603308.SH": "高端铸件/航空发动机零部件",
    "300124.SZ": "工控自动化/新能源驱动",
    "688409.SH": "半导体设备零部件",
    "300223.SZ": "存储芯片/MCU/车规芯片",
    "688585.SH": "复合材料/风电材料",
    "688300.SH": "电子封装材料/球形硅微粉",
    "688333.SH": "金属3D打印/航空航天零部件",
    "301526.SZ": "玻纤/复合材料",
    "300779.SZ": "固废处理/资源化利用",
    "600726.SH": "火电/煤电",
    "000510.SZ": "氯碱化工/新材料",
    "600110.SH": "铜箔/锂电材料",
    "600367.SH": "钡盐/锰系材料",
    "605196.SH": "电线电缆/电力设备",
    "603608.SH": "鞋履制造/时尚消费",
    "300100.SZ": "汽车零部件/智能底盘",
    "688308.SH": "硬质合金刀具",
    "688116.SH": "碳纳米管导电浆料",
    "000712.SZ": "券商/金融服务",
    "300353.SZ": "工业互联网/网络通信",
    "300763.SZ": "光伏逆变器/储能逆变器",
    "688690.SH": "纳米微球/色谱填料",
    "000636.SZ": "MLCC/电子元件",
    "002273.SZ": "光学元件/AR光波导",
    "301319.SZ": "焊接材料/电子装联材料",
    "600863.SH": "火电/风光电力",
    "688478.SH": "半导体晶体生长设备",
    "603663.SH": "锆系材料/镁铝合金",
    "002251.SZ": "零售商超/消费复苏",
    "605289.SH": "城市照明/文旅夜游",
    "601991.SH": "火电/绿电运营",
    "688108.SH": "冠脉支架/医疗器械",
    "002484.SZ": "铝电解电容/薄膜电容",
    "601888.SH": "免税零售/旅游消费",
    "603618.SH": "电线电缆/电力设备",
    "600021.SH": "火电/电力运营",
    "002081.SZ": "建筑装饰/装修工程",
    "600869.SH": "电线电缆/储能",
    "603011.SH": "锻压设备/机器人装备",
    "001389.SZ": "PCB/高多层板",
    "603938.SH": "三氯氢硅/有机硅",
    "600693.SH": "商业零售/仓储物流",
    "002436.SZ": "PCB/封装基板",
    "688772.SH": "消费电池/锂电池",
    "300377.SZ": "金融IT/证券软件",
    "600601.SH": "PCB/电子制造",
    "688388.SH": "锂电铜箔/锂电材料",
    "000960.SZ": "锡/有色金属",
    "300803.SZ": "金融信息服务/互联网券商",
    "301323.SZ": "磁性材料/功能材料",
    "002812.SZ": "锂电隔膜/锂电材料",
    "001696.SZ": "通机发动机/低空动力",
    "301275.SZ": "零售数字化/电子价签",
    "688003.SH": "机器视觉/工业检测设备",
    "000737.SZ": "铜/有色金属",
    "601609.SH": "铜加工/铜材",
    "688114.SH": "基因测序/生命科学仪器",
    "688757.SH": "半导体检测/第三方实验室",
    "600378.SH": "氟化工/电子气体/高端材料",
    "002183.SZ": "供应链服务/消费分销",
    "002387.SZ": "OLED显示/柔性屏",
    "603360.SH": "工业杀菌剂/精细化工",
    "300976.SZ": "消费电子功能器件",
    "601636.SH": "浮法玻璃/光伏玻璃",
    "688325.SH": "电池管理芯片/模拟芯片",
    "002636.SZ": "覆铜板/PCB材料",
    "300489.SZ": "红外材料/红外光学",
    "000785.SZ": "家居零售/家装卖场",
    "603203.SH": "电子装联设备/精密焊接",
    "603778.SH": "光伏组件/新能源",
    "002747.SZ": "工业机器人/伺服系统",
    "688519.SH": "覆铜板/电子材料",
    "603859.SH": "工业软件/智能制造",
    "688787.SH": "AI数据服务/训练数据",
    "002931.SZ": "机械零部件/液压元件",
    "002354.SZ": "数字营销/AI应用",
    "603823.SH": "颜料/精细化工",
    "688126.SH": "半导体硅片",
    "601208.SH": "电子材料/绝缘材料",
    "688662.SH": "热电器件/半导体热管理",
    "300331.SZ": "微纳光学/光学材料",
    "688809.SH": "半导体设备/检测服务",
    "000970.SZ": "稀土永磁/磁性材料",
    "603650.SH": "半导体材料/光刻胶",
    "603186.SH": "覆铜板/复合材料",
    "002585.SZ": "光学膜/新材料",
    "300263.SZ": "电子新材料/节能环保",
    "300209.SZ": "商业航天/卫星应用",
    "688548.SH": "电子大宗气体/工业气体",
    "688362.SH": "封测/先进封装",
    "301389.SZ": "电磁屏蔽材料/消费电子",
    "000688.SZ": "铅锌矿/有色金属",
    "688102.SH": "高温合金/特种材料",
    "000962.SZ": "钽铌金属/稀有金属",
    "600353.SH": "电真空器件/军工电子",
}

RESEARCH_FALLBACK_MAP = {
    "000815.SZ": {
        "2025营收": "截至2026-04-03未见正式披露2025全年营收；2025H1营收1.74亿元，-64.73%；2025Q1营收8753.30万元，-62.84%。",
        "同比": "全年同比暂未正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单；当前公开口径仍以机架、带宽、运维租赁为主，尚未直接提供GPU算力服务。",
        "核心逻辑分析": "核心逻辑是中卫绿色数据中心和国资云资产重估，不是纯AI算力训练弹性。它更像IDC/云底座资产，估值上限取决于上架率、租赁单价和后续是否延伸到更高附加值算力运营。",
        "核心用户及订单": "公开资料未逐一披露核心租户和新签订单金额，目前业务形态仍偏IDC租赁型客户。",
        "核心竞争力": "优势在于西部低成本绿电+机房资源禀赋，以及国资背景带来的资源整合能力。",
        "备注": "这只票要把“算力概念”与“实际业务”分开看。",
    },
    "301312.SZ": {
        "2025营收": "截至2026-04-03未见正式披露2025年报。",
        "同比": "暂未披露。",
        "2026新订单/新增项目": "未见明确订单金额披露，市场交易主线集中在光模块设备、CPO检测与高速光通信景气。",
        "核心逻辑分析": "公司处在高速光通信设备链条里，弹性来自光模块、CPO、硅光等高景气方向资本开支扩张。若头部客户继续扩产，它作为设备供应商通常先受益于上游扩产节奏。",
        "核心用户及订单": "公开口径未完整实名披露，客户通常分布于光通信与消费电子制造链。",
        "核心竞争力": "核心在高精度自动化设备和检测能力，能够切入高端光通信设备产线。",
        "备注": "更适合按设备弹性股理解，而不是按平台型龙头理解。",
    },
    "688205.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待公司正式披露口径确认。",
        "2026新订单/新增项目": "未见单独披露新签订单金额，市场关注点集中在高速光模块、800G/1.6T与CPO链条景气。",
        "核心逻辑分析": "德科立是高速光模块链条的重要弹性标的，景气来自AI算力拉动下的数据中心高速互联升级。它本质上赚的是光模块升级和份额提升的钱。",
        "核心用户及订单": "公开资料通常不逐一实名披露客户，客户群体以主流光通信和数据中心链条客户为主。",
        "核心竞争力": "强项在高速率产品迭代、光模块产品矩阵和数据中心场景渗透。",
        "备注": "适合放在CPO/高速光互联主线里跟踪。",
    },
    "002222.SZ": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单金额，市场关注点在光学晶体、激光和AI光通信材料需求。",
        "核心逻辑分析": "福晶科技的底层逻辑是光学晶体与激光器件平台，受益于激光、红外和部分光通信材料需求扩张。它不是最强弹性的终端环节，但具备上游材料卡位价值。",
        "核心用户及订单": "客户分布在激光、光电子和科研工业链条，公开信息未完整实名披露全部头部客户。",
        "核心竞争力": "核心壁垒在非线性光学晶体、生长和加工工艺积累，属于材料端长期壁垒。",
        "备注": "更偏材料平台型公司，节奏通常慢于下游主题股。",
    },
    "688726.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单金额，关注光通信设备及激光装备的资本开支周期。",
        "核心逻辑分析": "拉普拉斯更偏设备和工艺装备弹性，受益于光通信与高端制造资本开支回暖。交易逻辑更偏“设备先行”。",
        "核心用户及订单": "客户结构通常分布在光通信、高端制造及工艺设备需求方。",
        "核心竞争力": "设备制造与工艺整合能力是核心，若下游扩产则容易获取订单弹性。",
        "备注": "适合放在设备景气链里看。",
    },
    "001309.SZ": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单，市场焦点集中在存储主控、嵌入式存储和高端模组景气。",
        "核心逻辑分析": "德明利本质是存储景气周期与国产替代共振标的。存储价格上行时，主控和模组链条盈利弹性会被迅速放大。",
        "核心用户及订单": "客户覆盖消费电子、存储模组与终端应用链，公开资料未完整逐一实名披露。",
        "核心竞争力": "强项在主控设计、模组整合以及存储价格周期中的经营弹性。",
        "备注": "这只更偏存储景气股，不属于光通信主线。",
    },
    "600666.SH": {
        "2025营收": "截至2026-04-03未见正式披露2025全年营收；2025前三季度营收3.48亿元，+19.54%。",
        "同比": "全年同比暂未正式披露。",
        "2026新订单/新增项目": "未单独披露2026新签订单；已公告扩展算力综合服务及相关技术服务采购协议。",
        "核心逻辑分析": "奥瑞德的核心不是老蓝宝石业务，而是“困境反转 + 算力租赁/智算服务”双主线。若算力业务继续放量，估值锚可能从重组反转股切换到小市值算力服务弹性股。",
        "核心用户及订单": "公开口径称客户扩展至人工智能和大模型场景，但未完整实名披露具体客户清单。",
        "核心竞争力": "优势在于现有资产底座+转型智算服务的弹性；风险在于盈利质量和持续兑现仍需跟踪。",
        "备注": "高弹性也高波动，适合放在情绪和兑现双线跟踪。",
    },
    "601908.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单金额，跟踪点在硅片、光伏电站运营和新能源资产处置节奏。",
        "核心逻辑分析": "京运通更偏光伏制造与电站运营双属性，弹性主要看产业链价格与资产回报，不属于AI主线。",
        "核心用户及订单": "客户与订单更多分布于新能源制造和电站业务，公开信息未做完整实名披露。",
        "核心竞争力": "核心在新能源资产和制造基础，而非主题性科技弹性。",
        "备注": "若放在同一看板里，更多是做风格对照。",
    },
    "600522.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪重点通常在海缆、电网投资与光通信业务景气。",
        "核心逻辑分析": "中天科技是典型的‘海缆/电网/光通信’平台型龙头，胜在大体量和确定性，不胜在纯题材弹性。若你要做稳健主线观察，它是核心中军。",
        "核心用户及订单": "客户主要分布在电网、电信运营商和能源基础设施项目，订单属性偏大项目。",
        "核心竞争力": "强项在海缆、电力传输与通信产品的一体化平台能力，适合承接大项目。",
        "备注": "偏中军，不是妖股型标的。",
    },
    "600487.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露2026新签订单金额，市场主线聚焦光通信、海缆和CPO映射。",
        "核心逻辑分析": "亨通光电本质是‘海缆中军 + 光通信景气受益者’。海缆给确定性，光通信给弹性，因此它兼具机构偏好的稳健性和主题阶段的进攻性。",
        "核心用户及订单": "客户以运营商、电网和大型工程客户为主，公开资料未完整逐一实名披露。",
        "核心竞争力": "优势在平台大、业务线多、订单承接能力强，是典型景气主线中军。",
        "备注": "交易上比小票慢，但趋势通常更稳。",
    },
    "002202.SZ": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单金额，跟踪重点在风机招标、出海和海风项目推进。",
        "核心逻辑分析": "金风科技是风电整机龙头，交易逻辑主要跟风电装机周期和海风/出海进展走，不属于AI科技主线。",
        "核心用户及订单": "客户主要是能源开发商和风电项目主体，订单大多体现为招标中标和项目储备。",
        "核心竞争力": "龙头规模、整机技术和项目交付经验是核心护城河。",
        "备注": "属于新能源主线观察标的。",
    },
    "600410.SH": {
        "2025营收": "截至2026-04-03未在当前本地研究表中补入正式年报口径。",
        "同比": "待正式披露。",
        "2026新订单/新增项目": "未单独披露新签订单金额，交易重心在算力平台、系统集成和题材映射。",
        "核心逻辑分析": "华胜天成是典型算力平台映射标的，更多赚的是市场对算力平台和系统集成能力的预期，而不是像硬件龙头那样靠极强业绩确定性驱动。",
        "核心用户及订单": "客户主要分布于政府、企业数字化和算力平台相关场景，订单偏项目型。",
        "核心竞争力": "系统集成、软件平台和算力概念映射是主要优势，但纯度不如专用算力硬件龙头。",
        "备注": "更适合当情绪和风格观察位。",
    },
    "002281.SZ": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，市场主线围绕800G/1.6T光模块、硅光与CPO升级。",
        "核心逻辑分析": "光迅科技是国内光器件与光模块核心平台之一，逻辑在于AI算力推动数据中心高速互联升级，产品升级和份额提升会同步增厚业绩。",
        "核心用户及订单": "客户主要分布于运营商、数据中心和光通信设备链，公开口径未完整实名披露全部头部客户。",
        "核心竞争力": "核心壁垒在光器件+光模块一体化能力、产品谱系广和量产交付能力。",
        "备注": "偏主线中军，不是最极致的小票弹性。",
    },
    "300456.SZ": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪重点在MEMS、晶圆制造和半导体工艺平台进展。",
        "核心逻辑分析": "赛微电子的逻辑在于MEMS与晶圆制造平台卡位，兼具国产替代和工艺平台弹性，不是单纯题材映射。",
        "核心用户及订单": "客户覆盖MEMS器件及晶圆代工需求方，公开资料未完整实名披露。",
        "核心竞争力": "晶圆制造工艺积累和MEMS平台能力是护城河，平台属性强于单一产品公司。",
        "备注": "更偏半导体平台股，节奏和纯光模块链不同。",
    },
    "688195.SH": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，交易关注点集中在CPO、光引擎与高速光学元件放量。",
        "核心逻辑分析": "腾景科技处在高速光学元件链条里，受益于AI数据中心升级带来的CPO和高速光模块需求扩张。",
        "核心用户及订单": "客户分布于光通信、激光和高端制造链，公开资料未完整实名披露。",
        "核心竞争力": "精密光学制造和高端光学元件量产能力是核心壁垒。",
        "备注": "弹性偏光学零部件，不完全等同于整机光模块。",
    },
    "002730.SZ": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，市场更多交易其电源设备、矿用防爆及算力配套映射。",
        "核心逻辑分析": "电光科技更偏设备与配套映射逻辑，行情弹性主要来自主题扩散而非确定性业绩主升。",
        "核心用户及订单": "客户分布于煤矿装备及工业电气领域，公开资料未完整实名披露。",
        "核心竞争力": "工业电气与矿用防爆设备基础较扎实，题材扩散阶段具备辨识度。",
        "备注": "偏题材弹性位，需更重视情绪周期。",
    },
    "300570.SZ": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪重点在高速光模块、数据中心互联及海外需求。",
        "核心逻辑分析": "太辰光是高速光器件与连接方案受益标的，AI算力拉动下的数据中心互联升级会提升其产品需求。",
        "核心用户及订单": "客户主要分布于海外与国内光通信链条，公开口径未完整实名披露。",
        "核心竞争力": "高端光器件与连接产品能力较强，受益于海外数据中心景气。",
        "备注": "更偏海外映射和高景气细分环节。",
    },
    "688008.SH": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，核心跟踪点是DDR5、内存接口芯片和服务器平台迭代。",
        "核心逻辑分析": "澜起科技是服务器芯片与内存接口芯片龙头，业绩逻辑更偏基本面兑现，不是纯题材股。",
        "核心用户及订单": "客户覆盖服务器和云计算产业链核心厂商，公开资料未完整实名披露全部客户。",
        "核心竞争力": "芯片设计能力、产品代际领先和服务器生态卡位是核心护城河。",
        "备注": "属于机构重仓型半导体龙头。",
    },
    "688307.SH": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪重点在机器视觉和精密光学业务扩张。",
        "核心逻辑分析": "中润光学的逻辑在于高端光学镜头和机器视觉，属于AI视觉和工业检测链的细分卡位公司。",
        "核心用户及订单": "客户主要在机器视觉、安防和工业光学场景，公开资料未完整实名披露。",
        "核心竞争力": "精密光学设计与制造能力较强，细分领域壁垒高于普通消费镜头。",
        "备注": "偏细分成长股，流动性和波动都更高。",
    },
    "301205.SZ": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，市场聚焦高速光模块、800G/1.6T及CPO映射。",
        "核心逻辑分析": "联特科技是高弹性的高速光模块链标的，受益于AI算力对高速互联的持续拉动。",
        "核心用户及订单": "客户主要分布于光通信和数据中心链，公开资料未完整实名披露。",
        "核心竞争力": "高速率光模块产品迭代快，景气周期中盈利弹性大。",
        "备注": "属于高弹性主线票，波动也更大。",
    },
    "603929.SH": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪重点在半导体洁净厂房和高端制造厂务工程订单。",
        "核心逻辑分析": "亚翔集成本质是半导体洁净工程受益股，赚的是晶圆厂和高端制造扩产的钱。",
        "核心用户及订单": "客户以半导体与高端制造项目方为主，订单偏工程项目型。",
        "核心竞争力": "洁净工程交付经验和半导体厂务工程能力是核心壁垒。",
        "备注": "更偏工程订单兑现，不属于纯科技题材票。",
    },
    "600072.SH": {
        "2026新订单/新增项目": "未单独披露2026新签订单金额，跟踪点在船舶装备、新能源工程和资产整合节奏。",
        "核心逻辑分析": "中船科技更偏中字头装备和新能源工程逻辑，行情驱动常来自资产整合与订单预期。",
        "核心用户及订单": "客户主要分布于船舶工业和大型工程项目，订单偏大项目型。",
        "核心竞争力": "央企平台背景和工程装备能力是主要护城河。",
        "备注": "风格上更偏中字头和工程装备。",
    },
}


def normalize_code(code: str) -> str:
    if code.endswith((".SZ", ".SH")):
        return code
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def load_research_rows() -> dict[str, dict[str, str]]:
    if not DEEP_DIVE_CSV.exists():
        return {}
    rows = list(csv.DictReader(DEEP_DIVE_CSV.open(encoding="utf-8-sig")))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        code = normalize_code(row["代码"])
        out[code] = row
    return out


@lru_cache(maxsize=1)
def load_q1_financial_rows() -> dict[str, dict[str, str]]:
    q1_path = OUT_DIR / f"watchlist_q1_financials_{AS_OF.isoformat()}.json"
    if not q1_path.exists():
        candidates = sorted(OUT_DIR.glob("watchlist_q1_financials_*.json"))
        if not candidates:
            return {}
        q1_path = candidates[-1]
    rows = json.loads(q1_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        code = normalize_code(str(row.get("code", "")))
        if not code:
            continue
        out[code] = {
            "q1Revenue2026": str(row.get("revenue_text") or ""),
            "q1RevenueYoY2026": str(row.get("revenue_yoy_text") or ""),
            "q1NetProfit2026": str(row.get("net_profit_text") or ""),
            "q1NetProfitYoY2026": str(row.get("net_profit_yoy_text") or ""),
        }
    return out


def build_watchlist(research_map: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    merged = list(WATCHLIST_BASE)
    seen = {code for _, code in merged}
    for row in research_map.values():
        code = normalize_code(row["代码"])
        if code in seen:
            continue
        merged.append((row["股票"], code))
        seen.add(code)
    return merged


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = TRUST_ENV
    return session


class CurlFallbackResponse:
    def __init__(self, url: str, status_code: int, content: bytes):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: {self.url}",
                response=None,
            )


def curl_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    json_payload: dict | list | None = None,
    timeout: int = 20,
) -> CurlFallbackResponse:
    final_url = url
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        joiner = "&" if "?" in final_url else "?"
        final_url = f"{final_url}{joiner}{query}"

    cmd = [
        "curl",
        "-sS",
        "-L",
        "-X",
        method.upper(),
        "--connect-timeout",
        str(min(timeout, 10)),
        "--max-time",
        str(timeout),
        final_url,
        "-w",
        r"\n__CURL_STATUS__:%{http_code}",
    ]
    for key, value in (headers or {}).items():
        cmd.extend(["-H", f"{key}: {value}"])
    if json_payload is not None:
        cmd.extend(["--data-binary", json.dumps(json_payload, ensure_ascii=False)])

    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl request failed for {final_url}: {stderr or result.returncode}")

    marker = b"\n__CURL_STATUS__:"
    if marker not in result.stdout:
        raise RuntimeError(f"curl response missing status marker for {final_url}")
    body, status_raw = result.stdout.rsplit(marker, 1)
    status_code = int(status_raw.decode("utf-8", errors="replace").strip())
    response = CurlFallbackResponse(final_url, status_code, body)
    response.raise_for_status()
    return response


def request_with_fallback(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    json_payload: dict | list | None = None,
    timeout: int = 20,
):
    active_session = session or build_session()
    try:
        response = active_session.request(
            method.upper(),
            url,
            params=params,
            headers=headers,
            json=json_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException:
        return curl_request(
            method,
            url,
            params=params,
            headers=headers,
            json_payload=json_payload,
            timeout=timeout,
        )


def get_refresh_token() -> str:
    return (
        os.environ.get("ASTOCK_IFIND_REFRESH_TOKEN", "").strip()
        or os.environ.get("IFIND_REFRESH_TOKEN", "").strip()
        or REFRESH_TOKEN
    )


def decode_refresh_token_metadata(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = json.loads(base64.b64decode(parts[1]).decode("utf-8"))
    except Exception:
        return {}
    user = payload.get("user") or {}
    return {
        "uid": payload.get("uid"),
        "userId": user.get("userId"),
        "refreshTokenExpiredTime": user.get("refreshTokenExpiredTime"),
    }


def assert_refresh_token_not_expired(token: str) -> None:
    meta = decode_refresh_token_metadata(token)
    expiry = meta.get("refreshTokenExpiredTime")
    if not expiry:
        return
    try:
        expiry_dt = datetime.strptime(str(expiry), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
    except ValueError:
        return
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    if expiry_dt <= now_dt:
        raise RuntimeError(
            "iFinD refresh token expired at "
            f"{expiry_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            "Set ASTOCK_IFIND_REFRESH_TOKEN (or IFIND_REFRESH_TOKEN) to a fresh token."
        )


def get_access_token() -> str:
    token = get_refresh_token()
    assert_refresh_token_not_expired(token)
    session = build_session()
    try:
        resp = request_with_fallback(
            "POST",
            f"{BASE_URL}/get_access_token",
            session=session,
            headers={"Content-Type": "application/json", "refresh_token": token},
            timeout=20,
        )
    except Exception as exc:
        raise RuntimeError(
            "Failed to reach iFinD token endpoint "
            f"{BASE_URL}/get_access_token. "
            f"trust_env={session.trust_env}. Check DNS/network/proxy settings."
        ) from exc
    payload = resp.json()
    return payload["data"]["access_token"]


def batched(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def fetch_history(access_token: str, codes: list[str] | None = None) -> dict[str, dict]:
    if codes is None:
        codes = [code for _, code in build_watchlist(load_research_rows())]
    session = build_session()
    out: dict[str, dict] = {}

    def pull(batch_codes: list[str]) -> None:
        if not batch_codes:
            return
        payload = {
            "codes": ",".join(batch_codes),
            "indicators": "open,high,low,close,volume,amount",
            "startdate": (AS_OF - timedelta(days=45)).strftime("%Y-%m-%d"),
            "enddate": AS_OF.strftime("%Y-%m-%d"),
            "functionpara": {"Fill": "Blank"},
        }
        try:
            resp = request_with_fallback(
                "POST",
                f"{BASE_URL}/cmd_history_quotation",
                session=session,
                json_payload=payload,
                headers={"Content-Type": "application/json", "access_token": access_token},
                timeout=30,
            )
            out.update({row["thscode"]: row for row in resp.json()["tables"]})
        except Exception:
            if len(batch_codes) == 1:
                return
            mid = len(batch_codes) // 2
            pull(batch_codes[:mid])
            pull(batch_codes[mid:])

    for batch_codes in batched(codes, 40):
        pull(batch_codes)

    missing = [code for code in codes if code not in out]
    if missing:
        for batch_codes in batched(missing, 8):
            pull(batch_codes)

    missing = [code for code in codes if code not in out]
    if missing:
        for code in missing:
            pull([code])
    return out


def fetch_basic(access_token: str, codes: list[str] | None = None) -> dict[str, dict]:
    if codes is None:
        codes = [code for _, code in build_watchlist(load_research_rows())]
    session = build_session()
    out: dict[str, dict] = {}

    def pull(batch_codes: list[str]) -> None:
        if not batch_codes:
            return
        payload = {
            "codes": ",".join(batch_codes),
            "indipara": [
                {"indicator": "ths_total_shares_stock", "indiparams": [AS_OF.strftime("%Y%m%d")]},
                {"indicator": "ths_free_float_shares_stock", "indiparams": [AS_OF.strftime("%Y%m%d")]},
                {"indicator": "ths_close_price_stock", "indiparams": [AS_OF.strftime("%Y%m%d"), "", ""]},
            ],
        }
        try:
            resp = request_with_fallback(
                "POST",
                f"{BASE_URL}/basic_data_service",
                session=session,
                json_payload=payload,
                headers={"Content-Type": "application/json", "access_token": access_token},
                timeout=30,
            )
            out.update({row["thscode"]: row["table"] for row in resp.json()["tables"]})
        except Exception:
            if len(batch_codes) == 1:
                return
            mid = len(batch_codes) // 2
            pull(batch_codes[:mid])
            pull(batch_codes[mid:])

    for batch_codes in batched(codes, 60):
        pull(batch_codes)
    return out


def fetch_realtime_map(access_token: str, codes: list[str], trade_date: date | None = None) -> dict[str, dict]:
    if not codes:
        return {}
    if trade_date is None:
        trade_date = AS_OF
    session = build_session()
    out: dict[str, dict] = {}
    for batch_codes in batched(codes, 80):
        payload = {
            "codes": ",".join(batch_codes),
            "indicators": "latest,volume,amount,changeRatio",
            "starttime": f"{trade_date.isoformat()} 14:55:00",
            "endtime": f"{trade_date.isoformat()} 15:00:00",
        }
        resp = request_with_fallback(
            "POST",
            f"{BASE_URL}/real_time_quotation",
            session=session,
            json_payload=payload,
            headers={"Content-Type": "application/json", "access_token": access_token},
            timeout=40,
        )
        for item in resp.json().get("tables", []):
            out[item["thscode"]] = item.get("table") or {}
    return out


def fetch_order_book_snapshots(access_token: str, codes: list[str]) -> dict[str, dict]:
    indicators = (
        "latest,"
        "bid1,bid2,bid3,bid4,bid5,"
        "ask1,ask2,ask3,ask4,ask5,"
        "bidSize1,bidSize2,bidSize3,bidSize4,bidSize5,"
        "askSize1,askSize2,askSize3,askSize4,askSize5"
    )
    payload = {
        "codes": ",".join(codes),
        "indicators": indicators,
        "starttime": f"{AS_OF.isoformat()} 14:55:00",
        "endtime": f"{AS_OF.isoformat()} 15:00:00",
    }
    session = build_session()
    resp = request_with_fallback(
        "POST",
        f"{BASE_URL}/snap_shot",
        session=session,
        json_payload=payload,
        headers={"Content-Type": "application/json", "access_token": access_token},
        timeout=30,
    )
    snapshots: dict[str, dict] = {}
    for row in resp.json().get("tables", []):
        times = row.get("time") or []
        table = row.get("table") or {}
        if not times:
            continue
        last_idx = len(times) - 1
        snapshots[row["thscode"]] = {
            "time": times[last_idx],
            "latest": (table.get("latest") or [None])[last_idx] if table.get("latest") else None,
            "bids": [
                {
                    "level": level,
                    "price": (table.get(f"bid{level}") or [None])[last_idx] if table.get(f"bid{level}") else None,
                    "size": (table.get(f"bidSize{level}") or [None])[last_idx] if table.get(f"bidSize{level}") else None,
                }
                for level in range(1, 6)
            ],
            "asks": [
                {
                    "level": level,
                    "price": (table.get(f"ask{level}") or [None])[last_idx] if table.get(f"ask{level}") else None,
                    "size": (table.get(f"askSize{level}") or [None])[last_idx] if table.get(f"askSize{level}") else None,
                }
                for level in range(1, 6)
            ],
        }
    return snapshots


def fmt_yi(value: float) -> str:
    return f"{value / 1e8:,.2f}亿"


def fmt_wan_shou(volume: float) -> str:
    return f"{volume / 1e4:,.2f}万手"


def fmt_yi_rmb(value: float) -> str:
    return f"{value / 1e8:,.2f}亿"


def fetch_industry(code: str) -> str:
    secid = ("1." if code.split(".")[0].startswith(("5", "6", "9")) else "0.") + code.split(".")[0]
    try:
        session = build_session()
        resp = request_with_fallback(
            "GET",
            "https://push2.eastmoney.com/api/qt/stock/get",
            session=session,
            params={"invt": "2", "fltt": "2", "fields": "f100", "secid": secid},
            timeout=15,
        )
        return str((resp.json().get("data") or {}).get("f100") or "")
    except Exception:
        return ""


def fetch_total_market_cap(code: str) -> float:
    secid = ("1." if code.split(".")[0].startswith(("5", "6", "9")) else "0.") + code.split(".")[0]
    try:
        session = build_session()
        resp = request_with_fallback(
            "GET",
            "https://push2.eastmoney.com/api/qt/stock/get",
            session=session,
            params={"invt": "2", "fltt": "2", "fields": "f116", "secid": secid},
            timeout=15,
        )
        return float((resp.json().get("data") or {}).get("f116") or 0)
    except Exception:
        return 0.0


def fetch_pe_ratios(access_token: str, codes: list[str]) -> dict[str, float | None]:
    payload = {
        "codes": ",".join(codes),
        "indipara": [
            {"indicator": "ths_pe_ttm_stock", "indiparams": [AS_OF.strftime("%Y%m%d")]},
        ],
    }
    session = build_session()
    resp = request_with_fallback(
        "POST",
        f"{BASE_URL}/basic_data_service",
        session=session,
        json_payload=payload,
        headers={"Content-Type": "application/json", "access_token": access_token},
        timeout=30,
    )
    out: dict[str, float | None] = {}
    for row in resp.json().get("tables", []):
        values = row.get("table", {}).get("ths_pe_ttm_stock") or [None]
        value = values[0]
        out[row["thscode"]] = None if value in (None, "", "-") else float(value)
    return out


def backfill_pe_ratios(access_token: str, rows: list[dict]) -> list[dict]:
    codes = [row["code"] for row in rows if row.get("code") and row.get("peRatio") is None]
    if not codes:
        return rows
    pe_map = fetch_pe_ratios(access_token, codes)
    for row in rows:
        value = pe_map.get(row.get("code"))
        if value is not None:
            row["peRatio"] = value
    return rows


def fetch_margin_financing_map(codes: list[str]) -> dict[str, dict[str, float | str | None]]:
    out: dict[str, dict[str, float | str | None]] = {}
    for code in codes:
        raw_code, market = code.split(".")
        try:
            payload = fetch_json(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                {
                    "reportName": "RPTA_WEB_RZRQ_GGMX",
                    "columns": "DATE,SCODE,SECNAME,RZYE,RQYE,RZMRE,SECUCODE",
                    "source": "WEB",
                    "sortColumns": "DATE",
                    "sortTypes": "-1",
                    "pageNumber": "1",
                    "pageSize": "1",
                    "filter": f"(SECUCODE=\"{raw_code}.{market}\")(DATE<='{AS_OF.isoformat()}')",
                },
                timeout=20,
            )
            rows = (payload.get("result") or {}).get("data") or []
            if not rows:
                raise ValueError("empty margin financing rows")
            row = rows[0]
            out[code] = {
                "date": str(row.get("DATE") or "").split(" ")[0],
                "finBalance": to_float(row.get("RZYE")),
                "loanBalance": to_float(row.get("RQYE")),
                "finBuyAmount": to_float(row.get("RZMRE")),
            }
        except Exception:
            out[code] = {
                "date": "",
                "finBalance": None,
                "loanBalance": None,
                "finBuyAmount": None,
            }
    return out


def to_float(value) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def code_to_secid(code: str) -> str:
    raw = code.split(".")[0]
    if raw.startswith(("5", "6", "9")):
        return f"1.{raw}"
    return f"0.{raw}"


def fetch_json(url: str, params: dict, timeout: int = 15, retries: int = 2) -> dict:
    last_error = None
    for attempt in range(retries):
        try:
            session = build_session()
            resp = request_with_fallback("GET", url, session=session, params=params, timeout=timeout)
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                continue
    raise RuntimeError(f"request failed: {url} {params} err={last_error}")


def fetch_public_kline(code: str, as_of: date) -> list[list[float | str]]:
    start = (as_of - timedelta(days=75)).strftime("%Y%m%d")
    try:
        payload = fetch_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": code_to_secid(code),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": "101",
                "fqt": "1",
                "beg": start,
                "end": as_of.strftime("%Y%m%d"),
            },
            timeout=5,
            retries=1,
        )
        data = ((payload.get("data") or {}).get("klines") or [])
        rows: list[list[float | str]] = []
        for row in data:
            parts = str(row).split(",")
            if len(parts) < 7:
                continue
            rows.append(
                [
                    str(parts[0]),
                    float(parts[1]),
                    float(parts[2]),
                    float(parts[4]),
                    float(parts[3]),
                    float(parts[5]) * 100.0,
                    float(parts[6]),
                ]
            )
        if rows:
            return rows
    except Exception:
        pass

    market_prefix = "sh" if code.split(".")[0].startswith(("5", "6", "9")) else "sz"
    symbol = f"{market_prefix}{code.split('.')[0]}"
    session = build_session()
    resp = request_with_fallback(
        "GET",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        session=session,
        params={"param": f"{symbol},day,{(as_of - timedelta(days=75)).isoformat()},{as_of.isoformat()},640,qfq"},
        timeout=20,
    )
    payload = resp.json()
    data = (((payload.get("data") or {}).get(symbol) or {}).get("qfqday") or [])
    rows: list[list[float | str]] = []
    for row in data:
        if len(row) < 6:
            continue
        rows.append(
            [
                str(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[4]),
                float(row[3]),
                float(row[5]) * 100.0,
                0.0,
            ]
        )
    return rows


def update_row_from_public_kline(row: dict, as_of: date) -> dict:
    code = row["code"]
    prev_close = float(row.get("latestClose") or 0.0)
    prev_total_cap = float(row.get("totalMarketCap") or 0.0)
    prev_float_cap = float(row.get("floatMarketCap") or 0.0)
    total_shares = (prev_total_cap / prev_close) if prev_close else 0.0
    float_shares = (prev_float_cap / prev_close) if prev_close else 0.0
    snapshot = fetch_quote_snapshot(code)
    public_kline = fetch_public_kline(code, as_of)
    old_amount_by_date = {
        k[0]: k[6]
        for k in row.get("kline", [])
        if isinstance(k, list) and len(k) >= 7
    }
    kline = []
    for item in public_kline[-30:]:
        trade_date, open_px, close_px, low_px, high_px, volume, amount = item
        amount = snapshot.get("todayAmount") if trade_date == as_of.isoformat() and snapshot.get("todayAmount") else amount
        amount = old_amount_by_date.get(trade_date, amount) if not amount else amount
        kline.append([trade_date, open_px, close_px, low_px, high_px, volume, amount])

    if kline and kline[-1][0] < as_of.isoformat() and snapshot.get("latestClose"):
        prev_close_for_bar = float(kline[-1][2])
        latest_close_for_bar = float(snapshot["latestClose"])
        kline.append(
            [
                as_of.isoformat(),
                prev_close_for_bar,
                latest_close_for_bar,
                min(prev_close_for_bar, latest_close_for_bar),
                max(prev_close_for_bar, latest_close_for_bar),
                float(snapshot.get("todayVolume") or 0.0),
                float(snapshot.get("todayAmount") or old_amount_by_date.get(as_of.isoformat(), 0.0) or 0.0),
            ]
        )
        kline = kline[-30:]

    if kline:
        row["kline"] = kline
        row["latestClose"] = float(kline[-1][2])
        row["todayVolume"] = float(kline[-1][5])
        row["todayAmount"] = float(kline[-1][6]) or 0.0
        ref_close = float(kline[-2][2]) if len(kline) >= 2 else None
        row["todayPct"] = ((row["latestClose"] / ref_close - 1.0) * 100.0) if ref_close else row.get("todayPct")
        last5 = []
        prev = None
        for item in kline[-5:]:
            pct = None if prev is None else (float(item[2]) / prev - 1.0) * 100.0
            last5.append(
                {
                    "date": item[0],
                    "close": float(item[2]),
                    "volume": float(item[5]),
                    "amount": float(item[6]),
                    "pct": pct,
                }
            )
            prev = float(item[2])
        row["last5"] = last5

    if snapshot.get("latestClose"):
        row["latestClose"] = float(snapshot["latestClose"])
    if snapshot.get("todayPct") is not None:
        row["todayPct"] = float(snapshot["todayPct"])
    if snapshot.get("todayVolume"):
        row["todayVolume"] = float(snapshot["todayVolume"])
    if snapshot.get("todayAmount"):
        row["todayAmount"] = float(snapshot["todayAmount"])
    if snapshot.get("turnoverRate") is not None:
        row["turnoverRate"] = float(snapshot["turnoverRate"])
    if snapshot.get("totalMarketCap"):
        row["totalMarketCap"] = float(snapshot["totalMarketCap"])
    if snapshot.get("floatMarketCap"):
        row["floatMarketCap"] = float(snapshot["floatMarketCap"])
    if total_shares and row.get("latestClose"):
        row["totalMarketCap"] = total_shares * float(row["latestClose"])
    if float_shares and row.get("latestClose"):
        row["floatMarketCap"] = float_shares * float(row["latestClose"])
    return row


def fetch_top5_shareholders(code: str) -> dict[str, str | float | list[str]]:
    try:
        base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        date_payload = fetch_json(
            base,
            {
                "reportName": "RPT_F10_EH_HOLDERSDATE",
                "columns": "END_DATE,REPORT_DATE_NAME",
                "sortColumns": "END_DATE",
                "sortTypes": "-1",
                "pageSize": "1",
                "pageNumber": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": f'(SECURITY_CODE="{code.split(".")[0]}")',
            },
            timeout=15,
        )
        date_rows = (date_payload.get("result") or {}).get("data") or []
        if not date_rows:
            return {"reportDate": "", "totalRatio": None, "holders": []}
        end_date = date_rows[0].get("END_DATE") or ""
        report_date = str(end_date)[:10]

        holder_payload = fetch_json(
            base,
            {
                "reportName": "RPT_DMSK_HOLDERS",
                "columns": "ALL",
                "sortColumns": "RANK",
                "sortTypes": "1",
                "pageSize": "5",
                "pageNumber": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": f'(SECURITY_CODE="{code.split(".")[0]}")(END_DATE=\'{report_date}\')(LISTING_STATE<>"10")',
            },
            timeout=15,
        )
        rows = (holder_payload.get("result") or {}).get("data") or []
        holders: list[str] = []
        total_ratio = 0.0
        has_ratio = False
        for idx, row in enumerate(rows[:5], start=1):
            name = str(row.get("HOLDER_NAME") or "").strip()
            ratio = row.get("HOLD_RATIO")
            if not name:
                continue
            ratio_text = ""
            if ratio not in (None, "", "-"):
                try:
                    ratio_value = float(ratio)
                    total_ratio += ratio_value
                    has_ratio = True
                    ratio_text = f" ({ratio_value:.2f}%)"
                except Exception:
                    ratio_text = f" ({ratio}%)"
            holders.append(f"{idx}. {name}{ratio_text}")
        return {
            "reportDate": report_date,
            "totalRatio": round(total_ratio, 2) if has_ratio else None,
            "holders": holders,
        }
    except Exception:
        return {"reportDate": "", "totalRatio": None, "holders": []}


def fetch_top3_business_segments(code: str) -> dict[str, str | list[dict[str, float | str | None]]]:
    try:
        raw, market = code.split(".")
        symbol = f"{market.upper()}{raw}"
        resp = request_with_fallback(
            "GET",
            "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax",
            params={"code": symbol},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": f"https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/Index?type=web&code={symbol}",
            },
            timeout=20,
        )
        payload = resp.json()
        rows = payload.get("zygcfx") or []
        if not rows:
            return {"reportDate": "", "category": "", "items": []}

        latest_report = ""
        for row in rows:
            report_date = str(row.get("REPORT_DATE") or "")[:10]
            if report_date and report_date > latest_report:
                latest_report = report_date
        if not latest_report:
            return {"reportDate": "", "category": "", "items": []}

        latest_rows = [row for row in rows if str(row.get("REPORT_DATE") or "")[:10] == latest_report]
        category_priority = [("2", "按产品分类"), ("1", "按行业分类"), ("3", "按地区分类")]
        chosen_rows: list[dict] = []
        chosen_category = ""
        for category_code, category_name in category_priority:
            bucket = [row for row in latest_rows if str(row.get("MAINOP_TYPE") or "") == category_code]
            if bucket:
                chosen_rows = bucket
                chosen_category = category_name
                break
        if not chosen_rows:
            return {"reportDate": latest_report, "category": "", "items": []}

        chosen_rows.sort(key=lambda row: to_float(row.get("MAIN_BUSINESS_INCOME")) or 0.0, reverse=True)
        items: list[dict[str, float | str | None]] = []
        for row in chosen_rows[:3]:
            items.append(
                {
                    "name": str(row.get("ITEM_NAME") or "").strip(),
                    "revenue": to_float(row.get("MAIN_BUSINESS_INCOME")),
                    "ratio": to_float(row.get("MBI_RATIO")),
                }
            )
        return {"reportDate": latest_report, "category": chosen_category, "items": items}
    except Exception:
        return {"reportDate": "", "category": "", "items": []}


def normalize_main_business_value(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return ""
    return text


def infer_main_business_from_segments(payload: dict[str, object] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    items = payload.get("items") or []
    if not isinstance(items, list):
        return ""
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in {"其他", "其他业务", "其他产品", "其他地区"}:
            continue
        if name not in labels:
            labels.append(name)
        if len(labels) >= 2:
            break
    return "/".join(labels)


def resolve_main_business(
    code: str,
    *,
    industry: str | None = None,
    existing: str | None = None,
    business_segments: dict[str, object] | None = None,
    news_text: str | None = None,
) -> str:
    mapped = normalize_main_business_value(MAIN_BUSINESS_MAP.get(code))
    if mapped:
        return mapped
    existing_value = normalize_main_business_value(existing)
    if existing_value:
        return existing_value
    segment_hint = infer_main_business_from_segments(business_segments)
    if segment_hint:
        return segment_hint
    industry_value = normalize_main_business_value(industry)
    if industry_value:
        return industry_value
    news_hint = infer_main_business_from_text(news_text or "")
    if news_hint:
        return news_hint
    return "-"


def clean_news_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip().replace("｜", "-").replace("|", "-").replace("_", "-")


def summarize_news_title(title: str, name: str) -> str:
    cleaned = clean_news_title(title)
    cleaned = re.sub(rf"^{re.escape(name)}[（(]?\d{{6}}[)）]?", "", cleaned).strip(" -:")
    cleaned = re.sub(r"\s*-\s*(证券之星|中金在线|Sohu|新浪财经|东方财富|中财网|财联社|同花顺财经|界面新闻|第一财经).*$", "", cleaned)
    return cleaned or clean_news_title(title)


def parse_news_pub_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(AS_OF_DT.tzinfo)
    except Exception:
        return None


def score_news_title(title: str, pub_dt: datetime | None) -> int:
    score = 0
    for keyword, value in POSITIVE_NEWS_KEYWORDS.items():
        if keyword in title:
            score += value
    for keyword, value in NEGATIVE_NEWS_KEYWORDS.items():
        if keyword in title:
            score += value
    if pub_dt is not None:
        age_hours = max((AS_OF_DT - pub_dt).total_seconds() / 3600.0, 0)
        if age_hours <= 24:
            score += 40
        elif age_hours <= 48:
            score += 26
        elif age_hours <= 72:
            score += 14
        else:
            score -= 50
    return score


def infer_main_business_from_text(text: str) -> str:
    content = (text or "").upper()
    for keyword, label in BUSINESS_KEYWORD_MAP:
        if keyword.upper() in content:
            return label
    return ""


def has_valid_recent_news(news: dict[str, str] | None) -> bool:
    if not news:
        return False
    summary = (news.get("summary") or "").strip()
    if not summary:
        return False
    if "未筛到高质量公告或媒体新闻" in summary:
        return False
    recent_flag = news.get("isRecent")
    if recent_flag is not None:
        return bool(recent_flag)
    return True


def matches_mainline_business(value: str | None) -> bool:
    text = (value or "").strip()
    if not text or text == "-":
        return False
    return any(keyword in text for keyword in MAINLINE_KEYWORDS)


def strong_stock_passes_final_filters(row: dict) -> bool:
    pct = row.get("todayPct")
    total_cap = row.get("totalMarketCap") or 0
    amount = row.get("todayAmount") or 0
    turnover = row.get("turnoverRate")
    if pct is None or pct <= STRONG_MIN_PCT:
        return False
    if total_cap < STRONG_MIN_TOTAL_CAP or total_cap > STRONG_MAX_TOTAL_CAP:
        return False
    if amount < STRONG_MIN_AMOUNT:
        return False
    if turnover is None or turnover <= STRONG_MIN_TURNOVER or turnover > STRONG_MAX_TURNOVER:
        return False
    return True


def score_strong_stock(row: dict, watch_codes: set[str] | None = None) -> float:
    score = 0.0
    if matches_mainline_business(row.get("mainBusiness")):
        score += 40.0
        text = row.get("mainBusiness") or ""
        for keyword in ("CPO", "光模块", "存储", "PCB", "算力", "连接器", "液冷"):
            if keyword in text:
                score += 6.0
    news = row.get("latestNews") or {}
    if has_valid_recent_news(news):
        score += 28.0
        if news.get("isRecent"):
            score += 12.0
    amount = float(row.get("todayAmount") or 0.0)
    score += min(amount / 1e8, 120.0) * 0.45
    pct = float(row.get("todayPct") or 0.0)
    score += min(max(pct, 0.0), 20.0) * 2.8
    turnover = row.get("turnoverRate")
    if turnover is not None:
        turnover = float(turnover)
        if 8.0 <= turnover <= 18.0:
            score += 18.0
        elif 6.0 <= turnover < 8.0 or 18.0 < turnover <= 22.0:
            score += 10.0
        elif 22.0 < turnover <= 25.0:
            score += 4.0
    total_cap = float(row.get("totalMarketCap") or 0.0)
    if 1.2e10 <= total_cap <= 8e10:
        score += 8.0
    elif 8e10 < total_cap <= 2e11:
        score += 4.0
    if watch_codes and row.get("code") in watch_codes:
        score += 8.0
    return round(score, 4)


def finalize_strong_stocks(rows: list[dict], watch_codes: set[str] | None = None) -> list[dict]:
    filtered = [row for row in rows if strong_stock_passes_final_filters(row)]
    for row in filtered:
        row["strongScore"] = score_strong_stock(row, watch_codes)
    filtered.sort(
        key=lambda row: (
            row.get("strongScore") or 0,
            row.get("todayAmount") or 0,
            row.get("todayPct") or 0,
        ),
        reverse=True,
    )
    return filtered[:STRONG_DISPLAY_LIMIT]


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_eastmoney_notice_news(session: requests.Session, name: str, code: str) -> dict[str, str]:
    payload = {
        "uid": "",
        "keyword": name,
        "type": ["noticeWeb"],
        "client": "web",
        "clientVersion": "curr",
        "clientType": "web",
        "param": {
            "noticeWeb": {
                "preTag": '<em class="red">',
                "postTag": "</em>",
                "pageSize": 8,
                "pageIndex": 1,
            }
        },
    }
    try:
        url = "https://search-api-web.eastmoney.com/search/jsonp?" + urllib.parse.urlencode(
            {"cb": "cb", "param": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
        )
        completed = subprocess.run(
            ["curl", "-A", NEWS_UA, "-sS", url],
            capture_output=True,
            text=True,
            timeout=12,
            check=True,
        )
        match = re.search(r"^[^(]+\((.*)\)\s*$", completed.stdout, re.S)
        if not match:
            return {"time": "", "summary": "", "title": "", "link": "", "isRecent": False}
        data = json.loads(match.group(1))
        notices = ((data.get("result") or {}).get("noticeWeb") or [])
        candidates: list[tuple[int, datetime | None, str, str]] = []
        plain_code = code.split(".")[0]
        for item in notices:
            title = clean_news_title(strip_tags(item.get("title") or item.get("shortTitle") or ""))
            link = (item.get("url") or "").strip()
            security_name = strip_tags(item.get("securityFullName") or "")
            raw_date = (item.get("date") or "").strip()
            pub = None
            if raw_date:
                try:
                    pub = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
                except ValueError:
                    try:
                        pub = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=8)))
                    except ValueError:
                        pub = None
            if not title or not link:
                continue
            score = score_news_title(title, pub) + 10
            is_match = (
                security_name == name
                or plain_code in link
                or (name in title and (not security_name or security_name == name))
            )
            if is_match:
                candidates.append((score, pub, title, link))
        candidates.sort(key=lambda row: (row[0], row[1] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        best = candidates[0] if candidates else None
        if not best:
            return {"time": "", "summary": "", "title": "", "link": "", "isRecent": False}
        _, pub, title, link = best
        is_recent = bool(pub and pub >= AS_OF_DT - timedelta(days=NEWS_LOOKBACK_DAYS))
        return {
            "time": pub.strftime("%m-%d %H:%M") if pub else "",
            "summary": summarize_news_title(title, name),
            "title": title,
            "link": link,
            "isRecent": is_recent,
        }
    except Exception:
        return {"time": "", "summary": "", "title": "", "link": "", "isRecent": False}


def fetch_latest_news_map(stocks: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    session = build_session()
    session.headers.update({"User-Agent": NEWS_UA})
    for name, code in stocks:
        out[code] = fetch_eastmoney_notice_news(session, name, code)
    return out


def fetch_strong_stock_candidates(trade_date: date) -> list[dict]:
    page = 1
    rows: list[dict] = []
    trade_date_text = trade_date.isoformat()
    while True:
        payload = fetch_json(
            FUNDFLOW_API,
            {
                "type": "RPT_DMSK_TS_FUNDFLOWHIS",
                "sty": "ALL",
                "source": "SECURITIES",
                "client": "APP",
                "st": "TRADE_DATE",
                "sr": "-1",
                "p": str(page),
                "ps": "500",
                "filter": f"(TRADE_DATE>='{trade_date_text}')(TRADE_DATE<='{trade_date_text}')(CHANGE_RATE>{STRONG_MIN_PCT})",
            },
            timeout=20,
            retries=3,
        )
        result = payload.get("result") or {}
        data = result.get("data") or []
        for row in data:
            code = str(row.get("SECURITY_CODE") or "").strip()
            name = str(row.get("SECURITY_NAME_ABBR") or "").strip()
            pct = to_float(row.get("CHANGE_RATE"))
            if not code or not name or pct is None:
                continue
            rows.append({"code": normalize_code(code), "name": name, "todayPct": pct})
        pages = int(result.get("pages") or 0)
        if pages > 0 and page >= pages:
            break
        if not data:
            break
        page += 1
    return rows


def fetch_limit_up_candidates(trade_date: date) -> list[dict]:
    payload = fetch_json(
        LIMIT_UP_POOL_API,
        {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": "0",
            "pagesize": "500",
            "sort": "amount:desc",
            "date": trade_date.strftime("%Y%m%d"),
        },
        timeout=20,
        retries=3,
    )
    pool = ((payload.get("data") or {}).get("pool") or [])
    rows: list[dict] = []
    for item in pool:
        code = normalize_code(str(item.get("c") or "").strip())
        name = str(item.get("n") or "").strip()
        pct = to_float(item.get("zdp"))
        latest_close_raw = to_float(item.get("p"))
        latest_close = (latest_close_raw / 1000.0) if latest_close_raw is not None else None
        if not code or not name or pct is None:
            continue
        rows.append(
            {
                "code": code,
                "name": name,
                "industry": str(item.get("hybk") or "").strip(),
                "todayPct": pct,
                "todayAmount": to_float(item.get("amount")) or 0.0,
                "turnoverRate": to_float(item.get("hs")),
                "floatMarketCap": to_float(item.get("ltsz")) or 0.0,
                "totalMarketCap": to_float(item.get("tshare")) or 0.0,
                "latestClose": latest_close,
            }
        )
    return rows


def resolve_strong_stock_candidates(trade_date: date, lookback_days: int = 5) -> tuple[date, list[dict]]:
    for offset in range(lookback_days + 1):
        candidate_date = trade_date - timedelta(days=offset)
        rows = fetch_strong_stock_candidates(candidate_date)
        if rows:
            return candidate_date, rows
    return trade_date, []


def parse_strong_stock_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) < 5 or parts[0] in {"股票代码", "---"}:
            continue
        code = normalize_code(parts[0])
        name = parts[1]
        pct_text = parts[2].replace("%", "").strip()
        pct = to_float(pct_text)
        amount_match = re.search(r"([0-9]+(?:\.[0-9]+)?)亿", parts[4])
        amount = float(amount_match.group(1)) * 1e8 if amount_match else None
        if not code or not name or pct is None:
            continue
        rows.append({"code": code, "name": name, "todayPct": pct, "todayAmount": amount})
    return rows


def fetch_quote_snapshot(code: str) -> dict:
    data: dict[str, object] = {}
    try:
        session = build_session()
        resp = request_with_fallback(
            "GET",
            QUOTE_API,
            session=session,
            params={
                "invt": "2",
                "fltt": "2",
                "fields": "f57,f58,f2,f3,f5,f6,f116,f117,f168",
                "secid": code_to_secid(code),
            },
            timeout=15,
        )
        data = (resp.json().get("data") or {})
    except Exception:
        data = {}
    snapshot = {
        "latestClose": to_float(data.get("f2")),
        "todayPct": to_float(data.get("f3")),
        "todayVolume": (to_float(data.get("f5")) or 0.0) * 100.0,
        "totalMarketCap": to_float(data.get("f116")) or 0.0,
        "floatMarketCap": to_float(data.get("f117")) or 0.0,
        "turnoverRate": to_float(data.get("f168")),
        "todayAmount": to_float(data.get("f6")) or 0.0,
    }
    if snapshot["latestClose"] not in (None, 0):
        return snapshot

    try:
        raw = code.split(".")[0]
        symbol = ("sh" if raw.startswith(("5", "6", "9")) else "sz") + raw
        session = build_session()
        resp = request_with_fallback(
            "GET",
            "https://qt.gtimg.cn/q=" + symbol,
            session=session,
            timeout=15,
        )
        text = resp.text
        if "=" not in text:
            return snapshot
        payload = text.split("=", 1)[1].strip().strip(";").strip().strip('"')
        parts = payload.split("~")
        if len(parts) >= 46:
            snapshot["latestClose"] = to_float(parts[3])
            snapshot["todayPct"] = to_float(parts[32])
            snapshot["todayVolume"] = to_float(parts[36]) or 0.0
            snapshot["todayAmount"] = (to_float(parts[37]) or 0.0) * 10000.0
            snapshot["turnoverRate"] = to_float(parts[38])
            snapshot["totalMarketCap"] = (to_float(parts[44]) or 0.0) * 1e8
            snapshot["floatMarketCap"] = (to_float(parts[45]) or 0.0) * 1e8
    except Exception:
        pass
    return snapshot


def fetch_strong_stocks(trade_date: date) -> list[dict]:
    resolved_trade_date = trade_date
    if trade_date == AS_OF:
        candidates = parse_strong_stock_report(POST_CLOSE_REPORT)
        if not candidates:
            resolved_trade_date, candidates = resolve_strong_stock_candidates(trade_date)
    else:
        resolved_trade_date, candidates = resolve_strong_stock_candidates(trade_date)
    strong_rows: list[dict] = []
    for row in candidates:
        snap = fetch_quote_snapshot(row["code"])
        total_cap = snap["totalMarketCap"]
        if total_cap < STRONG_MIN_TOTAL_CAP or total_cap > STRONG_MAX_TOTAL_CAP:
            continue
        if (snap["todayAmount"] or row.get("todayAmount") or 0.0) < STRONG_MIN_AMOUNT:
            continue
        if snap["turnoverRate"] is None or snap["turnoverRate"] <= STRONG_MIN_TURNOVER or snap["turnoverRate"] > STRONG_MAX_TURNOVER:
            continue
        code = row["code"]
        industry = fetch_industry(code)
        strong_rows.append(
            {
                "code": code,
                "name": row["name"],
                "industry": industry,
                "mainBusiness": resolve_main_business(code, industry=industry),
                "latestClose": snap["latestClose"],
                "totalMarketCap": total_cap,
                "floatMarketCap": snap["floatMarketCap"],
                "todayAmount": snap["todayAmount"] or row.get("todayAmount") or 0.0,
                "turnoverRate": snap["turnoverRate"],
                "todayPct": row["todayPct"],
            }
        )
    if strong_rows:
        news_map = fetch_latest_news_map([(row["name"], row["code"]) for row in strong_rows])
        for row in strong_rows:
            row["latestNews"] = news_map.get(row["code"]) or row.get("latestNews") or {
                "time": "",
                "summary": "",
                "title": "",
                "link": "",
                "isRecent": False,
            }
            row["mainBusiness"] = resolve_main_business(
                row["code"],
                industry=row.get("industry"),
                existing=row.get("mainBusiness"),
                business_segments=row.get("businessSegments"),
                news_text=" ".join(
                    [
                        row["latestNews"].get("title", ""),
                        row["latestNews"].get("summary", ""),
                    ]
                ),
            )
    strong_rows = finalize_strong_stocks(strong_rows, {code for _, code in WATCHLIST_BASE})
    return strong_rows


def fetch_strong_stocks_via_ifind(access_token: str, trade_date: date) -> list[dict]:
    _, candidates = resolve_strong_stock_candidates(trade_date)
    if not candidates:
        return []
    codes = [row["code"] for row in candidates]
    code_to_name = {row["code"]: row["name"] for row in candidates}
    code_to_pct = {row["code"]: row["todayPct"] for row in candidates}
    history_map = fetch_history(access_token, codes)

    basic_map = fetch_basic(access_token, codes)
    try:
        pe_map = fetch_pe_ratios(access_token, codes)
    except Exception:
        pe_map = {}

    strong_rows: list[dict] = []
    for code in codes:
        history = history_map.get(code)
        table = (history or {}).get("table") or {}
        times = ((history or {}).get("time") or [])[-30:]
        open_list = (table.get("open") or [])[-30:]
        high_list = (table.get("high") or [])[-30:]
        low_list = (table.get("low") or [])[-30:]
        close_list = (table.get("close") or [])[-30:]
        volume_list = (table.get("volume") or [])[-30:]
        amount_list = (table.get("amount") or [])[-30:]

        basic = basic_map.get(code, {})
        close_field = basic.get("ths_close_price_stock") or []
        total_field = basic.get("ths_total_shares_stock") or []
        float_field = basic.get("ths_free_float_shares_stock") or []
        latest_close = float(
            (close_field[0] if close_field else None)
            or (close_list[-1] if close_list else None)
            or 0.0
        )
        total_shares = float((total_field[0] if total_field else 0) or 0)
        free_float_shares = float((float_field[0] if float_field else 0) or 0)
        total_cap = total_shares * latest_close if total_shares else 0.0
        float_cap = free_float_shares * latest_close if free_float_shares else 0.0
        if total_cap <= 0 and float_cap > 0:
            total_cap = float_cap
        today_amount = amount_list[-1] if amount_list else 0.0
        turnover_rate = (
            (volume_list[-1] / free_float_shares * 100.0) if volume_list and free_float_shares else None
        )

        if total_cap < STRONG_MIN_TOTAL_CAP or total_cap > STRONG_MAX_TOTAL_CAP:
            continue
        if today_amount < STRONG_MIN_AMOUNT:
            continue
        if turnover_rate is None or turnover_rate <= STRONG_MIN_TURNOVER or turnover_rate > STRONG_MAX_TURNOVER:
            continue

        last5 = []
        if times and close_list and len(times) == len(close_list):
            prev = None
            for i in range(max(0, len(times) - 5), len(times)):
                current_close = close_list[i]
                pct = None
                if current_close not in (None, "") and prev not in (None, ""):
                    pct = (current_close / prev - 1.0) * 100.0
                last5.append(
                    {
                        "date": times[i],
                        "close": current_close,
                        "volume": volume_list[i],
                        "amount": amount_list[i],
                        "pct": pct,
                    }
                )
                prev = current_close if current_close not in (None, "") else prev

        industry = fetch_industry(code)
        strong_rows.append(
            {
                "code": code,
                "name": code_to_name.get(code, code),
                "industry": industry,
                "mainBusiness": resolve_main_business(code, industry=industry),
                "latestClose": latest_close,
                "totalMarketCap": total_cap,
                "floatMarketCap": float_cap,
                "todayAmount": today_amount,
                "turnoverRate": turnover_rate,
                "todayPct": code_to_pct.get(code),
                "latestNews": {"time": "", "summary": "", "title": "", "link": "", "isRecent": False},
                "research": empty_research_payload(),
                "peRatio": pe_map.get(code),
                "marginFinancing": {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None},
                "topHolders": {"reportDate": "", "totalRatio": None, "holders": []},
                "businessSegments": {"reportDate": "", "category": "", "items": []},
                "topCustomers": {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []},
                "kline": [
                    [times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]]
                    for i in range(len(times))
                    if close_list[i] not in (None, "")
                ] if times and len(times) == len(close_list) else [],
                "last5": last5,
            }
        )

    if strong_rows:
        news_map = fetch_latest_news_map([(row["name"], row["code"]) for row in strong_rows])
        for row in strong_rows:
            row["latestNews"] = news_map.get(row["code"]) or {
                "time": "",
                "summary": "",
                "title": "",
                "link": "",
                "isRecent": False,
            }

    for row in strong_rows:
        row["mainBusiness"] = resolve_main_business(
            row["code"],
            industry=row.get("industry"),
            existing=row.get("mainBusiness"),
            business_segments=row.get("businessSegments"),
            news_text=" ".join(
                [
                    row["latestNews"].get("title", ""),
                    row["latestNews"].get("summary", ""),
                ]
            ),
        )

    strong_rows = finalize_strong_stocks(strong_rows, {code for _, code in WATCHLIST_BASE})
    return strong_rows


def fetch_top5_customers(name: str, code: str) -> dict[str, object]:
    headers = {
        "User-Agent": NEWS_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cninfo.com.cn/new/fulltextSearch",
    }
    annual_years = [AS_OF.year - 1, AS_OF.year - 2, AS_OF.year - 3]
    annual_report = None
    report_year = None

    for year in annual_years:
        params = {
            "searchkey": f"{name} {year} 年度报告",
            "pageNum": 1,
            "pageSize": 10,
            "sortName": "nothing",
            "sortType": "desc",
            "isfulltext": "true",
            "type": "",
        }
        try:
            resp = request_with_fallback(
                "GET",
                "https://www.cninfo.com.cn/new/fulltextSearch/full",
                params=params,
                headers=headers,
                timeout=20,
            )
            announcements = (resp.json() or {}).get("announcements") or []
        except Exception:
            continue

        for ann in announcements:
            raw_title = f"{ann.get('shortTitle') or ''} {ann.get('announcementTitle') or ''}"
            title = re.sub(r"<[^>]+>", "", raw_title)
            if (
                "年度报告" in title
                and "半年度" not in title
                and "摘要" not in title
                and "英文" not in title
                and "已取消" not in title
            ):
                annual_report = ann
                year_match = re.search(r"(20\d{2})年年度报告", title)
                report_year = int(year_match.group(1)) if year_match else year
                break
        if annual_report:
            break

    if not annual_report:
        return {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []}

    adjunct_url = annual_report.get("adjunctUrl") or ""
    if not adjunct_url:
        return {"reportDate": str(report_year or ""), "totalAmount": None, "totalRatio": None, "customers": []}

    try:
        pdf_resp = request_with_fallback(
            "GET",
            f"https://static.cninfo.com.cn/{adjunct_url}",
            headers={"User-Agent": NEWS_UA, "Referer": "https://www.cninfo.com.cn/"},
            timeout=40,
        )
        reader = PdfReader(io.BytesIO(pdf_resp.content))
    except Exception:
        return {"reportDate": str(report_year or ""), "totalAmount": None, "totalRatio": None, "customers": []}

    section_text = ""
    for page in reader.pages:
        text = (page.extract_text() or "").replace("\u3000", " ")
        if "前五名客户合计销售金额" not in text and "公司前 5 大客户资料" not in text and "公司前5大客户资料" not in text:
            continue
        section_text = text
        break

    if not section_text:
        return {"reportDate": str(report_year or ""), "totalAmount": None, "totalRatio": None, "customers": []}

    total_amount = None
    total_ratio = None
    amount_match = re.search(r"前五名客户合计销售金额（元）\s*([\d,]+(?:\.\d+)?)", section_text)
    if amount_match:
        total_amount = float(amount_match.group(1).replace(",", ""))
    ratio_match = re.search(r"前五名客户合计销售金额占年度销售总额比例\s*([\d.]+)%", section_text)
    if ratio_match:
        total_ratio = float(ratio_match.group(1))

    customers: list[dict[str, object]] = []
    in_customer_table = False
    for raw_line in section_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if "公司前 5 大客户资料" in line or "公司前5大客户资料" in line:
            in_customer_table = True
            continue
        if not in_customer_table:
            continue
        if line.startswith("合计") or "主要客户其他情况说明" in line or "公司主要供应商情况" in line:
            break
        row_match = re.match(r"^([1-5])\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+([\d.]+%)$", line)
        if not row_match:
            continue
        customers.append(
            {
                "rank": int(row_match.group(1)),
                "name": row_match.group(2),
                "amount": float(row_match.group(3).replace(",", "")),
                "ratio": float(row_match.group(4).rstrip("%")),
            }
        )

    return {
        "reportDate": str(report_year or ""),
        "totalAmount": total_amount,
        "totalRatio": total_ratio,
        "customers": customers,
    }


def format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:+.2f}%"


def format_revenue_yi(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value / 1e8:.2f}亿元"


def fetch_financial_snapshots(access_token: str, codes: list[str]) -> dict[str, dict[str, str]]:
    report_labels = {
        "20251231": "2025年报",
        "20250930": "2025三季报",
        "20250630": "2025中报",
        "20250331": "2025一季报",
    }
    snapshots: dict[str, dict[str, float | str | None]] = {code: {} for code in codes}
    q1_snapshots: dict[str, dict[str, float | None]] = {code: {} for code in codes}

    for report in ["20251231", "20250930", "20250630", "20250331"]:
        payload = {
            "codes": ",".join(codes),
            "indipara": [
                {"indicator": "ths_revenue_stock", "indiparams": [report, "1", "1"]},
                {"indicator": "ths_operating_revenue_yoy_stock", "indiparams": [report, "1", "1"]},
            ],
        }
        session = build_session()
        resp = request_with_fallback(
            "POST",
            f"{BASE_URL}/basic_data_service",
            session=session,
            json_payload=payload,
            headers={"Content-Type": "application/json", "access_token": access_token},
            timeout=60,
        )
        for row in resp.json()["tables"]:
            code = row["thscode"]
            table = row["table"]
            revenue = (table.get("ths_revenue_stock") or [None])[0]
            yoy = (table.get("ths_operating_revenue_yoy_stock") or [None])[0]
            if revenue is None and yoy is None:
                continue
            current = snapshots.setdefault(code, {})
            # Prefer the first available report in the sequence above, i.e. annual first then latest disclosed quarter.
            if current.get("report_date"):
                continue
            current["report_date"] = report
            current["report_label"] = report_labels[report]
            current["revenue"] = revenue
            current["yoy"] = yoy

    q1_payload = {
        "codes": ",".join(codes),
        "indipara": [
            {"indicator": "ths_revenue_stock", "indiparams": ["20260331", "1", "1"]},
            {"indicator": "ths_operating_revenue_yoy_stock", "indiparams": ["20260331", "1", "1"]},
            {"indicator": "ths_np_stock", "indiparams": ["20260331", "1", "1"]},
            {"indicator": "ths_np_yoy_stock", "indiparams": ["20260331", "1", "1"]},
        ],
    }
    session = build_session()
    resp = request_with_fallback(
        "POST",
        f"{BASE_URL}/basic_data_service",
        session=session,
        json_payload=q1_payload,
        headers={"Content-Type": "application/json", "access_token": access_token},
        timeout=60,
    )
    for row in resp.json()["tables"]:
        code = row["thscode"]
        table = row["table"]
        q1_snapshots[code] = {
            "q1Revenue2026": (table.get("ths_revenue_stock") or [None])[0],
            "q1RevenueYoY2026": (table.get("ths_operating_revenue_yoy_stock") or [None])[0],
            "q1NetProfit2026": (table.get("ths_np_stock") or [None])[0],
            "q1NetProfitYoY2026": (table.get("ths_np_yoy_stock") or [None])[0],
        }

    out: dict[str, dict[str, str]] = {}
    for code, snap in snapshots.items():
        report_label = snap.get("report_label")
        revenue = snap.get("revenue")
        yoy = snap.get("yoy")
        q1 = q1_snapshots.get(code, {})
        if not report_label:
            out[code] = {
                "q1Revenue2026": format_revenue_yi(q1.get("q1Revenue2026")),
                "q1RevenueYoY2026": format_pct(q1.get("q1RevenueYoY2026")),
                "q1NetProfit2026": format_revenue_yi(q1.get("q1NetProfit2026")),
                "q1NetProfitYoY2026": format_pct(q1.get("q1NetProfitYoY2026")),
            }
            continue
        if report_label == "2025年报":
            out[code] = {
                "revenue2025": f"iFinD口径：2025年营收{format_revenue_yi(revenue)}",
                "revenueYoY": format_pct(yoy),
                "q1Revenue2026": format_revenue_yi(q1.get("q1Revenue2026")),
                "q1RevenueYoY2026": format_pct(q1.get("q1RevenueYoY2026")),
                "q1NetProfit2026": format_revenue_yi(q1.get("q1NetProfit2026")),
                "q1NetProfitYoY2026": format_pct(q1.get("q1NetProfitYoY2026")),
            }
        else:
            yoy_text = format_pct(yoy) or "暂无同比"
            out[code] = {
                "revenue2025": f"iFinD截至{AS_OF.isoformat()}未检索到2025年报，最近一期已披露为{report_label}营收{format_revenue_yi(revenue)}",
                "revenueYoY": f"{report_label}同比{yoy_text}",
                "q1Revenue2026": format_revenue_yi(q1.get("q1Revenue2026")),
                "q1RevenueYoY2026": format_pct(q1.get("q1RevenueYoY2026")),
                "q1NetProfit2026": format_revenue_yi(q1.get("q1NetProfit2026")),
                "q1NetProfitYoY2026": format_pct(q1.get("q1NetProfitYoY2026")),
            }
    return out


def build_research_payload(code: str, research_row: dict[str, str]) -> dict[str, str]:
    fallback = RESEARCH_FALLBACK_MAP.get(code, {})
    q1_row = load_q1_financial_rows().get(code, {})
    return {
        "revenue2025": research_row.get("2025营收") or research_row.get("revenue2025") or fallback.get("2025营收", ""),
        "revenueYoY": research_row.get("同比") or research_row.get("revenueYoY") or fallback.get("同比", ""),
        "q1Revenue2026": research_row.get("q1Revenue2026") or q1_row.get("q1Revenue2026", ""),
        "q1RevenueYoY2026": research_row.get("q1RevenueYoY2026") or q1_row.get("q1RevenueYoY2026", ""),
        "q1NetProfit2026": research_row.get("q1NetProfit2026") or q1_row.get("q1NetProfit2026", ""),
        "q1NetProfitYoY2026": research_row.get("q1NetProfitYoY2026") or q1_row.get("q1NetProfitYoY2026", ""),
        "newOrders2026": research_row.get("2026新订单/新增项目") or research_row.get("newOrders2026") or fallback.get("2026新订单/新增项目", ""),
        "coreLogic": research_row.get("核心逻辑分析") or research_row.get("coreLogic") or fallback.get("核心逻辑分析", ""),
        "coreUsers": research_row.get("核心用户及订单") or research_row.get("coreUsers") or fallback.get("核心用户及订单", ""),
        "coreEdge": research_row.get("核心竞争力") or research_row.get("coreEdge") or fallback.get("核心竞争力", ""),
        "notes": research_row.get("备注") or research_row.get("notes") or fallback.get("备注", ""),
    }


def empty_research_payload() -> dict[str, str]:
    return {
        "revenue2025": "",
        "revenueYoY": "",
        "q1Revenue2026": "",
        "q1RevenueYoY2026": "",
        "q1NetProfit2026": "",
        "q1NetProfitYoY2026": "",
        "newOrders2026": "",
        "coreLogic": "",
        "coreUsers": "",
        "coreEdge": "",
        "notes": "",
    }


def build_dataset() -> list[dict]:
    research_map = load_research_rows()
    watchlist = build_watchlist(research_map)
    access_token = get_access_token()
    history_map = fetch_history(access_token)
    basic_map = fetch_basic(access_token)
    financial_map = fetch_financial_snapshots(access_token, [code for _, code in watchlist])
    order_book_map = fetch_order_book_snapshots(access_token, [code for _, code in watchlist])
    pe_ratio_map = fetch_pe_ratios(access_token, [code for _, code in watchlist])
    margin_financing_map = fetch_margin_financing_map([code for _, code in watchlist])

    dataset: list[dict] = []
    for name, code in watchlist:
        history = history_map[code]
        table = history["table"]
        times = history["time"][-30:]
        open_list = table["open"][-30:]
        high_list = table["high"][-30:]
        low_list = table["low"][-30:]
        close_list = table["close"][-30:]
        volume_list = table["volume"][-30:]
        amount_list = table["amount"][-30:]

        basic = basic_map.get(code, {})
        research = research_map.get(code, {})
        free_float_shares = float((basic.get("ths_free_float_shares_stock") or [0])[0] or 0)
        latest_close = float((basic.get("ths_close_price_stock") or [close_list[-1]])[0] or close_list[-1])
        float_mcap = free_float_shares * latest_close
        turnover_rate = (volume_list[-1] / free_float_shares * 100.0) if free_float_shares else None
        total_mcap = fetch_total_market_cap(code)
        pe_ratio = pe_ratio_map.get(code)
        today_pct = None
        if len(close_list) >= 2 and close_list[-2]:
            today_pct = (close_list[-1] / close_list[-2] - 1.0) * 100.0
        industry = fetch_industry(code)

        last_5 = []
        prev = None
        for i in range(max(0, len(times) - 5), len(times)):
            pct = None if prev is None else (close_list[i] / prev - 1.0) * 100.0
            last_5.append(
                {
                    "date": times[i],
                    "close": close_list[i],
                    "volume": volume_list[i],
                    "amount": amount_list[i],
                    "pct": pct,
                }
            )
            prev = close_list[i]

        research_payload = build_research_payload(code, research)
        financial = financial_map.get(code, {})
        if financial.get("revenue2025"):
            research_payload["revenue2025"] = financial["revenue2025"]
        if financial.get("revenueYoY"):
            research_payload["revenueYoY"] = financial["revenueYoY"]
        if financial.get("q1Revenue2026"):
            research_payload["q1Revenue2026"] = financial["q1Revenue2026"]
        if financial.get("q1RevenueYoY2026"):
            research_payload["q1RevenueYoY2026"] = financial["q1RevenueYoY2026"]
        if financial.get("q1NetProfit2026"):
            research_payload["q1NetProfit2026"] = financial["q1NetProfit2026"]
        if financial.get("q1NetProfitYoY2026"):
            research_payload["q1NetProfitYoY2026"] = financial["q1NetProfitYoY2026"]
        top_holders = fetch_top5_shareholders(code)
        business_segments = fetch_top3_business_segments(code)
        top_customers = fetch_top5_customers(name, code)
        main_business = resolve_main_business(code, industry=industry, business_segments=business_segments)

        dataset.append(
            {
                "name": name,
                "code": code,
                "totalMarketCap": total_mcap,
                "floatMarketCap": float_mcap,
                "todayVolume": volume_list[-1],
                "todayAmount": amount_list[-1],
                "turnoverRate": turnover_rate,
                "peRatio": pe_ratio,
                "marginFinancing": margin_financing_map.get(
                    code, {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None}
                ),
                "latestClose": latest_close,
                "todayPct": today_pct,
                "industry": industry,
                "mainBusiness": main_business,
                "research": research_payload,
                "topHolders": top_holders,
                "businessSegments": business_segments,
                "topCustomers": top_customers,
                "last5": last_5,
                "orderBook": order_book_map.get(code, {}),
                "kline": [
                    [times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]]
                    for i in range(len(times))
                ],
            }
        )
    return dataset


def enrich_strong_stocks_for_modal(access_token: str, strong_rows: list[dict]) -> list[dict]:
    if not strong_rows:
        return []
    codes = [item["code"] for item in strong_rows]
    try:
        history_map = fetch_history(access_token, codes)
    except Exception:
        history_map = {}
    try:
        pe_map = fetch_pe_ratios(access_token, codes)
    except Exception:
        pe_map = {}

    enriched: list[dict] = []
    for item in strong_rows:
        history = history_map.get(item["code"])
        kline = []
        if history:
            table = history.get("table") or {}
            times = (history.get("time") or [])[-30:]
            open_list = (table.get("open") or [])[-30:]
            high_list = (table.get("high") or [])[-30:]
            low_list = (table.get("low") or [])[-30:]
            close_list = (table.get("close") or [])[-30:]
            volume_list = (table.get("volume") or [])[-30:]
            amount_list = (table.get("amount") or [])[-30:]
            count = min(len(times), len(open_list), len(high_list), len(low_list), len(close_list), len(volume_list), len(amount_list))
            kline = [
                [times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]]
                for i in range(count)
            ]
        enriched.append(
            {
                **item,
                "research": empty_research_payload(),
                "peRatio": item.get("peRatio") if item.get("peRatio") is not None else pe_map.get(item["code"]),
                "marginFinancing": item.get("marginFinancing") or {
                    "date": "",
                    "finBalance": None,
                    "loanBalance": None,
                    "finBuyAmount": None,
                },
                "topHolders": item.get("topHolders") or {"reportDate": "", "totalRatio": None, "holders": []},
                "businessSegments": item.get("businessSegments") or {"reportDate": "", "category": "", "items": []},
                "topCustomers": item.get("topCustomers") or {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []},
                "kline": kline,
                "last5": item.get("last5") or [],
            }
        )
    return enriched


def fetch_market_overview(access_token: str | None) -> dict[str, object]:
    overview = {
        "tradeDate": AS_OF.isoformat(),
        "indices": [],
        "totalAmount": None,
        "totalVolume": None,
        "shAmount": None,
        "szAmount": None,
        "cybAmount": None,
        "kc50Amount": None,
        "indexRiseCount": None,
        "indexFallCount": None,
    }
    if access_token:
        try:
            cards = fetch_market_index_cards(access_token)
            overview["indices"] = cards
            by_name = {item["name"]: item for item in cards}
            sh_amount = to_float((by_name.get("上证指数") or {}).get("amount"))
            sz_amount = to_float((by_name.get("深证成指") or {}).get("amount"))
            sh_volume = to_float((by_name.get("上证指数") or {}).get("volume"))
            sz_volume = to_float((by_name.get("深证成指") or {}).get("volume"))
            overview["shAmount"] = sh_amount
            overview["szAmount"] = sz_amount
            overview["cybAmount"] = to_float((by_name.get("创业板指") or {}).get("amount"))
            overview["kc50Amount"] = to_float((by_name.get("科创50") or {}).get("amount"))
            if sh_amount is not None and sz_amount is not None:
                overview["totalAmount"] = sh_amount + sz_amount
            if sh_volume is not None and sz_volume is not None:
                overview["totalVolume"] = sh_volume + sz_volume
            pct_values = [to_float(item.get("pct")) for item in cards]
            overview["indexRiseCount"] = sum(1 for v in pct_values if v is not None and v > 0)
            overview["indexFallCount"] = sum(1 for v in pct_values if v is not None and v < 0)
        except Exception:
            overview["indices"] = []
    if not overview["indices"]:
        try:
            cards = fetch_market_index_cards_public()
            overview["indices"] = cards
            if cards:
                overview["tradeDate"] = str(cards[0].get("date") or overview["tradeDate"])
            by_name = {item["name"]: item for item in cards}
            sh_amount = to_float((by_name.get("上证指数") or {}).get("amount"))
            sz_amount = to_float((by_name.get("深证成指") or {}).get("amount"))
            sh_volume = to_float((by_name.get("上证指数") or {}).get("volume"))
            sz_volume = to_float((by_name.get("深证成指") or {}).get("volume"))
            overview["shAmount"] = sh_amount
            overview["szAmount"] = sz_amount
            overview["cybAmount"] = to_float((by_name.get("创业板指") or {}).get("amount"))
            overview["kc50Amount"] = to_float((by_name.get("科创50") or {}).get("amount"))
            if sh_amount is not None and sz_amount is not None:
                overview["totalAmount"] = sh_amount + sz_amount
            if sh_volume is not None and sz_volume is not None:
                overview["totalVolume"] = sh_volume + sz_volume
            pct_values = [to_float(item.get("pct")) for item in cards]
            overview["indexRiseCount"] = sum(1 for v in pct_values if v is not None and v > 0)
            overview["indexFallCount"] = sum(1 for v in pct_values if v is not None and v < 0)
        except Exception:
            overview["indices"] = []
    return overview


def fetch_market_index_cards(access_token: str) -> list[dict[str, object]]:
    history_map = fetch_history(access_token, [code for _, code in MARKET_INDEXES])
    cards: list[dict[str, object]] = []
    for name, code in MARKET_INDEXES:
        history = history_map.get(code) or {}
        table = history.get("table") or {}
        times = history.get("time") or []
        opens = table.get("open") or []
        highs = table.get("high") or []
        lows = table.get("low") or []
        closes = table.get("close") or []
        amounts = table.get("amount") or []
        volumes = table.get("volume") or []
        if not times or not closes:
            continue
        latest_close = to_float(closes[-1])
        prev_close = to_float(closes[-2]) if len(closes) >= 2 else None
        pct = None
        if latest_close not in (None, 0) and prev_close not in (None, 0):
            pct = (latest_close / prev_close - 1.0) * 100.0
        cards.append(
            {
                "name": name,
                "code": code,
                "date": times[-1],
                "latestClose": latest_close,
                "pct": pct,
                "amount": to_float(amounts[-1]) if amounts else None,
                "volume": to_float(volumes[-1]) if volumes else None,
                "miniKline": [
                    {
                        "date": times[i],
                        "open": to_float(opens[i]) if i < len(opens) else None,
                        "close": to_float(closes[i]) if i < len(closes) else None,
                        "low": to_float(lows[i]) if i < len(lows) else None,
                        "high": to_float(highs[i]) if i < len(highs) else None,
                    }
                    for i in range(max(0, len(times) - 18), len(times))
                    if i < len(closes)
                ],
                "detail": INDEX_DETAIL_MAP.get(code),
            }
        )
    return cards


def index_code_to_qq_symbol(code: str) -> str:
    raw = code.split(".")[0]
    return ("sh" if code.endswith(".SH") else "sz") + raw


def fetch_market_index_cards_public() -> list[dict[str, object]]:
    symbols = [index_code_to_qq_symbol(code) for _, code in MARKET_INDEXES]
    session = build_session()
    resp = request_with_fallback(
        "GET",
        "https://qt.gtimg.cn/q=" + ",".join(symbols),
        session=session,
        timeout=20,
    )
    text = resp.content.decode("gbk", errors="ignore")
    parsed: dict[str, list[str]] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        left, right = line.split("=", 1)
        symbol = left.removeprefix("v_")
        payload = right.strip().strip('"')
        if not payload:
            continue
        parsed[symbol] = payload.split("~")

    cards: list[dict[str, object]] = []
    for name, code in MARKET_INDEXES:
        parts = parsed.get(index_code_to_qq_symbol(code)) or []
        if len(parts) < 36:
            continue
        latest_close = to_float(parts[3])
        prev_close = to_float(parts[4])
        pct = to_float(parts[32])
        amount = None
        combined = parts[35].split("/") if parts[35] else []
        if len(combined) >= 3:
            amount = to_float(combined[2])
        volume = to_float(parts[36])
        timestamp = parts[30]
        trade_date = ""
        if len(timestamp) >= 8:
            trade_date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        if pct is None and latest_close not in (None, 0) and prev_close not in (None, 0):
            pct = (latest_close / prev_close - 1.0) * 100.0
        cards.append(
            {
                "name": name,
                "code": code,
                "date": trade_date,
                "latestClose": latest_close,
                "pct": pct,
                "amount": amount,
                "volume": volume,
                "detail": INDEX_DETAIL_MAP.get(code),
            }
        )
    return cards


def extract_js_var_text(source: str, var_name: str) -> str | None:
    match = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*(.*?);", source, re.S)
    if not match:
        return None
    return match.group(1).strip()


def parse_pct_text(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except Exception:
        return None


def fetch_stock_name_map(symbols: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not symbols:
        return out
    qq_symbols = []
    symbol_backmap: dict[str, str] = {}
    for symbol in symbols:
        market, raw = symbol.split(".")
        qq_symbol = ("sh" if market == "1" else "sz") + raw
        qq_symbols.append(qq_symbol)
        symbol_backmap[qq_symbol] = symbol
    session = build_session()
    for batch in batched(qq_symbols, 40):
        try:
            resp = request_with_fallback(
                "GET",
                "https://qt.gtimg.cn/q=" + ",".join(batch),
                session=session,
                timeout=20,
            )
            for line in resp.text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                left, right = line.split("=", 1)
                qq_symbol = left.removeprefix("v_")
                payload = right.strip().strip('"')
                parts = payload.split("~")
                if len(parts) >= 2 and qq_symbol in symbol_backmap:
                    out[symbol_backmap[qq_symbol]] = parts[1].strip() or symbol_backmap[qq_symbol]
        except Exception:
            continue
    return out


def probe_ifind_fund_quote_support(access_token: str | None) -> dict[str, object]:
    result: dict[str, object] = {
        "supportsFundQuotes": False,
        "supportsFundHoldings": False,
        "notes": "未检测",
        "samples": [],
    }
    if not access_token:
        result["notes"] = "未提供 iFinD access token，本页基金数据改走公开源。"
        return result
    session = build_session()
    samples: list[dict[str, object]] = []
    supported_codes: list[str] = []
    for code in ["161725.OF", "159915.OF", "588000.OF"]:
        try:
            resp = request_with_fallback(
                "POST",
                f"{BASE_URL}/cmd_history_quotation",
                session=session,
                json_payload={
                    "codes": code,
                    "indicators": "open,high,low,close,volume,amount",
                    "startdate": (AS_OF - timedelta(days=20)).strftime("%Y-%m-%d"),
                    "enddate": AS_OF.strftime("%Y-%m-%d"),
                    "functionpara": {"Fill": "Blank"},
                },
                headers={"Content-Type": "application/json", "access_token": access_token},
                timeout=20,
            )
            tables = resp.json().get("tables") or []
            times = (tables[0].get("time") if tables else None) or []
            if times:
                supported_codes.append(code)
                samples.append({"code": code, "lastDate": times[-1]})
        except Exception:
            continue
    if supported_codes:
        result["supportsFundQuotes"] = True
        result["notes"] = (
            "iFinD QuantAPI 已验证支持 .OF 基金行情历史；"
            "但当前未找到可直接返回前十大重仓股的现成 QuantAPI 持仓接口，"
            "机构持仓页改用天天基金公开披露数据。"
        )
        result["samples"] = samples
    else:
        result["notes"] = "iFinD 当前仅确认股票链路；基金持仓页改走天天基金公开源。"
    return result


def fetch_institution_holdings(access_token: str | None = None) -> dict[str, object]:
    session = build_session()
    fund_snapshots: list[dict[str, object]] = []
    stock_symbol_set: set[str] = set()
    for code in INSTITUTION_FUND_CODES:
        try:
            resp = request_with_fallback(
                "GET",
                f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(time.time())}",
                session=session,
                headers={"User-Agent": NEWS_UA, "Referer": f"https://fund.eastmoney.com/{code}.html"},
                timeout=20,
            )
            text = resp.text
            name_match = re.search(r'var\s+fS_name\s*=\s*"([^"]+)"', text)
            name = name_match.group(1).strip() if name_match else code
            stock_codes_raw = extract_js_var_text(text, "stockCodesNew") or "[]"
            stock_codes = json.loads(stock_codes_raw)
            scale_raw = extract_js_var_text(text, "Data_fluctuationScale") or "{}"
            scale_data = json.loads(scale_raw)
            categories = scale_data.get("categories") or []
            series = scale_data.get("series") or []
            latest_scale = series[-1].get("y") if series else None
            latest_mom = series[-1].get("mom") if series else None
            latest_scale_date = categories[-1] if categories else ""
            latest_holder_ratio = None
            holder_date = ""
            holder_raw = extract_js_var_text(text, "Data_holderStructure") or "{}"
            holder_data = json.loads(holder_raw)
            holder_series = holder_data.get("series") or []
            holder_categories = holder_data.get("categories") or []
            for entry in holder_series:
                if (entry.get("name") or "").strip() == "机构持有比例":
                    holder_values = entry.get("data") or []
                    latest_holder_ratio = holder_values[-1] if holder_values else None
                    holder_date = holder_categories[-1] if holder_categories else ""
                    break
            asset_raw = extract_js_var_text(text, "Data_assetAllocation") or "{}"
            asset_data = json.loads(asset_raw)
            asset_series = asset_data.get("series") or []
            asset_categories = asset_data.get("categories") or []
            latest_stock_ratio = None
            for entry in asset_series:
                if (entry.get("name") or "").strip() == "股票占净比":
                    values = entry.get("data") or []
                    latest_stock_ratio = values[-1] if values else None
                    break
            asset_date = asset_categories[-1] if asset_categories else ""
            manager_name = ""
            manager_work_time = ""
            manager_fund_size = ""
            manager_tenure_return = None
            manager_nav_date = ""
            manager_raw = extract_js_var_text(text, "Data_currentFundManager") or "[]"
            manager_data = json.loads(manager_raw)
            if manager_data:
                manager = manager_data[0] or {}
                manager_name = str(manager.get("name") or "").strip()
                manager_work_time = str(manager.get("workTime") or "").strip()
                manager_fund_size = str(manager.get("fundSize") or "").strip()
                profit = manager.get("profit") or {}
                manager_nav_date = str(profit.get("jzrq") or "") or str((manager.get("power") or {}).get("jzrq") or "")
                profit_series = profit.get("series") or []
                if profit_series:
                    series_data = (profit_series[0] or {}).get("data") or []
                    if series_data:
                        manager_tenure_return = (series_data[0] or {}).get("y")
            stock_symbol_set.update(stock_codes)
            fund_snapshots.append(
                {
                    "fundCode": code,
                    "fundName": name,
                    "fundScaleYi": latest_scale,
                    "scaleChange": latest_mom,
                    "scaleReportDate": latest_scale_date,
                    "recent1yReturn": parse_pct_text(re.search(r'var\s+syl_1n\s*=\s*"([^"]*)"', text).group(1) if re.search(r'var\s+syl_1n\s*=\s*"([^"]*)"', text) else None),
                    "recent6mReturn": parse_pct_text(re.search(r'var\s+syl_6y\s*=\s*"([^"]*)"', text).group(1) if re.search(r'var\s+syl_6y\s*=\s*"([^"]*)"', text) else None),
                    "recent3mReturn": parse_pct_text(re.search(r'var\s+syl_3y\s*=\s*"([^"]*)"', text).group(1) if re.search(r'var\s+syl_3y\s*=\s*"([^"]*)"', text) else None),
                    "recent1mReturn": parse_pct_text(re.search(r'var\s+syl_1y\s*=\s*"([^"]*)"', text).group(1) if re.search(r'var\s+syl_1y\s*=\s*"([^"]*)"', text) else None),
                    "institutionHolderRatio": latest_holder_ratio,
                    "holderReportDate": holder_date,
                    "stockAllocationRatio": latest_stock_ratio,
                    "assetReportDate": asset_date,
                    "managerName": manager_name,
                    "managerWorkTime": manager_work_time,
                    "managerFundSize": manager_fund_size,
                    "managerTenureReturn": manager_tenure_return,
                    "managerNavDate": manager_nav_date,
                    "holdingSymbols": stock_codes[:10],
                }
            )
        except Exception:
            continue

    name_map = fetch_stock_name_map(sorted(stock_symbol_set))
    rows: list[dict[str, object]] = []
    for item in fund_snapshots:
        holdings = [
            {
                "name": name_map.get(symbol, symbol.split(".")[1]),
                "code": symbol.split(".")[1] + (".SH" if symbol.startswith("1.") else ".SZ"),
            }
            for symbol in item.get("holdingSymbols", [])
        ]
        rows.append(
            {
                "fundCode": item["fundCode"],
                "fundName": item["fundName"],
                "fundScaleYi": item["fundScaleYi"],
                "scaleChange": item["scaleChange"],
                "scaleReportDate": item["scaleReportDate"],
                "recent1yReturn": item.get("recent1yReturn"),
                "recent6mReturn": item.get("recent6mReturn"),
                "recent3mReturn": item.get("recent3mReturn"),
                "recent1mReturn": item.get("recent1mReturn"),
                "institutionHolderRatio": item.get("institutionHolderRatio"),
                "holderReportDate": item.get("holderReportDate"),
                "stockAllocationRatio": item.get("stockAllocationRatio"),
                "assetReportDate": item.get("assetReportDate"),
                "managerName": item.get("managerName"),
                "managerWorkTime": item.get("managerWorkTime"),
                "managerFundSize": item.get("managerFundSize"),
                "managerTenureReturn": item.get("managerTenureReturn"),
                "managerNavDate": item.get("managerNavDate"),
                "topHoldings": holdings,
            }
        )
    rows.sort(key=lambda item: float(item.get("fundScaleYi") or 0.0), reverse=True)
    return {
        "tradeDate": AS_OF.isoformat(),
        "source": "天天基金 pingzhongdata / 腾讯行情名称映射",
        "ifindProbe": probe_ifind_fund_quote_support(access_token),
        "rows": rows,
    }


def is_limit_up_candidate(code: str, pct: float | None) -> bool:
    if pct is None:
        return False
    raw = code.split(".")[0]
    if raw.startswith(("4", "8")):
        return pct >= 29.0
    if raw.startswith("68") or raw.startswith("30"):
        return pct >= 19.0
    return pct >= 9.7


def load_report_modal_extras(current_codes: set[str], limit: int = 220) -> list[dict]:
    extras: list[dict] = []
    seen = set(current_codes)
    report_name_index = load_report_name_index()
    mapped_codes = set(REPORT_MODAL_NAME_MAP.values())
    for name, code in report_name_index.items():
        if code in seen:
            continue
        historical = load_historical_report_row(code) or {}
        historical_segments = historical.get("businessSegments") if historical else None
        historical_news = historical.get("latestNews") if historical else None
        seed = {
            "code": code,
            "name": name,
            "mainBusiness": resolve_main_business(
                code,
                industry=(historical.get("industry") if historical else "") or "",
                existing=historical.get("mainBusiness") if historical else "",
                business_segments=historical_segments,
                news_text=" ".join(
                    [
                        str((historical_news or {}).get("title") or ""),
                        str((historical_news or {}).get("summary") or ""),
                    ]
                ),
            ),
            "industry": "",
            "research": build_research_payload(code, {}),
            "marginFinancing": {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None},
            "topHolders": {"reportDate": "", "totalRatio": None, "holders": []},
            "businessSegments": {"reportDate": "", "category": "", "items": []},
            "topCustomers": {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []},
            "orderBook": {"time": "", "asks": [], "bids": []},
            "kline": [],
            "last5": [],
            "latestClose": None,
            "todayPct": None,
            "todayVolume": 0.0,
            "todayAmount": 0.0,
            "turnoverRate": None,
            "totalMarketCap": 0.0,
            "floatMarketCap": 0.0,
            "latestNews": {"time": "", "summary": "", "title": "", "link": "", "isRecent": False},
        }
        if historical:
            seed = {
                **seed,
                **historical,
                "code": code,
                "name": historical.get("name") or name,
                "mainBusiness": historical.get("mainBusiness") or seed["mainBusiness"],
                "research": historical.get("research") or seed["research"],
                "marginFinancing": historical.get("marginFinancing") or seed["marginFinancing"],
                "topHolders": historical.get("topHolders") or seed["topHolders"],
                "businessSegments": historical.get("businessSegments") or seed["businessSegments"],
                "topCustomers": historical.get("topCustomers") or seed["topCustomers"],
                "orderBook": historical.get("orderBook") or seed["orderBook"],
                "latestNews": historical.get("latestNews") or seed["latestNews"],
                "kline": historical.get("kline") or seed["kline"],
                "last5": historical.get("last5") or seed["last5"],
            }
        needs_live_fill = TRUST_ENV and code in mapped_codes and (
            not seed.get("kline")
            or not seed.get("latestClose")
            or not seed.get("totalMarketCap")
        )
        if needs_live_fill:
            try:
                seed = update_row_from_public_kline(seed, AS_OF)
            except Exception:
                pass
            try:
                seed["latestNews"] = fetch_latest_news_map([(name, code)]).get(code) or seed["latestNews"]
            except Exception:
                pass
        if TRUST_ENV and not normalize_main_business_value(seed.get("industry")):
            try:
                seed["industry"] = fetch_industry(code) or seed.get("industry") or ""
            except Exception:
                pass
        seed["mainBusiness"] = resolve_main_business(
            code,
            industry=seed.get("industry"),
            existing=seed.get("mainBusiness"),
            business_segments=seed.get("businessSegments"),
            news_text=" ".join(
                [
                    str((seed.get("latestNews") or {}).get("title") or ""),
                    str((seed.get("latestNews") or {}).get("summary") or ""),
                ]
            ),
        )
        seen.add(code)
        extras.append(seed)
        if len(extras) >= limit:
            return extras
    paths = sorted(OUT_DIR.glob("watchlist_strong_stocks_*.json"))
    for path in reversed(paths):
        try:
            path_date = date.fromisoformat(path.stem.removeprefix("watchlist_strong_stocks_"))
        except ValueError:
            continue
        if path_date > AS_OF:
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in rows:
            code = row.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            if TRUST_ENV and not normalize_main_business_value(row.get("industry")):
                try:
                    row["industry"] = fetch_industry(code) or row.get("industry") or ""
                except Exception:
                    pass
            row["mainBusiness"] = resolve_main_business(
                code,
                industry=row.get("industry"),
                existing=row.get("mainBusiness"),
                business_segments=row.get("businessSegments"),
                news_text=" ".join(
                    [
                        str((row.get("latestNews") or {}).get("title") or ""),
                        str((row.get("latestNews") or {}).get("summary") or ""),
                    ]
                ),
            )
            extras.append(row)
            if len(extras) >= limit:
                return extras
    return extras


def build_html(
    dataset: list[dict],
    strong_stocks: list[dict],
    market_overview: dict[str, object] | None = None,
    institution_holdings: dict[str, object] | None = None,
) -> str:
    market_overview = market_overview or {}
    institution_holdings = institution_holdings or {}
    watchlist_state_payload = load_watchlist_state_payload()
    initial_watchlist_status = watchlist_state_payload.get("watchlistStatus") or {}
    initial_strong_join_status = watchlist_state_payload.get("strongJoinStatus") or {}
    initial_report_entries = load_research_report_entries()
    initial_social_trackers = load_social_kol_watchlist()
    initial_social_entries = load_social_media_entries()
    market_dates = sorted(
        {
            row[0]
            for item in (dataset + strong_stocks)
            for row in (item.get("kline") or [])
            if row and row[0]
        }
    )
    latest_market_date = market_dates[-1] if market_dates else ""
    is_stale_market_data = bool(latest_market_date and latest_market_date != AS_OF.isoformat())
    data_source_label = "iFinD QuantAPI" if not is_stale_market_data else "iFinD QuantAPI / 本地缓存"
    stale_notice_html = ""
    if is_stale_market_data:
        stale_notice_html = (
            '<div class="notice-banner">'
            f'当前页面文件日期为 {AS_OF.isoformat()}，但行情主数据最新仅到 {latest_market_date}。'
            "当前为缓存展示，不应视为当日实盘数据。"
            "</div>"
        )
    market_index_codes = [code for _, code in MARKET_INDEXES]
    overview_indices_by_code = {item.get("code"): item for item in (market_overview.get("indices") or [])}
    overview_indices = [overview_indices_by_code[code] for code in market_index_codes if code in overview_indices_by_code]
    client_market_overview = {**market_overview, "indices": overview_indices}
    overview_trade_date = market_overview.get("tradeDate") or latest_market_date or AS_OF.isoformat()

    def render_index_mini_kline(item: dict) -> str:
        rows = []
        for raw in item.get("miniKline") or []:
            open_value = to_float(raw.get("open")) if isinstance(raw, dict) else None
            close_value = to_float(raw.get("close")) if isinstance(raw, dict) else None
            low_value = to_float(raw.get("low")) if isinstance(raw, dict) else None
            high_value = to_float(raw.get("high")) if isinstance(raw, dict) else None
            if open_value is None or close_value is None:
                continue
            low_value = min(v for v in [open_value, close_value, low_value] if v is not None)
            high_value = max(v for v in [open_value, close_value, high_value] if v is not None)
            rows.append((open_value, close_value, low_value, high_value))

        latest_close = to_float(item.get("latestClose"))
        pct = to_float(item.get("pct"))
        if len(rows) < 2 and latest_close not in (None, 0) and pct is not None:
            prev_close = latest_close / (1 + pct / 100) if pct != -100 else latest_close
            steps = 8
            rows = []
            for idx in range(steps):
                t = idx / max(steps - 1, 1)
                close_value = prev_close + (latest_close - prev_close) * t
                wave = (0.003 if idx % 2 == 0 else -0.002) * latest_close
                open_value = prev_close + (latest_close - prev_close) * max(t - 0.12, 0) + wave
                low_value = min(open_value, close_value) - abs(latest_close) * 0.003
                high_value = max(open_value, close_value) + abs(latest_close) * 0.003
                rows.append((open_value, close_value, low_value, high_value))

        if not rows:
            return """
              <svg class="market-mini-kline" viewBox="0 0 128 56" role="img" aria-label="暂无K线缩略图">
                <path class="mini-grid" d="M4 14H124M4 28H124M4 42H124" />
                <path class="mini-empty" d="M10 36C30 28 48 32 66 24C86 16 104 24 118 18" />
              </svg>
            """

        values = [value for row in rows for value in row]
        low_bound = min(values)
        high_bound = max(values)
        if high_bound == low_bound:
            high_bound = low_bound + 1

        width = 128
        height = 56
        left = 6
        right = 6
        top = 7
        bottom = 7
        chart_width = width - left - right
        chart_height = height - top - bottom

        def y_pos(value: float) -> float:
            return top + (high_bound - value) / (high_bound - low_bound) * chart_height

        step = chart_width / max(len(rows), 1)
        candle_width = max(2.2, min(5.0, step * 0.42))
        body_parts = []
        for idx, (open_value, close_value, low_value, high_value) in enumerate(rows):
            x = left + step * idx + step / 2
            y_open = y_pos(open_value)
            y_close = y_pos(close_value)
            y_low = y_pos(low_value)
            y_high = y_pos(high_value)
            body_y = min(y_open, y_close)
            body_h = max(abs(y_close - y_open), 1.2)
            cls = "rise" if close_value >= open_value else "fall"
            body_parts.append(
                f'<line class="mini-wick {cls}" x1="{x:.1f}" y1="{y_high:.1f}" x2="{x:.1f}" y2="{y_low:.1f}" />'
                f'<rect class="mini-candle {cls}" x="{x - candle_width / 2:.1f}" y="{body_y:.1f}" width="{candle_width:.1f}" height="{body_h:.1f}" rx="0.7" />'
            )
        return (
            '<svg class="market-mini-kline" viewBox="0 0 128 56" role="img" aria-label="指数K线缩略图">'
            '<path class="mini-grid" d="M4 14H124M4 28H124M4 42H124" />'
            + "".join(body_parts)
            + "</svg>"
        )

    index_cards_html = "".join(
        f"""
        <article class="market-card">
          <button class="market-card-button" type="button" {'data-index-code="' + str(item.get('code')) + '"' if item.get('detail') else 'disabled'}>
            <div class="market-card-content">
              <div class="market-card-main">
                <div class="market-card-head">
                  <strong>{item.get('name') or '-'}</strong>
                  <span>{str(item.get('date') or overview_trade_date)[5:]}</span>
                </div>
                <div class="market-card-price-row">
                  <div class="market-card-price">{'-' if item.get('latestClose') is None else f"{float(item['latestClose']):,.2f}"}</div>
                  <div class="market-card-pct {'pct-rise' if (item.get('pct') or 0) > 0 else 'pct-fall' if (item.get('pct') or 0) < 0 else 'pct-flat'}">{'-' if item.get('pct') is None else f"{float(item['pct']):+.2f}%"}</div>
                </div>
                <div class="market-card-meta">
                  <span>成交额 {'-' if item.get('amount') is None else fmt_yi_rmb(float(item['amount']))}</span>
                </div>
              </div>
              <div class="market-card-chart">
                {render_index_mini_kline(item)}
              </div>
            </div>
          </button>
        </article>
        """
        for item in overview_indices
    )
    institution_rows_data = institution_holdings.get("rows") or []
    institution_probe = institution_holdings.get("ifindProbe") or {}
    institution_rows_html = []
    for idx, item in enumerate(institution_rows_data, start=1):
        holdings = item.get("topHoldings") or []
        holdings_html = "".join(
            f'<span class="holding-chip" data-code="{holding.get("code") or ""}">{holding.get("name") or "-"} <small>{holding.get("code") or ""}</small></span>'
            for holding in holdings
        )
        perf_parts = []
        for label, value in (
            ("近1年", item.get("recent1yReturn")),
            ("近6月", item.get("recent6mReturn")),
            ("近3月", item.get("recent3mReturn")),
            ("近1月", item.get("recent1mReturn")),
        ):
            perf_parts.append(f"{label} {'-' if value is None else format_pct(float(value))}")
        manager_lines = []
        if item.get("managerName"):
            manager_lines.append(str(item.get("managerName")))
        if item.get("managerWorkTime"):
            manager_lines.append(str(item.get("managerWorkTime")))
        if item.get("managerFundSize"):
            manager_lines.append(f"在管 {item.get('managerFundSize')}")
        tenure_return = item.get("managerTenureReturn")
        if tenure_return is not None:
            manager_lines.append(f"任期收益 {format_pct(float(tenure_return))}")
        holder_ratio = item.get("institutionHolderRatio")
        stock_ratio = item.get("stockAllocationRatio")
        institution_rows_html.append(
            f"""
            <tr data-institution-row="true">
              <td class="index-cell">{idx}</td>
              <td>
                <div class="strong-name-cell">
                  <span class="stock-name">{item.get('fundName') or '-'}</span>
                  <span class="stock-code">{item.get('fundCode') or '-'}</span>
                </div>
              </td>
              <td class="nowrap-cell">
                {('-' if item.get('fundScaleYi') is None else f"{float(item['fundScaleYi']):,.2f}亿")}
                <div class="news-time">披露期 {item.get('scaleReportDate') or '-'}</div>
                <div class="news-time">{'环比 -' if item.get('scaleChange') in (None, '') else f"环比 {item.get('scaleChange')}"}</div>
              </td>
              <td class="nowrap-cell">
                {'<br />'.join(perf_parts)}
              </td>
              <td>
                <div>{'<br />'.join(manager_lines) or '-'}</div>
                <div class="news-time">
                  {'机构持有占比 -' if holder_ratio is None else f"机构持有占比 {float(holder_ratio):.2f}%"}
                  ·
                  {'股票仓位 -' if stock_ratio is None else f"股票仓位 {float(stock_ratio):.2f}%"}
                </div>
                <div class="news-time">
                  持有人披露期 {item.get('holderReportDate') or '-'} · 资产配置披露期 {item.get('assetReportDate') or '-'}
                </div>
              </td>
              <td>
                <div class="holding-chip-list">{holdings_html or '<span class="holding-chip empty">暂无</span>'}</div>
              </td>
            </tr>
            """
        )
    current_modal_codes = {item.get("code") for item in (dataset + strong_stocks) if item.get("code")}
    report_modal_extras = load_report_modal_extras(current_modal_codes)
    modal_items = dataset + strong_stocks + report_modal_extras
    cap_groups = [
        ("mega", "1000亿以上"),
        ("large", "500-1000亿"),
        ("small", "500亿以下"),
    ]

    def cap_group_key(item: dict) -> str:
        cap = float(item.get("totalMarketCap") or 0)
        if cap >= 1e11:
            return "mega"
        if cap >= 5e10:
            return "large"
        return "small"

    def format_pe_cell(item: dict) -> str:
        value = item.get("peRatio")
        if value is None:
            return "暂无"
        try:
            return f"{float(value):.2f}"
        except Exception:
            return "暂无"

    watchlist_rows_by_cap = {key: [] for key, _ in cap_groups}
    for item in dataset:
        pct_class = "pct-rise" if (item["todayPct"] or 0) > 0 else "pct-fall" if (item["todayPct"] or 0) < 0 else "pct-flat"
        group_key = cap_group_key(item)
        watchlist_rows_by_cap[cap_group_key(item)].append(
            f"""
            <tr data-watchlist-row="true" data-cap-group="{group_key}" data-code="{item['code']}" data-total-market-cap="{item['totalMarketCap'] or 0}" data-float-market-cap="{item['floatMarketCap'] or 0}" data-today-amount="{item['todayAmount'] or 0}" data-turnover-rate="{'' if item.get('turnoverRate') is None else item['turnoverRate']}" data-today-pct="{'' if item['todayPct'] is None else item['todayPct']}">
              <td class="index-cell" data-index-cell="watchlist"></td>
              <td data-search="{item['name']} {item['code']}">
                <button class="stock-trigger" type="button" data-code="{item['code']}">
                  <span class="stock-name">{item['name']}</span>
                  <span class="stock-code">{item['code']}</span>
                </button>
              </td>
              <td>{item['mainBusiness'] or '-'}</td>
              <td class="nowrap-cell">{fmt_yi(item['totalMarketCap'])} / {fmt_yi(item['floatMarketCap'])}</td>
              <td class="nowrap-cell">{fmt_yi_rmb(item['todayAmount'])}</td>
              <td class="nowrap-cell">{'-' if item.get('turnoverRate') is None else f"{item['turnoverRate']:.2f}%"}</td>
              <td class="nowrap-cell">{item['latestClose']:.2f}</td>
              <td class="{pct_class}">{'-' if item['todayPct'] is None else f"{item['todayPct']:+.2f}%"}</td>
              <td class="nowrap-cell">{format_pe_cell(item)}</td>
              <td>
                <select class="status-select" data-code="{item['code']}" aria-label="{item['name']}状态">
                  <option value="active">跟踪中</option>
                  <option value="removed">移除</option>
                </select>
              </td>
            </tr>
            """
        )

    def build_momentum_rows(rows: list[dict], index_key: str) -> list[str]:
        out = []
        for item in rows:
            pct_class = "pct-rise" if (item["todayPct"] or 0) > 0 else "pct-fall" if (item["todayPct"] or 0) < 0 else "pct-flat"
            group_key = cap_group_key(item)
            join_select_html = (
                f'''
                    <select class="join-watchlist-select" data-code="{item['code']}" data-name="{item['name']}" data-watchlist-member="true" aria-label="{item['name']}跟踪状态">
                      <option value="active" selected>跟踪中</option>
                      <option value="removed">已移除</option>
                    </select>
                '''
                if item["code"] in watchlist_codes
                else f'''
                    <select class="join-watchlist-select" data-code="{item['code']}" data-name="{item['name']}" aria-label="{item['name']}是否加入自选">
                      <option value="pending" selected>不加入</option>
                      <option value="joined">加入自选</option>
                    </select>
                '''
            )
            out.append(
                f"""
                <tr data-strong-stock-row="true" data-cap-group="{group_key}" data-code="{item['code']}" data-name="{item['name']}" data-main-business="{item['mainBusiness'] or '-'}" data-total-market-cap="{item['totalMarketCap'] or 0}" data-float-market-cap="{item['floatMarketCap'] or 0}" data-today-amount="{item['todayAmount'] or 0}" data-turnover-rate="{'' if item['turnoverRate'] is None else item['turnoverRate']}" data-today-pct="{'' if item['todayPct'] is None else item['todayPct']}">
                  <td class="index-cell" data-index-cell="{index_key}"></td>
                  <td data-search="{item['name']} {item['code']}">
                    <button class="stock-trigger strong-stock-trigger" type="button" data-code="{item['code']}">
                      <span class="stock-name">{item['name']}</span>
                      <span class="stock-code">{item['code']}</span>
                    </button>
                  </td>
                  <td>{item['mainBusiness'] or '-'}</td>
                  <td class="nowrap-cell">{fmt_yi(item['totalMarketCap'])} / {fmt_yi(item['floatMarketCap'])}</td>
                  <td class="nowrap-cell">{fmt_yi_rmb(item['todayAmount'])}</td>
                  <td class="nowrap-cell">{'-' if item['turnoverRate'] is None else f"{item['turnoverRate']:.2f}%"}</td>
                  <td class="{pct_class}">{'-' if item['todayPct'] is None else f"{item['todayPct']:+.2f}%"}</td>
                  <td class="nowrap-cell">{format_pe_cell(item)}</td>
                  <td>{join_select_html}</td>
                </tr>
                """
            )
        return out

    watchlist_codes = {item["code"] for item in dataset}
    strong_rows_by_cap = {
        key: build_momentum_rows([item for item in strong_stocks if cap_group_key(item) == key], "strong")
        for key, _ in cap_groups
    }

    strong_head_html = """
          <tr>
            <th>编号</th>
            <th>股票</th>
            <th>主营方向</th>
            <th>总市值 / 流通市值</th>
            <th>当日成交额</th>
            <th>换手率</th>
            <th>当日涨幅</th>
            <th>PE</th>
            <th>加入自选</th>
          </tr>
    """
    watchlist_head_html = """
          <tr>
            <th>编号</th>
            <th>股票</th>
            <th>主营方向</th>
            <th><button class="sort-button" type="button" data-watchlist-sort="floatMarketCap" data-default-direction="desc" data-active="true"><span>总市值 / 流通市值</span><span class="sort-indicator">↓</span></button></th>
            <th><button class="sort-button" type="button" data-watchlist-sort="todayAmount" data-default-direction="desc"><span>当日成交额</span><span class="sort-indicator">↕</span></button></th>
            <th><button class="sort-button" type="button" data-watchlist-sort="turnoverRate" data-default-direction="desc"><span>换手率</span><span class="sort-indicator">↕</span></button></th>
            <th>最新收盘</th>
            <th><button class="sort-button" type="button" data-watchlist-sort="todayPct" data-default-direction="desc"><span>当日涨幅</span><span class="sort-indicator">↕</span></button></th>
            <th>PE</th>
            <th>状态</th>
          </tr>
    """

    def render_grouped_rows(rows_by_cap: dict[str, list[str]], colspan: int) -> str:
        out = []
        for key, _ in cap_groups:
            rows = rows_by_cap.get(key) or []
            if not rows:
                continue
            out.extend(rows)
        if not out:
            return f'<tr class="empty-group-row" data-empty-row="true"><td colspan="{colspan}">暂无符合股票</td></tr>'
        return "".join(out)

    strong_grouped_rows_html = render_grouped_rows(strong_rows_by_cap, 9)
    watchlist_grouped_rows_html = render_grouped_rows(watchlist_rows_by_cap, 10)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Stock Killer-A Shares</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    :root {{
      --bg: #333333;
      --paper: #3b3b3b;
      --paper-2: #3b3b3b;
      --bg-2: #1c2b36;
      --paper: rgba(20, 31, 40, 0.88);
      --paper-2: rgba(16, 26, 35, 0.94);
      --ink: #edf7f7;
      --muted: #8ea5ae;
      --line: rgba(83, 242, 229, 0.15);
      --line-strong: rgba(83, 242, 229, 0.28);
      --accent: #58e7da;
      --accent-2: #84d9ff;
      --accent-soft: rgba(83, 242, 229, 0.08);
      --rise: #ff5a7d;
      --fall: #39e6a0;
      --warn: #ffbc64;
      --shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
      --panel-glow: 0 0 0 1px rgba(83, 242, 229, 0.06), 0 18px 40px rgba(0, 0, 0, 0.22), inset 0 1px 0 rgba(255,255,255,0.025);
      --radius-panel: 2px;
      --radius-card: 2px;
      --radius-chip: 999px;
      --cut-size: 14px;
    }}
    body[data-theme="light"] {{
      --bg: #ffffff;
      --paper: #f9f9fa;
      --paper-2: #f9f9fa;
      --bg-2: #e3edf2;
      --paper: rgba(250, 253, 255, 0.92);
      --paper-2: rgba(255, 255, 255, 0.96);
      --ink: #10212b;
      --muted: #627786;
      --line: rgba(11, 67, 86, 0.12);
      --line-strong: rgba(15, 132, 151, 0.24);
      --accent: #007f8c;
      --accent-2: #0099d5;
      --accent-soft: rgba(0, 127, 140, 0.08);
      --rise: #d73d63;
      --fall: #0e9f69;
      --warn: #b7791f;
      --shadow: 0 20px 50px rgba(32, 62, 81, 0.10);
      --panel-glow: 0 0 0 1px rgba(0, 127, 140, 0.05), 0 18px 36px rgba(17, 52, 71, 0.08), inset 0 1px 0 rgba(255,255,255,0.7);
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: var(--bg);
      min-height: 100vh;
      position: relative;
    }}
    body[data-theme="light"] {{
      background: var(--bg);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(rgba(83,242,229,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(83,242,229,0.03) 1px, transparent 1px);
      background-size: 24px 24px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.6), transparent 92%);
      opacity: 0.28;
    }}
    body::after {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(180deg, rgba(255,255,255,0.015), rgba(255,255,255,0));
      opacity: 0.8;
    }}
    .app-shell {{
      width: min(1680px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 16px 0 40px;
      display: grid;
      grid-template-columns: 172px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .sidebar {{
      position: sticky;
      top: 12px;
      min-height: calc(100vh - 24px);
      align-self: stretch;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      padding: 18px 12px;
      border-radius: 0;
      background: linear-gradient(180deg, rgba(15,23,31,0.96), rgba(12,19,26,0.98));
      border: 1px solid rgba(83,242,229,0.12);
      box-shadow: 0 0 0 1px rgba(83,242,229,0.04), 0 18px 34px rgba(0, 0, 0, 0.18);
      backdrop-filter: blur(10px);
      overflow: hidden;
    }}
    .sidebar-brand strong {{
      display: block;
      font-size: 14px;
      letter-spacing: 0.12em;
      color: #f0fffd;
      text-transform: uppercase;
      line-height: 1.35;
      text-shadow: none;
    }}
    body[data-theme="light"] .sidebar-brand strong {{
      color: #0d2028;
      text-shadow: none;
    }}
    .sidebar-brand span {{
      display: none;
    }}
    body[data-theme="light"] .sidebar {{
      background: linear-gradient(180deg, rgba(250,252,253,0.98), rgba(241,246,249,0.98));
      border-color: rgba(0,127,140,0.10);
      box-shadow: 0 0 0 1px rgba(0,127,140,0.03), 0 14px 28px rgba(28, 59, 78, 0.06);
    }}
    .sidebar-nav {{
      position: relative;
      margin-top: 18px;
      display: grid;
      gap: 6px;
      align-content: start;
    }}
    .theme-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .theme-toggle button {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      color: var(--muted);
      border-radius: 999px;
      width: 34px;
      height: 34px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: 180ms ease;
    }}
    .theme-toggle button.active {{
      color: var(--ink);
      background: linear-gradient(135deg, var(--accent-soft), rgba(255,255,255,0.02));
      border-color: var(--line-strong);
    }}
    .theme-toggle svg {{
      width: 16px;
      height: 16px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    .sidebar-link {{
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      border-left: 2px solid transparent;
      background: transparent;
      color: #9eb4bb;
      border-radius: 0;
      padding: 10px 10px 10px 12px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: 0.10em;
      transition: 180ms ease;
      text-transform: uppercase;
    }}
    .sidebar-link.active {{
      color: #efffff;
      border-color: rgba(83,242,229,0.10);
      border-left-color: var(--accent);
      background: rgba(83,242,229,0.06);
      box-shadow: inset 0 0 0 1px rgba(83,242,229,0.04);
    }}
    .sidebar-link:hover {{
      color: #e8ffff;
      border-left-color: rgba(83,242,229,0.42);
      background: rgba(83,242,229,0.03);
    }}
    body[data-theme="light"] .sidebar-link {{
      color: #3e5663;
      background: transparent;
      border-left-color: transparent;
    }}
    body[data-theme="light"] .sidebar-link.active {{
      color: #07202a;
      background: rgba(0,127,140,0.06);
      border-color: rgba(0,127,140,0.08);
      border-left-color: var(--accent);
      box-shadow: inset 0 0 0 1px rgba(0,127,140,0.03);
    }}
    .wrap {{
      width: 100%;
      min-width: 0;
      padding: 0 0 56px;
    }}
    .page-view[hidden] {{
      display: none !important;
    }}
    .hero {{
      position: relative;
      padding: 22px 24px;
      background:
        radial-gradient(circle at top right, rgba(83,242,229,0.14), transparent 24%),
        linear-gradient(180deg, rgba(11,21,31,0.96), rgba(8,15,24,0.98));
      border: 1px solid var(--line);
      border-radius: var(--radius-panel);
      box-shadow: var(--panel-glow);
      backdrop-filter: blur(14px);
      overflow: hidden;
      clip-path: polygon(0 0, calc(100% - var(--cut-size)) 0, 100% var(--cut-size), 100% 100%, var(--cut-size) 100%, 0 calc(100% - var(--cut-size)));
    }}
    body[data-theme="light"] .hero {{
      background:
        radial-gradient(circle at top right, rgba(0,153,213,0.10), transparent 24%),
        linear-gradient(180deg, rgba(250,253,255,0.98), rgba(242,248,251,0.98));
    }}
    .hero::after {{
      content: "";
      position: absolute;
      left: 24px;
      right: 24px;
      top: 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(83,242,229,0.36), transparent);
    }}
    .notice-banner {{
      margin-top: 12px;
      border: 1px solid rgba(255,188,100,0.28);
      background: linear-gradient(135deg, rgba(64,36,10,0.66), rgba(27,20,8,0.72));
      color: #ffcf8f;
      border-radius: 8px;
      padding: 12px 14px;
      font-size: 13px;
      line-height: 1.5;
      box-shadow: 0 10px 24px rgba(0,0,0,0.18);
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(24px, 3vw, 38px);
      line-height: 1;
      letter-spacing: -0.05em;
      color: #f3ffff;
      text-shadow: 0 0 30px rgba(83,242,229,0.12);
    }}
    body[data-theme="light"] .hero h1 {{
      color: #0d2028;
      text-shadow: none;
    }}
    .hero-actions {{
      position: absolute;
      top: 16px;
      right: 18px;
      display: flex;
      align-items: center;
      gap: 8px;
      z-index: 2;
    }}
    .search-bar {{
      margin-top: 12px;
      display: flex;
      align-items: center;
      gap: 10px;
      max-width: 560px;
      padding: 11px 14px;
      border-radius: 8px;
      border: 1px solid rgba(83,242,229,0.14);
      background: rgba(5, 13, 20, 0.88);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
    }}
    body[data-theme="light"] .search-bar {{
      background: rgba(255,255,255,0.86);
      border-color: rgba(0,127,140,0.14);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.75);
    }}
    .search-bar span {{
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .search-input {{
      width: 100%;
      border: none;
      outline: none;
      background: transparent;
      color: var(--ink);
      font-size: 13px;
      font-weight: 600;
    }}
    .search-input::placeholder {{
      color: #597380;
      font-weight: 500;
    }}
    .meta {{
      margin-top: 14px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .meta span, .pill {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 11px;
      background: rgba(83,242,229,0.08);
      color: #9dfaf0;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      border: 1px solid rgba(83,242,229,0.12);
    }}
    body[data-theme="light"] .meta span,
    body[data-theme="light"] .pill {{
      background: rgba(0,127,140,0.06);
      color: #0b6671;
      border-color: rgba(0,127,140,0.10);
    }}
    .market-overview {{
      margin-top: 8px;
      padding: 8px 10px;
    }}
    .market-overview-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }}
    .market-overview-head h2 {{
      margin: 0;
      font-size: 15px;
      line-height: 1.1;
      letter-spacing: -0.03em;
      color: #efffff;
    }}
    body[data-theme="light"] .market-overview-head h2,
    body[data-theme="light"] .section-head h2,
    body[data-theme="light"] .modal-head h3,
    body[data-theme="light"] .index-modal-head h3 {{
      color: #10212b;
    }}
    .market-overview-head p {{
      margin: 4px 0 0;
      font-size: 11px;
      color: var(--muted);
    }}
    .market-index-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }}
    .market-card {{
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(13,24,35,0.92), rgba(8,17,25,0.96));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.02);
      clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 10px 100%, 0 calc(100% - 10px));
    }}
    body[data-theme="light"] .market-card,
    body[data-theme="light"] .market-stat,
    body[data-theme="light"] .summary-table-wrap,
    body[data-theme="light"] .panel,
    body[data-theme="light"] .report-entry-panel,
    body[data-theme="light"] .removed-item,
    body[data-theme="light"] .index-summary-item,
    body[data-theme="light"] .index-leader-item,
    body[data-theme="light"] .research-panel,
    body[data-theme="light"] .chart-insights .research-section,
    body[data-theme="light"] .research-metric,
    body[data-theme="light"] .index-modal-card,
    body[data-theme="light"] .modal-card {{
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(245,249,251,0.98));
    }}
    .market-card-button {{
      width: 100%;
      border: none;
      background: transparent;
      padding: 6px 8px;
      text-align: left;
      color: inherit;
      cursor: pointer;
    }}
    .market-card-button:disabled {{
      cursor: default;
    }}
    .market-card-content {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 128px;
      gap: 8px;
      align-items: center;
    }}
    .market-card-main {{
      min-width: 0;
    }}
    .market-card-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 11px;
      color: var(--muted);
    }}
    .market-card-head strong {{
      color: #f1ffff;
      font-size: 13px;
      letter-spacing: -0.02em;
    }}
    .market-card-price {{
      margin-top: 4px;
      font-size: 19px;
      font-weight: 800;
      letter-spacing: -0.04em;
      color: #f7ffff;
    }}
    body[data-theme="light"] .market-card-head strong,
    body[data-theme="light"] .market-card-price,
    body[data-theme="light"] .market-stat strong,
    body[data-theme="light"] .index-summary-item strong,
    body[data-theme="light"] .index-leader-item strong,
    body[data-theme="light"] .research-metric strong {{
      color: #0f2230;
    }}
    .market-card-price-row {{
      display: flex;
      align-items: baseline;
      gap: 8px;
    }}
    .market-card-pct {{
      font-size: 14px;
      font-weight: 800;
      letter-spacing: -0.03em;
    }}
    .market-card-meta {{
      margin-top: 3px;
      font-size: 10px;
      color: var(--muted);
    }}
    .market-card-chart {{
      width: 128px;
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-left: 1px solid rgba(83,242,229,0.08);
      padding-left: 6px;
    }}
    body[data-theme="light"] .market-card-chart {{
      border-left-color: rgba(0,127,140,0.08);
    }}
    .market-mini-kline {{
      width: 128px;
      height: 56px;
      display: block;
      overflow: visible;
    }}
    .mini-grid {{
      fill: none;
      stroke: rgba(142,165,174,0.20);
      stroke-width: 0.8;
    }}
    .mini-wick {{
      stroke-width: 1.1;
      stroke-linecap: round;
    }}
    .mini-candle.rise,
    .mini-wick.rise {{
      fill: var(--rise);
      stroke: var(--rise);
    }}
    .mini-candle.fall,
    .mini-wick.fall {{
      fill: var(--fall);
      stroke: var(--fall);
    }}
    .mini-empty {{
      fill: none;
      stroke: var(--muted);
      stroke-width: 2;
      opacity: 0.55;
    }}
    .market-stat-grid {{
      margin-top: 8px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }}
    .market-stat {{
      padding: 8px 10px;
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(12,23,34,0.92), rgba(8,16,25,0.96));
      clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 10px 100%, 0 calc(100% - 10px));
    }}
    .market-stat span {{
      display: block;
      font-size: 11px;
      color: var(--muted);
    }}
    .market-stat strong {{
      display: block;
      margin-top: 4px;
      font-size: 16px;
      letter-spacing: -0.03em;
      color: #f2fffe;
    }}
    .index-modal {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(18, 24, 30, 0.42);
      backdrop-filter: blur(6px);
      z-index: 31;
    }}
    .index-modal.open {{
      display: flex;
    }}
    .index-modal-card {{
      width: min(720px, calc(100vw - 24px));
      max-height: min(90vh, 840px);
      overflow: auto;
      background: var(--paper-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-panel);
      box-shadow: var(--panel-glow);
      padding: 16px;
      clip-path: polygon(0 0, calc(100% - var(--cut-size)) 0, 100% var(--cut-size), 100% 100%, var(--cut-size) 100%, 0 calc(100% - var(--cut-size)));
    }}
    .index-modal-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}
    .index-modal-head h3 {{
      margin: 0;
      font-size: 20px;
      letter-spacing: -0.04em;
    }}
    .index-modal-head p {{
      margin: 6px 0 0;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .index-summary-grid {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .index-summary-item,
    .index-leader-item {{
      padding: 12px 14px;
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(12,23,35,0.92), rgba(8,17,25,0.96));
      clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 10px 100%, 0 calc(100% - 10px));
    }}
    .index-summary-item span,
    .index-leader-item span {{
      display: block;
      font-size: 11px;
      color: var(--muted);
    }}
    .index-summary-item strong {{
      display: block;
      margin-top: 8px;
      font-size: 18px;
      letter-spacing: -0.03em;
    }}
    .index-leader-list {{
      margin-top: 14px;
      display: grid;
      gap: 10px;
    }}
    .index-leader-item strong {{
      display: block;
      font-size: 16px;
      letter-spacing: -0.03em;
    }}
    .index-leader-item em {{
      display: block;
      margin-top: 8px;
      font-style: normal;
      font-size: 12px;
      color: #aac3ca;
      line-height: 1.45;
    }}
    .summary-table-wrap, .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius-panel);
      box-shadow: var(--panel-glow);
      clip-path: polygon(0 0, calc(100% - var(--cut-size)) 0, 100% var(--cut-size), 100% 100%, var(--cut-size) 100%, 0 calc(100% - var(--cut-size)));
    }}
    .summary-table-wrap {{
      margin-top: 16px;
      padding: 6px 10px 10px;
      overflow-x: auto;
    }}
    .summary-table tbody tr[data-cap-group="mega"] td {{
      background: rgba(31, 41, 55, 0.130);
    }}
    .summary-table tbody tr[data-cap-group="large"] td {{
      background: rgba(31, 41, 55, 0.075);
    }}
    .summary-table tbody tr[data-cap-group="small"] td {{
      background: rgba(31, 41, 55, 0.025);
    }}
    .summary-table tbody tr[data-cap-group="mega"]:hover td {{
      background: rgba(31, 41, 55, 0.180);
    }}
    .summary-table tbody tr[data-cap-group="large"]:hover td {{
      background: rgba(31, 41, 55, 0.125);
    }}
    .summary-table tbody tr[data-cap-group="small"]:hover td {{
      background: rgba(31, 41, 55, 0.075);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="mega"] td {{
      background: rgba(255, 255, 255, 0.130);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="large"] td {{
      background: rgba(255, 255, 255, 0.080);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="small"] td {{
      background: rgba(255, 255, 255, 0.035);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="mega"]:hover td {{
      background: rgba(255, 255, 255, 0.190);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="large"]:hover td {{
      background: rgba(255, 255, 255, 0.135);
    }}
    body[data-theme="dark"] .summary-table tbody tr[data-cap-group="small"]:hover td {{
      background: rgba(255, 255, 255, 0.085);
    }}
    .removed-panel {{
      margin-top: 12px;
      padding: 10px 12px 12px;
      background: rgba(38, 23, 10, 0.36);
      border: 1px dashed rgba(255,188,100,0.22);
      border-radius: 8px;
    }}
    .removed-panel[hidden] {{
      display: none;
    }}
    .removed-head {{
      display: block;
    }}
    .removed-head h3 {{
      margin: 0;
      font-size: 13px;
    }}
    .removed-head p {{
      margin: 2px 0 0;
      font-size: 10px;
      color: var(--muted);
    }}
    .removed-list {{
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .removed-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 6px;
      background: rgba(10,19,29,0.86);
      clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px));
    }}
    .removed-info {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 92px;
    }}
    .removed-info .stock-name {{
      font-size: 13px;
    }}
    .removed-item .status-select {{
      min-width: 80px;
    }}
    .section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin: 16px 2px 6px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 16px;
      line-height: 1.1;
      letter-spacing: -0.03em;
    }}
    .section-head p {{
      margin: 0;
      font-size: 11px;
      color: var(--muted);
    }}
    .summary-table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 700px;
    }}
    .summary-table th,
    .summary-table td {{
      padding: 7px 9px;
      text-align: left;
      border-bottom: 1px solid rgba(83,242,229,0.08);
      vertical-align: middle;
    }}
    .index-cell {{
      width: 38px;
      text-align: center !important;
      color: var(--muted);
      white-space: nowrap;
    }}
    .nowrap-cell {{
      white-space: nowrap;
    }}
    .summary-table th {{
      font-size: 11px;
      color: var(--muted);
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      background: rgba(83,242,229,0.02);
    }}
    body[data-theme="light"] .summary-table th {{
      background: rgba(0,127,140,0.03);
      color: #67808e;
    }}
    .sort-button {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: none;
      background: transparent;
      padding: 0;
      margin: 0;
      font: inherit;
      color: inherit;
      cursor: pointer;
      text-transform: inherit;
      letter-spacing: inherit;
    }}
    .sort-button:hover {{
      color: var(--accent);
    }}
    .sort-indicator {{
      min-width: 10px;
      font-size: 10px;
      color: rgba(100,116,139,0.72);
    }}
    .sort-button[data-active="true"] .sort-indicator {{
      color: var(--accent);
    }}
    .summary-table tbody tr:last-child td {{
      border-bottom: none;
    }}
    .summary-table tbody tr:hover {{
      background: rgba(83,242,229,0.06);
    }}
    .summary-table tbody tr[data-watchlist-row="true"] {{
      cursor: pointer;
    }}
    .empty-group-row td {{
      padding: 10px 9px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
    .holding-chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .holding-chip {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border-radius: 999px;
      padding: 5px 9px;
      background: rgba(83,242,229,0.08);
      color: #d8f7f4;
      font-size: 11px;
      line-height: 1.2;
      white-space: nowrap;
      border: 1px solid rgba(83,242,229,0.10);
    }}
    .holding-chip small {{
      color: var(--muted);
      font-size: 10px;
    }}
    .holding-chip.empty {{
      color: var(--muted);
    }}
    .institution-note {{
      margin-top: 12px;
    }}
    .institution-table td {{
      vertical-align: top;
    }}
    .report-entry-panel {{
      margin-top: 16px;
      padding: 16px;
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: var(--radius-panel);
      background: linear-gradient(180deg, rgba(11,22,32,0.94), rgba(8,16,25,0.98));
      clip-path: polygon(0 0, calc(100% - var(--cut-size)) 0, 100% var(--cut-size), 100% 100%, var(--cut-size) 100%, 0 calc(100% - var(--cut-size)));
    }}
    .report-form-grid {{
      display: grid;
      grid-template-columns: 220px 180px minmax(0, 1fr);
      gap: 12px;
      align-items: end;
    }}
    .report-field {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .report-field.full-span {{
      grid-column: 1 / -1;
    }}
    .report-field label {{
      font-size: 11px;
      color: var(--muted);
      font-weight: 700;
      letter-spacing: 0.10em;
      text-transform: uppercase;
    }}
    .report-input,
    .report-textarea {{
      width: 100%;
      border: 1px solid rgba(83,242,229,0.12);
      border-radius: 6px;
      background: rgba(4,12,19,0.92);
      color: var(--ink);
      font: inherit;
      padding: 11px 12px;
      outline: none;
      color-scheme: dark;
    }}
    body[data-theme="light"] .report-input,
    body[data-theme="light"] .report-textarea {{
      background: rgba(255,255,255,0.94);
      color: #10212b;
      color-scheme: light;
    }}
    .report-input:focus,
    .report-textarea:focus {{
      border-color: rgba(15,118,110,0.36);
      box-shadow: 0 0 0 3px rgba(15,118,110,0.10);
    }}
    .report-textarea {{
      min-height: 128px;
      resize: vertical;
    }}
    .report-actions {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 12px;
    }}
    .report-action-note {{
      font-size: 12px;
      color: var(--muted);
    }}
    .report-action-note[data-state="busy"] {{
      color: var(--accent-2);
    }}
    .report-action-note[data-state="error"] {{
      color: #f89b9b;
    }}
    .report-save-button {{
      border: none;
      border-radius: var(--radius-chip);
      background: linear-gradient(135deg, #53f2e5, #1ec8ff);
      color: #041017;
      padding: 10px 16px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(83,242,229,0.16);
    }}
    .report-save-button:hover {{
      filter: brightness(1.02);
    }}
    .report-tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .report-tag {{
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      font-size: 11px;
      color: var(--accent);
      border: 1px solid rgba(83,242,229,0.16);
      background: rgba(83,242,229,0.08);
      clip-path: polygon(0 0, calc(100% - 6px) 0, 100% 6px, 100% 100%, 6px 100%, 0 calc(100% - 6px));
    }}
    body[data-theme="light"] .report-tag {{
      color: #0f6f78;
      background: rgba(0,127,140,0.08);
      border-color: rgba(0,127,140,0.16);
    }}
    .report-tag-button {{
      appearance: none;
      border: 0;
      background: transparent;
      padding: 0;
      margin: 0;
      cursor: pointer;
    }}
    .report-head-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .report-hidden-toggle {{
      gap: 6px;
      cursor: pointer;
      user-select: none;
    }}
    .report-hidden-toggle input {{
      margin: 0;
    }}
    .report-entry-row {{
      cursor: pointer;
    }}
    .report-row-hidden {{
      opacity: 0.46;
    }}
    .report-hide-cell {{
      text-align: center;
    }}
    .report-hide-checkbox {{
      margin: 0;
    }}
    .report-drawer {{
      position: fixed;
      inset: 0;
      z-index: 55;
      display: flex;
      justify-content: flex-end;
      background: rgba(0, 0, 0, 0.34);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.18s ease;
    }}
    .report-drawer.open {{
      opacity: 1;
      pointer-events: auto;
    }}
    .report-drawer-panel {{
      width: min(520px, calc(100vw - 28px));
      height: 100%;
      display: grid;
      grid-template-rows: auto 1fr;
      border: 1px solid var(--line);
      border-right: 0;
      border-radius: var(--radius-card) 0 0 var(--radius-card);
      background: var(--paper-2);
      color: var(--ink);
      box-shadow: var(--shadow);
      transform: translateX(100%);
      transition: transform 0.22s ease;
      overflow: hidden;
    }}
    .report-drawer.open .report-drawer-panel {{
      transform: translateX(0);
    }}
    .report-drawer-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 18px 14px;
      border-bottom: 1px solid var(--line);
    }}
    .report-drawer-head h3 {{
      margin: 0 0 6px;
      font-size: 18px;
      line-height: 1.25;
    }}
    .report-drawer-head p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .report-drawer-body {{
      padding: 16px 18px 22px;
      overflow: auto;
      display: grid;
      align-content: start;
      gap: 14px;
    }}
    .report-detail-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .report-detail-meta span {{
      padding: 5px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 11px;
      background: rgba(255,255,255,0.04);
    }}
    .report-detail-section {{
      display: grid;
      gap: 8px;
    }}
    .report-detail-section h4 {{
      margin: 0;
      font-size: 12px;
      color: var(--muted);
    }}
    .report-detail-section p,
    .report-detail-section pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: inherit;
      font-size: 12px;
      line-height: 1.6;
    }}
    .report-detail-source {{
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-card);
      background: var(--paper);
      max-height: 52vh;
      overflow: auto;
    }}
    .report-target-hover-card {{
      position: fixed;
      z-index: 40;
      min-width: 190px;
      display: grid;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-card);
      background: var(--paper-2);
      color: var(--ink);
      box-shadow: var(--shadow);
      pointer-events: none;
    }}
    .report-target-hover-card[hidden] {{
      display: none;
    }}
    .report-hover-title {{
      font-size: 12px;
      font-weight: 700;
    }}
    .report-hover-lines {{
      display: grid;
      gap: 5px;
    }}
    .report-hover-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      white-space: nowrap;
      font-size: 11px;
    }}
    .report-hover-label {{
      color: var(--muted);
    }}
    .report-hover-value {{
      font-weight: 700;
    }}
    .report-summary {{
      display: grid;
      gap: 6px;
    }}
    .report-summary strong {{
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
    }}
    .report-summary p {{
      margin: 0;
      line-height: 1.55;
      white-space: pre-wrap;
    }}
    .report-stance {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      padding: 2px 8px;
      font-size: 11px;
      border: 1px solid rgba(132,217,255,0.18);
      color: var(--accent-2);
      background: rgba(132,217,255,0.08);
    }}
    .report-list-lines {{
      margin: 0;
      padding-left: 16px;
      display: grid;
      gap: 4px;
      line-height: 1.45;
    }}
    .report-target-trigger {{
      display: inline-flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 2px;
      border: none;
      background: transparent;
      padding: 0;
      color: inherit;
      cursor: pointer;
      text-align: left;
    }}
    .report-target-trigger:hover .stock-name {{
      color: var(--accent);
      text-decoration: underline;
    }}
    .report-empty {{
      margin-top: 12px;
      padding: 18px 16px;
      border: 1px dashed rgba(83,242,229,0.18);
      border-radius: 8px;
      color: var(--muted);
      text-align: center;
      background: rgba(10,19,29,0.72);
    }}
    .pct-rise {{
      color: var(--rise);
      font-weight: 700;
    }}
    .pct-fall {{
      color: var(--fall);
      font-weight: 700;
    }}
    .pct-flat {{
      color: var(--muted);
      font-weight: 600;
    }}
    .stock-trigger {{
      display: inline-flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 2px;
      border: none;
      background: transparent;
      padding: 0;
      color: inherit;
      cursor: pointer;
      font: inherit;
      text-align: left;
    }}
    .stock-trigger:hover .stock-name {{
      color: var(--accent);
    }}
    .stock-name {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.03em;
      line-height: 1.15;
    }}
    body[data-theme="light"] .stock-name {{
      color: #10212b;
    }}
    .stock-code {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.06em;
    }}
    .status-select {{
      min-width: 88px;
      border: 1px solid rgba(83,242,229,0.16);
      background: rgba(83,242,229,0.08);
      color: #b5fffa;
      border-radius: 6px;
      padding: 6px 24px 6px 10px;
      font-size: 11px;
      font-weight: 700;
      outline: none;
    }}
    .join-watchlist-select {{
      min-width: 96px;
      border: 1px solid rgba(83,242,229,0.16);
      background: rgba(83,242,229,0.08);
      color: #b5fffa;
      border-radius: 6px;
      padding: 6px 24px 6px 10px;
      font-size: 11px;
      font-weight: 700;
      outline: none;
    }}
    .join-watchlist-select:disabled {{
      color: var(--muted);
      border-color: rgba(31,42,55,0.10);
      background: rgba(31,42,55,0.04);
      cursor: not-allowed;
    }}
    .status-select[data-state="removed"] {{
      color: #9a3412;
      border-color: rgba(154,52,18,0.16);
      background: rgba(154,52,18,0.08);
    }}
    .join-watchlist-select[data-state="removed"],
    .join-watchlist-select[data-state="pending"] {{
      color: #9a3412;
      border-color: rgba(154,52,18,0.16);
      background: rgba(154,52,18,0.08);
    }}
    .join-watchlist-select[data-state="joined"],
    .join-watchlist-select[data-state="active"] {{
      color: var(--accent);
      border-color: rgba(15,118,110,0.16);
      background: rgba(15,118,110,0.08);
    }}
    .strong-name-cell {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .news-cell {{
      min-width: 240px;
      max-width: 320px;
    }}
    .news-link {{
      display: block;
      color: inherit;
      text-decoration: none;
    }}
    .news-link:hover .news-summary {{
      color: var(--accent);
    }}
    .news-summary {{
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      font-size: 12px;
      line-height: 1.35;
      color: #c7dce0;
    }}
    body[data-theme="light"] .news-summary,
    body[data-theme="light"] .research-section p,
    body[data-theme="light"] .index-leader-item em {{
      color: #48616f;
    }}
    .news-time {{
      display: block;
      margin-top: 3px;
      font-size: 10px;
      color: var(--muted);
    }}
    .modal {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(18, 24, 30, 0.42);
      backdrop-filter: blur(6px);
      z-index: 30;
    }}
    .modal.open {{
      display: flex;
    }}
    .modal-card {{
      width: min(1240px, calc(100vw - 24px));
      max-height: min(94vh, 980px);
      overflow: hidden;
      background: var(--paper-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-panel);
      box-shadow: var(--panel-glow);
      padding: 10px 10px 8px;
      clip-path: polygon(0 0, calc(100% - var(--cut-size)) 0, 100% var(--cut-size), 100% 100%, var(--cut-size) 100%, 0 calc(100% - var(--cut-size)));
    }}
    .modal-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 2px 4px 6px;
    }}
    .modal-head h3 {{
      margin: 0;
      font-size: 15px;
    }}
    .modal-head p {{
      margin: 2px 0 0;
      font-size: 10px;
      color: var(--muted);
    }}
    .modal-close {{
      border: none;
      background: rgba(83,242,229,0.08);
      color: var(--ink);
      width: 34px;
      height: 34px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }}
    .chart {{
      height: min(44vh, 360px);
      margin-top: 2px;
    }}
    .chart-side {{
      display: grid;
      grid-template-rows: auto auto;
      gap: 8px;
      min-width: 0;
    }}
    .chart-insights {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .chart-insights .research-section {{
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 8px;
      background: rgba(9,18,27,0.92);
      padding: 10px;
      margin-top: 0;
      clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px));
    }}
    .chart-insights .research-section.full-span {{
      grid-column: 1 / -1;
    }}
    .modal-body {{
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(360px, 0.95fr);
      gap: 10px;
      align-items: start;
    }}
    .research-panel {{
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 8px;
      background: rgba(9,18,27,0.92);
      padding: 10px;
      clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 10px 100%, 0 calc(100% - 10px));
    }}
    .research-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      margin-bottom: 8px;
    }}
    .research-metric {{
      border: 1px solid rgba(83,242,229,0.10);
      border-radius: 6px;
      background: rgba(7,15,23,0.94);
      padding: 7px 8px;
      clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 8px 100%, 0 calc(100% - 8px));
    }}
    .research-metric label {{
      display: block;
      font-size: 10px;
      color: var(--muted);
      margin-bottom: 3px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .research-metric strong {{
      font-size: 12px;
      line-height: 1.3;
    }}
    .research-panel .research-section + .research-section {{
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed rgba(31,42,55,0.12);
    }}
    .research-section h4 {{
      margin: 0 0 4px;
      font-size: 11px;
      letter-spacing: 0.02em;
    }}
    .research-section p {{
      margin: 0;
      font-size: 11px;
      line-height: 1.45;
      color: #c6dce1;
      white-space: pre-wrap;
    }}
    @media (max-width: 860px) {{
      .app-shell {{
        grid-template-columns: 1fr;
        width: min(100vw, calc(100vw - 12px));
        padding-top: 12px;
      }}
      .sidebar {{
        position: static;
        min-height: 0;
      }}
      .sidebar-nav {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .market-index-grid,
      .market-stat-grid,
      .index-summary-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .market-card-content {{
        grid-template-columns: minmax(0, 1fr) 96px;
      }}
      .market-card-chart,
      .market-mini-kline {{
        width: 96px;
      }}
      .report-form-grid {{
        grid-template-columns: 1fr;
      }}
      .modal {{
        padding: 12px;
      }}
      .modal-card {{
        width: calc(100vw - 12px);
      }}
      .modal-body {{
        grid-template-columns: 1fr;
      }}
      .research-grid {{
        grid-template-columns: 1fr;
      }}
      .chart {{
        height: 28vh;
      }}
    }}
    .summary-table-wrap,
    .panel,
    .market-card,
    .market-stat,
    .report-entry-panel,
    .removed-item,
    .index-summary-item,
    .index-leader-item,
    .research-panel,
    .chart-insights .research-section,
    .research-metric,
    .index-modal-card,
    .modal-card,
    .report-detail-box {{
      background: var(--paper) !important;
    }}
    .summary-table thead,
    .summary-table tbody,
    .summary-table tr,
    .summary-table td,
    .summary-table th {{
      background-color: transparent;
    }}
    :root {{
      --bg: #ffffff;
      --paper: #f9f9fa;
      --paper-2: #f9f9fa;
      --ink: #1f1f1f;
      --muted: #8a8a8a;
      --line: #ececef;
      --line-strong: #dddddf;
      --accent: #1f1f1f;
      --accent-soft: rgba(0, 0, 0, 0.04);
      --rise: #3a8f52;
      --fall: #d46a6a;
      --warn: #d7a94b;
      background: #ffffff !important;
      color: #1f1f1f;
    }}
    body[data-theme="dark"] {{
      --bg: #333333;
      --paper: #3b3b3b;
      --paper-2: #3b3b3b;
      --ink: #f2f2f2;
      --muted: #b7b7b7;
      --line: #4a4a4a;
      --line-strong: #5a5a5a;
      --accent: #f2f2f2;
      --accent-soft: rgba(255, 255, 255, 0.06);
      --rise: #7bd88f;
      --fall: #ff8f8f;
      --warn: #f0c674;
      background: #333333 !important;
      color: #f2f2f2;
    }}
    body {{
      background: #ffffff !important;
      color: #1f1f1f;
    }}
    body[data-theme="dark"] {{
      background: #333333 !important;
      color: #f2f2f2;
    }}
    .summary-table-wrap,
    .panel,
    .market-card,
    .market-stat,
    .report-entry-panel,
    .removed-item,
    .index-summary-item,
    .index-leader-item,
    .research-panel,
    .chart-insights .research-section,
    .research-metric,
    .index-modal-card,
    .modal-card,
    .report-detail-box,
    .sidebar,
    .hero,
    .search-bar,
    .report-input,
    .report-textarea,
    .notice-banner,
    .removed-panel {{
      background: #f9f9fa !important;
      color: #1f1f1f !important;
      border-color: #ececef !important;
      box-shadow: none !important;
    }}
    body[data-theme="dark"] .summary-table-wrap,
    body[data-theme="dark"] .panel,
    body[data-theme="dark"] .market-card,
    body[data-theme="dark"] .market-stat,
    body[data-theme="dark"] .report-entry-panel,
    body[data-theme="dark"] .removed-item,
    body[data-theme="dark"] .index-summary-item,
    body[data-theme="dark"] .index-leader-item,
    body[data-theme="dark"] .research-panel,
    body[data-theme="dark"] .chart-insights .research-section,
    body[data-theme="dark"] .research-metric,
    body[data-theme="dark"] .index-modal-card,
    body[data-theme="dark"] .modal-card,
    body[data-theme="dark"] .report-detail-box,
    body[data-theme="dark"] .sidebar,
    body[data-theme="dark"] .hero,
    body[data-theme="dark"] .search-bar,
    body[data-theme="dark"] .report-input,
    body[data-theme="dark"] .report-textarea,
    body[data-theme="dark"] .notice-banner,
    body[data-theme="dark"] .removed-panel {{
      background: #3b3b3b !important;
      color: #f2f2f2 !important;
      border-color: #4a4a4a !important;
    }}
    .summary-table thead,
    .summary-table tbody tr:hover,
    .report-row-main[data-open="true"] td,
    .meta span,
    .pill,
    .report-tag,
    .theme-toggle button.active {{
      background: #efeff1 !important;
      color: #1f1f1f !important;
      border-color: #dddddf !important;
    }}
    body[data-theme="dark"] .summary-table thead,
    body[data-theme="dark"] .summary-table tbody tr:hover,
    body[data-theme="dark"] .report-row-main[data-open="true"] td,
    body[data-theme="dark"] .meta span,
    body[data-theme="dark"] .pill,
    body[data-theme="dark"] .report-tag,
    body[data-theme="dark"] .theme-toggle button.active {{
      background: #454545 !important;
      color: #f2f2f2 !important;
      border-color: #555555 !important;
    }}
    .market-card-head strong,
    .market-card-price,
    .market-stat strong,
    .index-summary-item strong,
    .index-leader-item strong,
    .research-metric strong,
    .section-head h2,
    .market-overview-head h2,
    .hero h1,
    .modal-head h3,
    .index-modal-head h3,
    .stock-name {{
      color: #1f1f1f !important;
    }}
    body[data-theme="dark"] .market-card-head strong,
    body[data-theme="dark"] .market-card-price,
    body[data-theme="dark"] .market-stat strong,
    body[data-theme="dark"] .index-summary-item strong,
    body[data-theme="dark"] .index-leader-item strong,
    body[data-theme="dark"] .research-metric strong,
    body[data-theme="dark"] .section-head h2,
    body[data-theme="dark"] .market-overview-head h2,
    body[data-theme="dark"] .hero h1,
    body[data-theme="dark"] .modal-head h3,
    body[data-theme="dark"] .index-modal-head h3,
    body[data-theme="dark"] .stock-name {{
      color: #f2f2f2 !important;
    }}
    .summary-table th,
    .summary-table td,
    .stock-code,
    .market-card-head,
    .market-card-meta,
    .meta span,
    .pill,
    .report-action-note,
    .report-field label,
    .section-head p,
    .removed-head p,
    .notice-banner,
    .news-time {{
      color: #8a8a8a !important;
    }}
    body[data-theme="dark"] .summary-table th,
    body[data-theme="dark"] .summary-table td,
    body[data-theme="dark"] .stock-code,
    body[data-theme="dark"] .market-card-head,
    body[data-theme="dark"] .market-card-meta,
    body[data-theme="dark"] .meta span,
    body[data-theme="dark"] .pill,
    body[data-theme="dark"] .report-action-note,
    body[data-theme="dark"] .report-field label,
    body[data-theme="dark"] .section-head p,
    body[data-theme="dark"] .removed-head p,
    body[data-theme="dark"] .notice-banner,
    body[data-theme="dark"] .news-time {{
      color: #b7b7b7 !important;
    }}
    .search-input,
    .report-input,
    .report-textarea,
    .modal-close,
    .sidebar-link,
    .status-select,
    .join-watchlist-select {{
      color: #1f1f1f !important;
    }}
    body[data-theme="dark"] .search-input,
    body[data-theme="dark"] .report-input,
    body[data-theme="dark"] .report-textarea,
    body[data-theme="dark"] .modal-close,
    body[data-theme="dark"] .sidebar-link,
    body[data-theme="dark"] .status-select,
    body[data-theme="dark"] .join-watchlist-select {{
      color: #f2f2f2 !important;
    }}
    .search-input::placeholder,
    .report-textarea::placeholder {{
      color: #9f9f9f !important;
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <strong>Stock Killer-A Shares</strong>
      </div>
      <nav class="sidebar-nav">
        <button class="sidebar-link active" type="button" data-view-target="market-view">市场行情</button>
        <button class="sidebar-link" type="button" data-view-target="institution-view">机构持仓</button>
        <button class="sidebar-link" type="button" data-view-target="report-view">投研报告</button>
        <button class="sidebar-link" type="button" data-view-target="social-view">社交媒体</button>
      </nav>
    </aside>
    <main class="wrap">
    <section class="page-view active" id="market-view">
    <section class="hero">
      <div class="hero-actions">
        <div class="theme-toggle" aria-label="主题切换">
          <button id="theme-dark-button" type="button" data-theme-value="dark" aria-label="深色模式">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
          </button>
          <button id="theme-light-button" type="button" data-theme-value="light" aria-label="浅色模式">
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M21.5 12h-2.2M4.7 12H2.5M18.72 5.28l-1.56 1.56M6.84 17.16l-1.56 1.56M18.72 18.72l-1.56-1.56M6.84 6.84 5.28 5.28"/></svg>
          </button>
        </div>
      </div>
      <div class="search-bar">
        <span>搜索股票</span>
        <input id="stock-search" class="search-input" type="text" placeholder="输入股票名称或代码，例如 美利云 / 000815" />
      </div>
      <div class="meta">
        <span>更新日期：{AS_OF.isoformat()}</span>
        <span>行情数据截至：{latest_market_date or '-'}</span>
        <span>行情区间：近30个交易日</span>
        <span>数据源：{data_source_label}</span>
      </div>
      {stale_notice_html}
    </section>
    <section class="market-overview panel">
      <div class="market-overview-head">
        <div>
          <h2>整体市场指数</h2>
          <p>仅保留创业板指、沪深300、科创50</p>
        </div>
        <span class="pill">统计日：{overview_trade_date}</span>
      </div>
      <div class="market-index-grid">
        {index_cards_html}
      </div>
    </section>
    <div class="section-head">
      <div>
        <h2>当日强势股</h2>
      </div>
      <span class="pill">共 {len(strong_stocks)} 只</span>
    </div>
    <section class="summary-table-wrap">
      <table class="summary-table">
        <thead>
          {strong_head_html}
        </thead>
        <tbody id="strong-stocks-table-body" class="strong-stocks-table-body">
          {strong_grouped_rows_html}
        </tbody>
      </table>
    </section>
    <div class="section-head">
      <div>
        <h2>自选跟踪</h2>
        <p>点击股票名称可查看近 30 日 K 线、成交额和研究摘要</p>
      </div>
    </div>
    <section class="summary-table-wrap">
      <table class="summary-table">
        <thead>
          {watchlist_head_html}
        </thead>
        <tbody id="watchlist-table-body" class="watchlist-table-body">
          {watchlist_grouped_rows_html}
        </tbody>
      </table>
    </section>
    <section class="removed-panel" id="removed-panel" hidden>
      <div class="removed-head">
        <div>
          <h3>已移除</h3>
          <p>这里保留被隐藏的股票，方便随时恢复到自选跟踪</p>
        </div>
      </div>
      <div class="removed-list" id="removed-list"></div>
    </section>
    </section>
    <section class="page-view" id="institution-view" hidden>
      <section class="hero">
        <h1>机构持仓</h1>
        <div class="meta">
          <span>更新日期：{AS_OF.isoformat()}</span>
          <span>基金规模披露期：{((institution_rows_data[0].get('scaleReportDate') if institution_rows_data else '') or '-')}</span>
          <span>数据源：{institution_holdings.get('source') or '公开数据源'}</span>
        </div>
        <div class="notice-banner institution-note">
          iFinD 基金能力验证：
          {"已确认支持 .OF 基金行情历史" if institution_probe.get("supportsFundQuotes") else "当前未确认 .OF 基金行情历史"}
          。机构页当前使用天天基金公开披露口径，可展示近1年/6月/3月/1月收益、基金规模环比、基金经理在管规模、机构持有占比和前十大重仓股。
        </div>
      </section>
      <div class="section-head">
        <div>
          <h2>重点基金持仓</h2>
          <p>{institution_probe.get('notes') or '基金规模、阶段收益、管理信息和前十大重仓股按最近公开披露整理'}</p>
        </div>
        <span class="pill">共 {len(institution_rows_data)} 只</span>
      </div>
      <section class="summary-table-wrap">
        <table class="summary-table institution-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>基金名称</th>
              <th>基金体量</th>
              <th>阶段收益</th>
              <th>管理信息</th>
              <th>前10大重仓股</th>
            </tr>
          </thead>
          <tbody id="institution-table-body">
            {''.join(institution_rows_html)}
          </tbody>
        </table>
      </section>
    </section>
    <section class="page-view" id="report-view" hidden>
      <section class="hero">
        <h1>投研报告</h1>
        <div class="meta">
          <span>更新日期：{AS_OF.isoformat()}</span>
          <span>默认日期：{AS_OF.isoformat()}</span>
          <span>处理方式：GPT提取后结构化保存</span>
        </div>
        <div class="notice-banner institution-note">
          在这里粘贴投研原文、纪要或观点摘要。系统会自动提取核心观点、行业细分方向、日期，以及涉及的具体标的，并按结构化表格保存。
        </div>
      </section>
      <section class="report-entry-panel">
        <div class="report-form-grid">
          <div class="report-field">
            <label for="report-date-input">日期</label>
            <input id="report-date-input" class="report-input" type="date" value="{AS_OF.isoformat()}" />
          </div>
          <div class="report-field full-span">
            <label for="report-content-input">原文输入</label>
            <textarea id="report-content-input" class="report-textarea" placeholder="粘贴投研报告、会议纪要、群聊观点或自己整理的文字内容，系统会自动提取标的和关键信息"></textarea>
          </div>
        </div>
        <div class="report-actions">
          <span class="report-action-note" id="report-action-note">提取完成后按日期倒序展示，并同步到云端状态。</span>
          <button id="report-save-button" class="report-save-button" type="button">GPT提取并保存</button>
        </div>
      </section>
      <div class="section-head">
        <div>
          <h2>历史研究</h2>
          <p>系统会按核心观点、行业和涉及标的结构化展示</p>
        </div>
        <div class="report-head-actions">
          <label class="pill report-hidden-toggle">
            <input id="report-hidden-toggle" type="checkbox" />
            <span>显示隐藏项</span>
          </label>
          <span class="pill" id="report-count-pill">共 0 条</span>
        </div>
      </div>
      <section class="summary-table-wrap" id="report-table-section">
        <table class="summary-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>行业</th>
              <th>核心观点</th>
              <th>日期</th>
              <th>隐藏</th>
            </tr>
          </thead>
          <tbody id="report-table-body"></tbody>
        </table>
        <div class="report-empty" id="report-empty-state">当前还没有录入任何投研内容。</div>
      </section>
      <div class="report-target-hover-card" id="report-target-hover-card" hidden></div>
      <div class="report-drawer" id="report-detail-drawer" aria-hidden="true">
        <aside class="report-drawer-panel" role="dialog" aria-modal="true" aria-labelledby="report-detail-title">
          <div class="report-drawer-head">
            <div>
              <h3 id="report-detail-title">投研详情</h3>
              <p id="report-detail-subtitle">点击历史研究行查看原文</p>
            </div>
            <button class="modal-close" id="report-detail-close" type="button" aria-label="关闭">×</button>
          </div>
          <div class="report-drawer-body">
            <div class="report-detail-meta" id="report-detail-meta"></div>
            <div class="report-detail-section">
              <h4>涉及标的</h4>
              <div id="report-detail-targets"></div>
            </div>
            <div class="report-detail-section">
              <h4>核心观点</h4>
              <p id="report-detail-summary">暂无</p>
            </div>
            <div class="report-detail-section">
              <h4>输入原文</h4>
              <pre class="report-detail-source" id="report-detail-source">暂无原文</pre>
            </div>
          </div>
        </aside>
      </div>
    </section>
    <section class="page-view" id="social-view" hidden>
      <section class="hero">
        <h1>社交媒体</h1>
        <div class="meta">
          <span>更新日期：{AS_OF.isoformat()}</span>
          <span>自动频率：每日云端刷新</span>
          <span>处理方式：Grok 自动追踪与总结</span>
        </div>
        <div class="notice-banner institution-note">
          在这里维护你想追踪的 KOL 列表。云端每日刷新时会尝试按 KOL handle 自动总结近24小时观点，并提取涉及标的与行业方向。
        </div>
      </section>
      <section class="report-entry-panel">
        <div class="report-form-grid">
          <div class="report-field full-span">
            <label for="social-tweet-url-input">X 推文链接</label>
            <input id="social-tweet-url-input" class="report-input" type="url" placeholder="粘贴 x.com 或 twitter.com 的单条推文链接" />
          </div>
        </div>
        <div class="report-actions">
          <span class="report-action-note" id="social-tweet-action-note">系统会抓取推文原文，翻译成中文并生成摘要后写入下方表格。</span>
          <button id="social-tweet-save-button" class="report-save-button" type="button">总结并记录</button>
        </div>
      </section>
      <section class="report-entry-panel">
        <div class="report-form-grid">
          <div class="report-field">
            <label for="social-kol-name-input">KOL 名称</label>
            <input id="social-kol-name-input" class="report-input" type="text" placeholder="例如：某头部KOL" />
          </div>
          <div class="report-field">
            <label for="social-kol-handle-input">KOL ID / @handle</label>
            <input id="social-kol-handle-input" class="report-input" type="text" placeholder="例如：laoyao 或 @laoyao" />
          </div>
          <div class="report-field">
            <label for="social-kol-platform-input">平台</label>
            <input id="social-kol-platform-input" class="report-input" type="text" value="X" />
          </div>
        </div>
        <div class="report-actions">
          <span class="report-action-note" id="social-action-note">保存后会同步到云端，后续每天刷新时自动尝试抓取并总结该 KOL 近24小时观点。</span>
          <button id="social-save-button" class="report-save-button" type="button">添加追踪KOL</button>
        </div>
      </section>
      <div class="section-head">
        <div>
          <h2>追踪列表</h2>
          <p>维护你希望云端每天自动总结的 KOL</p>
        </div>
        <span class="pill" id="social-tracker-count-pill">共 0 个</span>
      </div>
      <section class="summary-table-wrap">
        <table class="summary-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>KOL</th>
              <th>Handle</th>
              <th>平台</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody id="social-tracker-table-body"></tbody>
        </table>
        <div class="report-empty" id="social-tracker-empty-state">当前还没有追踪任何 KOL。</div>
      </section>
      <div class="section-head">
        <div>
          <h2>KOL 每日摘要</h2>
          <p>云端按日期倒序展示每日自动总结结果</p>
        </div>
        <span class="pill" id="social-count-pill">共 0 条</span>
      </div>
      <section class="summary-table-wrap">
        <table class="summary-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>日期</th>
              <th>KOL</th>
              <th>链接</th>
              <th>行业</th>
              <th>核心观点</th>
              <th>中文翻译</th>
            </tr>
          </thead>
          <tbody id="social-table-body"></tbody>
        </table>
        <div class="report-empty" id="social-empty-state">当前还没有自动生成的 KOL 摘要。</div>
      </section>
    </section>
    <div class="modal" id="stock-modal" aria-hidden="true">
      <div class="modal-card">
        <div class="modal-head">
          <div>
            <h3 id="modal-title">近30日价格K线与成交量</h3>
            <p id="modal-subtitle">点击表格中的股票名称查看</p>
          </div>
          <button class="modal-close" id="modal-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="modal-body">
          <div class="chart-side">
            <div id="modal-chart" class="chart"></div>
            <div class="chart-insights">
              <div class="research-section full-span">
                <h4 id="research-margin-title">融资融券</h4>
                <div class="research-grid">
                  <div class="research-metric">
                    <label>融资余额</label>
                    <strong id="research-fin-balance">-</strong>
                  </div>
                  <div class="research-metric">
                    <label>融券余额</label>
                    <strong id="research-loan-balance">-</strong>
                  </div>
                  <div class="research-metric">
                    <label>融资买入额</label>
                    <strong id="research-fin-buy-amount">-</strong>
                  </div>
                </div>
              </div>
              <div class="research-section">
                <h4>前三大细分业务</h4>
                <p id="research-business-segments">暂无</p>
              </div>
              <div class="research-section">
                <h4>前五大客户</h4>
                <p id="research-top-customers">暂无</p>
              </div>
            </div>
          </div>
          <aside class="research-panel">
            <div class="research-grid">
              <div class="research-metric">
                <label>2025营收</label>
                <strong id="research-revenue">-</strong>
              </div>
              <div class="research-metric">
                <label>2025同比</label>
                <strong id="research-yoy">-</strong>
              </div>
              <div class="research-metric">
                <label>2026Q1营收</label>
                <strong id="research-q1-revenue">-</strong>
              </div>
              <div class="research-metric">
                <label>2026Q1营收同比</label>
                <strong id="research-q1-revenue-yoy">-</strong>
              </div>
              <div class="research-metric">
                <label>2026Q1净利润</label>
                <strong id="research-q1-net-profit">-</strong>
              </div>
              <div class="research-metric">
                <label>2026Q1净利润同比</label>
                <strong id="research-q1-net-profit-yoy">-</strong>
              </div>
              <div class="research-metric">
                <label>2026新订单/新增项目</label>
                <strong id="research-orders">-</strong>
              </div>
              <div class="research-metric">
                <label>动态PE</label>
                <strong id="research-pe">-</strong>
              </div>
            </div>
            <div class="research-section">
              <h4>最新新闻</h4>
              <p id="research-latest-news">暂无</p>
            </div>
            <div class="research-section">
              <h4>核心逻辑</h4>
              <p id="research-logic">暂无</p>
            </div>
            <div class="research-section">
              <h4>核心用户及订单</h4>
              <p id="research-users">暂无</p>
            </div>
            <div class="research-section">
              <h4>前五大股东</h4>
              <p id="research-top-holders">暂无</p>
            </div>
            <div class="research-section">
              <h4>核心竞争力</h4>
              <p id="research-edge">暂无</p>
            </div>
            <div class="research-section">
              <h4>备注</h4>
              <p id="research-notes">暂无</p>
            </div>
          </aside>
        </div>
      </div>
    </div>
    <div class="index-modal" id="index-modal" aria-hidden="true">
      <div class="index-modal-card">
        <div class="index-modal-head">
          <div>
            <h3 id="index-modal-title">指数详情</h3>
            <p id="index-modal-subtitle">查看代表性权重股参考</p>
          </div>
          <button class="modal-close" id="index-modal-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="index-summary-grid">
          <div class="index-summary-item">
            <span>最新点位</span>
            <strong id="index-modal-close-value">-</strong>
          </div>
          <div class="index-summary-item">
            <span>当日涨跌幅</span>
            <strong id="index-modal-pct">-</strong>
          </div>
          <div class="index-summary-item">
            <span>当日成交额</span>
            <strong id="index-modal-amount">-</strong>
          </div>
        </div>
        <div class="index-leader-list" id="index-leader-list"></div>
      </div>
    </div>
  </main>
  </div>
  <script>
    const dataset = {json.dumps(dataset, ensure_ascii=False)};
    const strongStocks = {json.dumps(strong_stocks, ensure_ascii=False)};
    const reportModalExtras = {json.dumps(report_modal_extras, ensure_ascii=False)};
    const marketOverview = {json.dumps(client_market_overview, ensure_ascii=False)};
    const institutionHoldings = {json.dumps(institution_holdings, ensure_ascii=False)};
    const datasetCodeSet = new Set(dataset.map(item => item.code));
    const momentumExtras = strongStocks.filter((item, idx, list) => {{
      if (datasetCodeSet.has(item.code)) return false;
      return list.findIndex(other => other.code === item.code) === idx;
    }});
    const modalSeenCodes = new Set(dataset.map(item => item.code));
    momentumExtras.forEach(item => modalSeenCodes.add(item.code));
    const modalItems = dataset
      .concat(momentumExtras)
      .concat(reportModalExtras.filter(item => !modalSeenCodes.has(item.code)));
    const modalIndexByCode = Object.fromEntries(modalItems.map((item, idx) => [item.code, idx]));
    const modalCodeByAlias = {{}};
    function normalizeModalAlias(value) {{
      return String(value || '').trim().toLowerCase().replace(/\\s+/g, '');
    }}
    modalItems.forEach(item => {{
      const aliases = [item.name, item.code, (item.code || '').split('.')[0]];
      aliases.forEach(alias => {{
        const normalized = normalizeModalAlias(alias);
        if (normalized && !modalCodeByAlias[normalized]) {{
          modalCodeByAlias[normalized] = item.code;
        }}
      }});
    }});
    const indexCards = marketOverview.indices || [];
    const indexCardByCode = Object.fromEntries(indexCards.map(item => [item.code, item]));

    function buildChartOption(item) {{
      const category = item.kline.map(row => row[0]);
      const candleData = item.kline.map(row => [row[1], row[2], row[3], row[4]]);
      const volumeData = item.kline.map(row => row[5]);
      const colors = volumeData.map((_, idx) => {{
        if (idx === 0) return '#0f766e';
        const prev = candleData[idx - 1][1];
        const curr = candleData[idx][1];
        return curr >= prev ? '#d64545' : '#1f8f67';
      }});

      return {{
        animation: false,
        backgroundColor: 'transparent',
        color: ['#d64545', '#1f8f67', '#0f766e'],
        tooltip: {{
          trigger: 'axis',
          axisPointer: {{ type: 'cross' }},
          backgroundColor: 'rgba(29, 28, 26, 0.92)',
          borderWidth: 0,
          textStyle: {{ color: '#fff' }}
        }},
        grid: [
          {{ left: 56, right: 20, top: 30, height: 200 }},
          {{ left: 56, right: 20, top: 260, height: 70 }}
        ],
        xAxis: [
          {{
            type: 'category',
            data: category,
            boundaryGap: true,
            axisLine: {{ lineStyle: {{ color: 'rgba(31,42,55,0.18)' }} }},
            axisLabel: {{ color: '#6b7280', fontSize: 11 }},
            min: 'dataMin',
            max: 'dataMax'
          }},
          {{
            type: 'category',
            gridIndex: 1,
            data: category,
            boundaryGap: true,
            axisLine: {{ lineStyle: {{ color: 'rgba(31,42,55,0.18)' }} }},
            axisLabel: {{ show: false }},
            axisTick: {{ show: false }},
            min: 'dataMin',
            max: 'dataMax'
          }}
        ],
        yAxis: [
          {{
            scale: true,
            splitLine: {{ lineStyle: {{ color: 'rgba(31,42,55,0.08)' }} }},
            axisLabel: {{ color: '#6b7280' }}
          }},
          {{
            scale: true,
            gridIndex: 1,
            splitNumber: 2,
            splitLine: {{ show: false }},
            axisLabel: {{
              color: '#6b7280',
              formatter: value => (value / 10000).toFixed(0) + '万'
            }}
          }}
        ],
        series: [
          {{
            name: item.name + ' K线',
            type: 'candlestick',
            data: candleData,
            itemStyle: {{
              color: '#d64545',
              color0: '#1f8f67',
              borderColor: '#d64545',
              borderColor0: '#1f8f67'
            }}
          }},
          {{
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: volumeData.map((value, idx) => {{
              return {{
                value,
                itemStyle: {{ color: colors[idx] }}
              }};
            }})
          }}
        ]
      }};
    }}

    const modal = document.getElementById('stock-modal');
    const modalClose = document.getElementById('modal-close');
    const modalTitle = document.getElementById('modal-title');
    const modalSubtitle = document.getElementById('modal-subtitle');
    const modalChartNode = document.getElementById('modal-chart');
    const stockSearch = document.getElementById('stock-search');
    const reportDateInput = document.getElementById('report-date-input');
    const reportContentInput = document.getElementById('report-content-input');
    const reportSaveButton = document.getElementById('report-save-button');
    const reportTableBody = document.getElementById('report-table-body');
    const reportTableSection = document.getElementById('report-table-section');
    const reportHiddenToggle = document.getElementById('report-hidden-toggle');
    const reportEmptyState = document.getElementById('report-empty-state');
    const reportCountPill = document.getElementById('report-count-pill');
    const reportTargetHoverCard = document.getElementById('report-target-hover-card');
    const reportDetailDrawer = document.getElementById('report-detail-drawer');
    const reportDetailClose = document.getElementById('report-detail-close');
    const reportDetailTitle = document.getElementById('report-detail-title');
    const reportDetailSubtitle = document.getElementById('report-detail-subtitle');
    const reportDetailMeta = document.getElementById('report-detail-meta');
    const reportDetailTargets = document.getElementById('report-detail-targets');
    const reportDetailSummary = document.getElementById('report-detail-summary');
    const reportDetailSource = document.getElementById('report-detail-source');
    const reportActionNote = document.getElementById('report-action-note');
    const socialKolNameInput = document.getElementById('social-kol-name-input');
    const socialKolHandleInput = document.getElementById('social-kol-handle-input');
    const socialKolPlatformInput = document.getElementById('social-kol-platform-input');
    const socialSaveButton = document.getElementById('social-save-button');
    const socialTweetUrlInput = document.getElementById('social-tweet-url-input');
    const socialTweetSaveButton = document.getElementById('social-tweet-save-button');
    const socialTweetActionNote = document.getElementById('social-tweet-action-note');
    const socialTrackerTableBody = document.getElementById('social-tracker-table-body');
    const socialTrackerEmptyState = document.getElementById('social-tracker-empty-state');
    const socialTrackerCountPill = document.getElementById('social-tracker-count-pill');
    const socialTableBody = document.getElementById('social-table-body');
    const socialEmptyState = document.getElementById('social-empty-state');
    const socialCountPill = document.getElementById('social-count-pill');
    const socialActionNote = document.getElementById('social-action-note');
    const strongStocksTableBody = document.getElementById('strong-stocks-table-body');
    const watchlistTableBody = document.getElementById('watchlist-table-body');
    const removedPanel = document.getElementById('removed-panel');
    const removedList = document.getElementById('removed-list');
    const researchRevenue = document.getElementById('research-revenue');
    const researchYoy = document.getElementById('research-yoy');
    const researchQ1Revenue = document.getElementById('research-q1-revenue');
    const researchQ1RevenueYoY = document.getElementById('research-q1-revenue-yoy');
    const researchQ1NetProfit = document.getElementById('research-q1-net-profit');
    const researchQ1NetProfitYoY = document.getElementById('research-q1-net-profit-yoy');
    const researchOrders = document.getElementById('research-orders');
    const researchPe = document.getElementById('research-pe');
    const researchFinBalance = document.getElementById('research-fin-balance');
    const researchLoanBalance = document.getElementById('research-loan-balance');
    const researchFinBuyAmount = document.getElementById('research-fin-buy-amount');
    const researchMarginTitle = document.getElementById('research-margin-title');
    const researchLatestNews = document.getElementById('research-latest-news');
    const researchLogic = document.getElementById('research-logic');
    const researchUsers = document.getElementById('research-users');
    const researchEdge = document.getElementById('research-edge');
    const researchTopHolders = document.getElementById('research-top-holders');
    const researchBusinessSegments = document.getElementById('research-business-segments');
    const researchTopCustomers = document.getElementById('research-top-customers');
    const researchNotes = document.getElementById('research-notes');
    const indexModal = document.getElementById('index-modal');
    const indexModalClose = document.getElementById('index-modal-close');
    const indexModalTitle = document.getElementById('index-modal-title');
    const indexModalSubtitle = document.getElementById('index-modal-subtitle');
    const indexModalCloseValue = document.getElementById('index-modal-close-value');
    const indexModalPct = document.getElementById('index-modal-pct');
    const indexModalAmount = document.getElementById('index-modal-amount');
    const indexLeaderList = document.getElementById('index-leader-list');
    const sidebarLinks = [...document.querySelectorAll('.sidebar-link[data-view-target]')];
    const pageViews = [...document.querySelectorAll('.page-view[id]')];
    const themeButtons = [...document.querySelectorAll('[data-theme-value]')];
    const modalChart = echarts.init(modalChartNode, null, {{ renderer: 'canvas' }});
    const DASHBOARD_VIEW_KEY = 'astock_dashboard_view_v1';
    const DASHBOARD_THEME_KEY = 'astock_dashboard_theme_v2';
    const DASHBOARD_STATE_API_KEY = 'astock_dashboard_state_api_url_v1';
    const DASHBOARD_ADMIN_TOKEN_KEY = 'astock_dashboard_admin_token_v1';
    const DASHBOARD_ANALYZE_API_KEY = 'astock_dashboard_analyze_api_url_v1';
    const REPORT_HIDDEN_IDS_KEY = 'astock_report_hidden_ids_v1';
    const INITIAL_WATCHLIST_STATUS = {json.dumps(initial_watchlist_status, ensure_ascii=False)};
    const INITIAL_STRONG_JOIN_STATUS = {json.dumps(initial_strong_join_status, ensure_ascii=False)};
    const INITIAL_REPORT_ENTRIES = {json.dumps(initial_report_entries, ensure_ascii=False)};
    const INITIAL_SOCIAL_TRACKERS = {json.dumps(initial_social_trackers, ensure_ascii=False)};
    const INITIAL_SOCIAL_ENTRIES = {json.dumps(initial_social_entries, ensure_ascii=False)};
    const DEFAULT_DASHBOARD_STATE_API = (() => {{
      const host = window.location.hostname;
      if (host === 'stockkiller.xyz' || host === 'www.stockkiller.xyz') {{
        return 'https://api.stockkiller.xyz/api/dashboard-state';
      }}
      if (window.location.protocol === 'file:') {{
        return 'https://api.stockkiller.xyz/api/dashboard-state';
      }}
      return '';
    }})();
    const DEFAULT_REPORT_ANALYZE_API = DEFAULT_DASHBOARD_STATE_API
      ? DEFAULT_DASHBOARD_STATE_API.replace('/dashboard-state', '/report-analyze')
      : '';
    const DEFAULT_SOCIAL_ANALYZE_API = DEFAULT_DASHBOARD_STATE_API
      ? DEFAULT_DASHBOARD_STATE_API.replace('/dashboard-state', '/social-analyze')
      : '';
    let dashboardStateSyncPromise = Promise.resolve();
    let dashboardStateBootstrapped = false;
    let showHiddenReports = false;

    function normalizeTargetLabel(value) {{
      if (value == null) return '';
      if (typeof value === 'string' || typeof value === 'number') {{
        const text = String(value).trim();
        return text === '[object Object]' ? '' : text;
      }}
      if (typeof value !== 'object') return '';
      const candidates = [
        value.name,
        value.stockName,
        value.shortName,
        value.company,
        value.target,
        value.label,
        value.title,
        value.code,
        value.symbol,
      ];
      for (const candidate of candidates) {{
        const text = normalizeTargetLabel(candidate);
        if (text) return text;
      }}
      for (const candidate of Object.values(value)) {{
        const text = normalizeTargetLabel(candidate);
        if (text) return text;
      }}
      return '';
    }}

    function normalizeTargetList(values, limit = 12) {{
      if (!Array.isArray(values)) return [];
      return values.map(normalizeTargetLabel).filter(Boolean).slice(0, limit);
    }}

    function normalizeReportEntry(entry, fallbackId) {{
      if (!entry || typeof entry !== 'object') return null;
      const content = String(entry.content || entry.summary || '').trim();
      const targets = normalizeTargetList(entry.targets);
      const target = normalizeTargetLabel(entry.target) || targets[0] || '';
      if (!content) return null;
      const date = String(entry.date || '');
      const createdAt = Number(entry.createdAt || Date.now());
      const id = String(entry.id || fallbackId || `${{target}}-${{date}}-${{createdAt}}`);
      return {{
        id,
        target,
        targets: targets.length ? targets : (target ? [target] : []),
        content,
        summary: String(entry.summary || content).trim(),
        industry: String(entry.industry || '未分类').trim(),
        rawText: String(entry.rawText || entry.content || '').trim(),
        date,
        createdAt,
      }};
    }}

    function mergeReportEntries(baseEntries, overlayEntries) {{
      const merged = new Map();
      [...baseEntries, ...overlayEntries].forEach((entry, idx) => {{
        const normalized = normalizeReportEntry(entry, `report-${{idx + 1}}`);
        if (!normalized) return;
        const existing = merged.get(normalized.id);
        if (
          existing &&
          (!normalized.industry || normalized.industry === '未分类') &&
          existing.industry &&
          existing.industry !== '未分类'
        ) {{
          normalized.industry = existing.industry;
        }}
        merged.set(normalized.id, normalized);
      }});
      return [...merged.values()];
    }}

    function loadHiddenReportIds() {{
      try {{
        const parsed = JSON.parse(window.localStorage.getItem(REPORT_HIDDEN_IDS_KEY) || '[]');
        return new Set(Array.isArray(parsed) ? parsed.map(id => String(id)) : []);
      }} catch (error) {{
        return new Set();
      }}
    }}

    function saveHiddenReportIds(ids) {{
      window.localStorage.setItem(REPORT_HIDDEN_IDS_KEY, JSON.stringify([...ids]));
    }}

    function setReportEntryHidden(id, hidden) {{
      const ids = loadHiddenReportIds();
      if (hidden) {{
        ids.add(String(id));
      }} else {{
        ids.delete(String(id));
      }}
      saveHiddenReportIds(ids);
    }}

    function normalizeSocialEntry(entry, fallbackId) {{
      if (!entry || typeof entry !== 'object') return null;
      const kol = String(entry.kol || entry.source || '').trim();
      const content = String(entry.content || entry.summary || '').trim();
      const targets = normalizeTargetList(entry.targets);
      const target = normalizeTargetLabel(entry.target) || targets[0] || '';
      if (!kol || !content) return null;
      const date = String(entry.date || '');
      const createdAt = Number(entry.createdAt || Date.now());
      const id = String(entry.id || fallbackId || `${{kol}}-${{date}}-${{createdAt}}`);
      return {{
        id,
        kol,
        handle: String(entry.handle || '').trim(),
        platform: String(entry.platform || 'X').trim(),
        target,
        targets: targets.length ? targets : (target ? [target] : []),
        content,
        summary: String(entry.summary || content).trim(),
        industry: String(entry.industry || '').trim(),
        rawText: String(entry.rawText || entry.content || '').trim(),
        translatedText: String(entry.translatedText || entry.translation || '').trim(),
        sourceUrl: String(entry.sourceUrl || entry.tweetUrl || entry.url || '').trim(),
        tweetUrl: String(entry.tweetUrl || entry.sourceUrl || entry.url || '').trim(),
        sourceNote: String(entry.sourceNote || '').trim(),
        date,
        createdAt,
      }};
    }}

    function mergeSocialEntries(baseEntries, overlayEntries) {{
      const merged = new Map();
      [...baseEntries, ...overlayEntries].forEach((entry, idx) => {{
        const normalized = normalizeSocialEntry(entry, `social-${{idx + 1}}`);
        if (!normalized) return;
        merged.set(normalized.id, normalized);
      }});
      return [...merged.values()];
    }}

    function normalizeSocialTracker(entry, fallbackId) {{
      if (!entry || typeof entry !== 'object') return null;
      const handle = String(entry.handle || entry.id || '').trim().replace(/^@+/, '');
      if (!handle) return null;
      return {{
        id: String(entry.id || fallbackId || `kol-${{handle}}`),
        name: String(entry.name || handle).trim(),
        handle,
        platform: String(entry.platform || 'X').trim(),
        enabled: entry.enabled !== false,
        createdAt: Number(entry.createdAt || Date.now()),
      }};
    }}

    function mergeSocialTrackers(baseEntries, overlayEntries) {{
      const merged = new Map();
      [...baseEntries, ...overlayEntries].forEach((entry, idx) => {{
        const normalized = normalizeSocialTracker(entry, `tracker-${{idx + 1}}`);
        if (!normalized) return;
        merged.set(normalized.id, normalized);
      }});
      return [...merged.values()];
    }}

    function normalizeDashboardApiUrl(rawUrl, fallbackUrl) {{
      const fallback = String(fallbackUrl || '').trim();
      const candidate = String(rawUrl || '').trim() || fallback;
      if (!candidate) return '';
      try {{
        const url = new URL(candidate, window.location.href);
        if ((url.hostname === 'stockkiller.xyz' || url.hostname === 'www.stockkiller.xyz') && url.pathname.startsWith('/api/')) {{
          url.protocol = 'https:';
          url.hostname = 'api.stockkiller.xyz';
          return url.href;
        }}
        return url.href;
      }} catch (error) {{
        return fallback || candidate;
      }}
    }}

    function getStoredDashboardApiUrl(storageKey, fallbackUrl) {{
      const stored = window.localStorage.getItem(storageKey);
      const normalized = normalizeDashboardApiUrl(stored, fallbackUrl);
      if (stored && normalized && normalized !== String(stored).trim()) {{
        window.localStorage.setItem(storageKey, normalized);
      }}
      return normalized;
    }}

    function getDashboardStateApiUrl() {{
      return getStoredDashboardApiUrl(DASHBOARD_STATE_API_KEY, DEFAULT_DASHBOARD_STATE_API);
    }}

    function getReportAnalyzeApiUrl() {{
      return getStoredDashboardApiUrl(DASHBOARD_ANALYZE_API_KEY, DEFAULT_REPORT_ANALYZE_API);
    }}

    function getSocialAnalyzeApiUrl() {{
      return DEFAULT_SOCIAL_ANALYZE_API || getReportAnalyzeApiUrl().replace('/report-analyze', '/social-analyze');
    }}

    function getDashboardAdminToken() {{
      return window.localStorage.getItem(DASHBOARD_ADMIN_TOKEN_KEY) || '';
    }}

    function setDashboardAdminToken(token) {{
      const normalized = String(token || '').trim();
      if (normalized) {{
        window.localStorage.setItem(DASHBOARD_ADMIN_TOKEN_KEY, normalized);
      }} else {{
        window.localStorage.removeItem(DASHBOARD_ADMIN_TOKEN_KEY);
      }}
    }}

    function normalizeWatchlistStatusPayload(payload) {{
      const next = {{}};
      if (!payload || typeof payload !== 'object') return next;
      Object.entries(payload).forEach(([code, value]) => {{
        const normalizedCode = String(code || '').trim();
        const normalizedValue = String(value || '').trim();
        if (!normalizedCode) return;
        if (normalizedValue === 'removed') {{
          next[normalizedCode] = 'removed';
        }}
      }});
      return next;
    }}

    function normalizeStrongJoinPayload(payload) {{
      const next = {{}};
      if (!payload || typeof payload !== 'object') return next;
      Object.entries(payload).forEach(([code, value]) => {{
        const normalizedCode = String(code || '').trim();
        const normalizedValue = String(value || '').trim();
        if (!normalizedCode) return;
        if (normalizedValue === 'joined') {{
          next[normalizedCode] = 'joined';
        }}
      }});
      return next;
    }}

    function replaceObjectContents(target, source) {{
      Object.keys(target).forEach(key => delete target[key]);
      Object.entries(source).forEach(([key, value]) => {{
        target[key] = value;
      }});
    }}

    function applyTheme(theme) {{
      const nextTheme = theme === 'light' ? 'light' : 'dark';
      document.body.setAttribute('data-theme', nextTheme);
      window.localStorage.setItem(DASHBOARD_THEME_KEY, nextTheme);
      themeButtons.forEach(button => {{
        button.classList.toggle('active', button.dataset.themeValue === nextTheme);
      }});
    }}

    function setActiveView(viewId) {{
      pageViews.forEach(view => {{
        const active = view.id === viewId;
        view.hidden = !active;
        view.classList.toggle('active', active);
      }});
      sidebarLinks.forEach(link => {{
        link.classList.toggle('active', link.dataset.viewTarget === viewId);
      }});
      window.localStorage.setItem(DASHBOARD_VIEW_KEY, viewId);
      if (viewId === 'market-view') {{
        setTimeout(() => modalChart.resize(), 0);
      }}
    }}

    function openIndexModalByCode(code) {{
      const item = indexCardByCode[code];
      if (!item || !item.detail) return;
      indexModalTitle.textContent = item.detail.title || (item.name + ' 详情');
      indexModalSubtitle.textContent = (item.detail.note || '代表性权重股参考') + ' · ' + (item.date || '');
      indexModalCloseValue.textContent = item.latestClose == null ? '暂无' : Number(item.latestClose).toFixed(2);
      indexModalPct.textContent = item.pct == null ? '暂无' : ((Number(item.pct) >= 0 ? '+' : '') + Number(item.pct).toFixed(2) + '%');
      indexModalPct.className = 'pct-' + ((Number(item.pct) || 0) > 0 ? 'rise' : (Number(item.pct) || 0) < 0 ? 'fall' : 'flat');
      indexModalAmount.textContent = item.amount == null ? '暂无' : ((Number(item.amount) / 1e8).toFixed(2) + '亿');
      indexLeaderList.innerHTML = (item.detail.leaders || []).map((leader, idx) => `
        <article class="index-leader-item">
          <span>${{idx + 1}}. ${{leader.code || ''}}</span>
          <strong>${{leader.name || '-'}}</strong>
          <em>${{leader.tag || ''}}</em>
        </article>
      `).join('');
      indexModal.classList.add('open');
      indexModal.setAttribute('aria-hidden', 'false');
    }}

    function openStockModalByCode(code) {{
      const index = modalIndexByCode[code];
      if (index == null) return;
      const item = modalItems[index];
      if (!item.kline || !item.kline.length) return;
      modalTitle.textContent = item.name + ' 近30日价格K线与成交量';
      modalSubtitle.textContent = item.code + ' · 总市值/流通市值 ' + (item.totalMarketCap / 1e8).toFixed(2) + '亿 / ' + (item.floatMarketCap / 1e8).toFixed(2) + '亿 · 当日成交额 ' + (item.todayAmount / 1e8).toFixed(2) + '亿';
      modalChart.setOption(buildChartOption(item), true);
      researchRevenue.textContent = item.research.revenue2025 || '暂无';
      researchYoy.textContent = item.research.revenueYoY || '暂无';
      researchQ1Revenue.textContent = item.research.q1Revenue2026 || '暂无';
      researchQ1RevenueYoY.textContent = item.research.q1RevenueYoY2026 || '暂无';
      researchQ1NetProfit.textContent = item.research.q1NetProfit2026 || '暂无';
      researchQ1NetProfitYoY.textContent = item.research.q1NetProfitYoY2026 || '暂无';
      researchOrders.textContent = item.research.newOrders2026 || '暂无';
      researchPe.textContent = item.peRatio == null ? '暂无' : item.peRatio.toFixed(2);
      const marginFinancing = item.marginFinancing || {{}};
      researchMarginTitle.textContent = '融资融券' + (marginFinancing.date ? ' · ' + marginFinancing.date.slice(5) : '');
      researchFinBalance.textContent = marginFinancing.finBalance == null ? '暂无' : ((marginFinancing.finBalance / 1e8).toFixed(2) + '亿');
      researchLoanBalance.textContent = marginFinancing.loanBalance == null ? '暂无' : ((marginFinancing.loanBalance / 1e8).toFixed(2) + '亿');
      researchFinBuyAmount.textContent = marginFinancing.finBuyAmount == null ? '暂无' : ((marginFinancing.finBuyAmount / 1e8).toFixed(2) + '亿');
      const latestNews = item.latestNews || {{}};
      researchLatestNews.innerHTML = latestNews.summary
        ? `${{latestNews.time ? `<span class="news-time">${{escapeHtml(latestNews.time)}}</span><br />` : ''}}${{latestNews.link ? `<a class="news-link" href="${{escapeHtml(latestNews.link)}}" target="_blank" rel="noreferrer"><span class="news-summary">${{escapeHtml(latestNews.summary)}}</span></a>` : `<span class="news-summary">${{escapeHtml(latestNews.summary)}}</span>`}}`
        : '暂无';
      researchLogic.textContent = item.research.coreLogic || '暂无研究摘要';
      researchUsers.textContent = item.research.coreUsers || '暂无研究摘要';
      researchEdge.textContent = item.research.coreEdge || '暂无研究摘要';
      const topHolders = item.topHolders || {{}};
      const holderLines = topHolders.holders || [];
      const topHolderPrefix = [];
      if (topHolders.reportDate) topHolderPrefix.push('报告期：' + topHolders.reportDate);
      if (topHolders.totalRatio != null) topHolderPrefix.push('前五大合计：' + Number(topHolders.totalRatio).toFixed(2) + '%');
      researchTopHolders.textContent = holderLines.length
        ? (topHolderPrefix.length ? topHolderPrefix.join('\\n') + '\\n' : '') + holderLines.join('\\n')
        : '暂无';
      const businessSegments = item.businessSegments || {{}};
      const segmentLines = (businessSegments.items || []).map((row, idx) => {{
        const revenueText = row.revenue == null ? '暂无营收' : (Number(row.revenue) / 1e8).toFixed(2) + '亿元';
        const ratioText = row.ratio == null ? '' : ' / ' + (Number(row.ratio) * 100).toFixed(2) + '%';
        return `${{idx + 1}}. ${{row.name || '-'}}：${{revenueText}}${{ratioText}}`;
      }});
      const segmentPrefix = [];
      if (businessSegments.reportDate) segmentPrefix.push('报告期：' + businessSegments.reportDate);
      if (businessSegments.category) segmentPrefix.push('口径：' + businessSegments.category);
      researchBusinessSegments.textContent = segmentLines.length
        ? (segmentPrefix.length ? segmentPrefix.join('\\n') + '\\n' : '') + segmentLines.join('\\n')
        : '暂无';
      const topCustomers = item.topCustomers || {{}};
      const customerLines = (topCustomers.customers || []).map((row, idx) => {{
        const amountText = row.amount == null ? '暂无金额' : (Number(row.amount) / 1e8).toFixed(2) + '亿元';
        const ratioText = row.ratio == null ? '' : ' / ' + Number(row.ratio).toFixed(2) + '%';
        return `${{idx + 1}}. ${{row.name || '-'}}：${{amountText}}${{ratioText}}`;
      }});
      const customerPrefix = [];
      if (topCustomers.reportDate) customerPrefix.push('报告期：' + topCustomers.reportDate + '年报');
      if (topCustomers.totalAmount != null) customerPrefix.push('前五大合计：' + (Number(topCustomers.totalAmount) / 1e8).toFixed(2) + '亿元');
      if (topCustomers.totalRatio != null) customerPrefix.push('合计占比：' + Number(topCustomers.totalRatio).toFixed(2) + '%');
      researchTopCustomers.textContent = customerLines.length
        ? (customerPrefix.length ? customerPrefix.join('\\n') + '\\n' : '') + customerLines.join('\\n')
        : (customerPrefix.length ? customerPrefix.join('\\n') : '暂无');
      researchNotes.textContent = item.research.notes || '暂无';
      modal.classList.add('open');
      modal.setAttribute('aria-hidden', 'false');
      setTimeout(() => modalChart.resize(), 0);
    }}

    function closeStockModal() {{
      modal.classList.remove('open');
      modal.setAttribute('aria-hidden', 'true');
    }}

    function closeIndexModal() {{
      indexModal.classList.remove('open');
      indexModal.setAttribute('aria-hidden', 'true');
    }}

    const WATCHLIST_STORAGE_KEY = 'astock_watchlist_status_v1';
    const WATCHLIST_STRONG_JOIN_KEY = 'astock_strong_join_v1';
    const REPORT_ENTRIES_KEY = 'astock_report_entries_v1';
    const SOCIAL_TRACKERS_KEY = 'astock_social_trackers_v1';
    const SOCIAL_ENTRIES_KEY = 'astock_social_entries_v1';

    function loadWatchlistStatus() {{
      try {{
        const local = JSON.parse(window.localStorage.getItem(WATCHLIST_STORAGE_KEY) || '{{}}');
        return {{ ...INITIAL_WATCHLIST_STATUS, ...(local || {{}}) }};
      }} catch (error) {{
        return {{ ...INITIAL_WATCHLIST_STATUS }};
      }}
    }}

    function saveWatchlistStatus(statusMap) {{
      window.localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(statusMap));
    }}

    function loadStrongJoinStatus() {{
      try {{
        const local = JSON.parse(window.localStorage.getItem(WATCHLIST_STRONG_JOIN_KEY) || '{{}}');
        return {{ ...INITIAL_STRONG_JOIN_STATUS, ...(local || {{}}) }};
      }} catch (error) {{
        return {{ ...INITIAL_STRONG_JOIN_STATUS }};
      }}
    }}

    function saveStrongJoinStatus(statusMap) {{
      window.localStorage.setItem(WATCHLIST_STRONG_JOIN_KEY, JSON.stringify(statusMap));
    }}

    function loadReportEntries() {{
      try {{
        const raw = window.localStorage.getItem(REPORT_ENTRIES_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return mergeReportEntries(INITIAL_REPORT_ENTRIES, Array.isArray(parsed) ? parsed : []);
      }} catch (error) {{
        return [...INITIAL_REPORT_ENTRIES];
      }}
    }}

    function saveReportEntries(entries) {{
      window.localStorage.setItem(REPORT_ENTRIES_KEY, JSON.stringify(entries));
    }}

    function loadSocialEntries() {{
      try {{
        const raw = window.localStorage.getItem(SOCIAL_ENTRIES_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return mergeSocialEntries(INITIAL_SOCIAL_ENTRIES, Array.isArray(parsed) ? parsed : []);
      }} catch (error) {{
        return [...INITIAL_SOCIAL_ENTRIES];
      }}
    }}

    function saveSocialEntries(entries) {{
      window.localStorage.setItem(SOCIAL_ENTRIES_KEY, JSON.stringify(entries));
    }}

    function loadSocialTrackers() {{
      try {{
        const raw = window.localStorage.getItem(SOCIAL_TRACKERS_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return mergeSocialTrackers(INITIAL_SOCIAL_TRACKERS, Array.isArray(parsed) ? parsed : []);
      }} catch (error) {{
        return [...INITIAL_SOCIAL_TRACKERS];
      }}
    }}

    function saveSocialTrackers(entries) {{
      window.localStorage.setItem(SOCIAL_TRACKERS_KEY, JSON.stringify(entries));
    }}

    function setReportActionNote(message, state = '') {{
      reportActionNote.textContent = message;
      if (state) {{
        reportActionNote.dataset.state = state;
      }} else {{
        delete reportActionNote.dataset.state;
      }}
    }}

    function setSocialActionNote(message, state = '') {{
      socialActionNote.textContent = message;
      if (state) {{
        socialActionNote.dataset.state = state;
      }} else {{
        delete socialActionNote.dataset.state;
      }}
    }}

    function setSocialTweetActionNote(message, state = '') {{
      socialTweetActionNote.textContent = message;
      if (state) {{
        socialTweetActionNote.dataset.state = state;
      }} else {{
        delete socialTweetActionNote.dataset.state;
      }}
    }}

    function buildDashboardStatePayload() {{
      return {{
        watchlistStatus: normalizeWatchlistStatusPayload(watchlistStatusMap),
        strongJoinStatus: normalizeStrongJoinPayload(strongJoinMap),
        reports: loadReportEntries(),
        socialKolWatchlist: loadSocialTrackers(),
        socialPosts: loadSocialEntries(),
      }};
    }}

    async function requestDashboardAdminToken() {{
      const existing = getDashboardAdminToken();
      if (existing) return existing;
      return new Promise(resolve => {{
        const dialog = document.createElement('div');
        dialog.className = 'modal open';
        dialog.setAttribute('aria-hidden', 'false');
        dialog.innerHTML = `
          <div class="modal-card" style="max-width: 460px;">
            <div class="modal-head">
              <div>
                <h3>Dashboard admin token</h3>
                <p>输入后会保存在本机浏览器，用于调用云端分析和同步结果。</p>
              </div>
              <button class="modal-close" type="button" data-token-cancel>×</button>
            </div>
            <div class="report-field full-span" style="margin-top: 14px;">
              <label for="dashboard-admin-token-input">Admin token</label>
              <input id="dashboard-admin-token-input" class="report-input" type="password" autocomplete="off" placeholder="粘贴 Dashboard admin token" />
            </div>
            <div class="report-actions">
              <span class="report-action-note">Grok API key 保存在后端环境，不需要填在这里。</span>
              <button class="report-save-button" type="button" data-token-save>保存</button>
            </div>
          </div>
        `;
        document.body.appendChild(dialog);
        const input = dialog.querySelector('#dashboard-admin-token-input');
        const cleanup = token => {{
          if (token) setDashboardAdminToken(token);
          dialog.remove();
          resolve(token || '');
        }};
        dialog.querySelector('[data-token-save]').addEventListener('click', () => cleanup(input.value.trim()));
        dialog.querySelector('[data-token-cancel]').addEventListener('click', () => cleanup(''));
        dialog.addEventListener('keydown', event => {{
          if (event.key === 'Enter') cleanup(input.value.trim());
          if (event.key === 'Escape') cleanup('');
        }});
        setTimeout(() => input.focus(), 0);
      }});
    }}

    async function ensureDashboardAdminToken() {{
      const apiUrl = getDashboardStateApiUrl();
      if (!apiUrl) return '';
      const existing = getDashboardAdminToken();
      if (existing) return existing;
      return requestDashboardAdminToken();
    }}

    function applyRemoteDashboardState(payload) {{
      const nextWatchlistStatus = normalizeWatchlistStatusPayload(payload?.watchlistStatus || {{}});
      const nextStrongJoinStatus = normalizeStrongJoinPayload(payload?.strongJoinStatus || {{}});
      const nextReports = mergeReportEntries([], Array.isArray(payload?.reports) ? payload.reports : []);
      const nextSocialKolWatchlist = mergeSocialTrackers([], Array.isArray(payload?.socialKolWatchlist) ? payload.socialKolWatchlist : []);
      const nextSocialPosts = mergeSocialEntries([], Array.isArray(payload?.socialPosts) ? payload.socialPosts : []);
      replaceObjectContents(watchlistStatusMap, nextWatchlistStatus);
      replaceObjectContents(strongJoinMap, nextStrongJoinStatus);
      saveWatchlistStatus(watchlistStatusMap);
      saveStrongJoinStatus(strongJoinMap);
      saveReportEntries(nextReports);
      saveSocialTrackers(nextSocialKolWatchlist);
      saveSocialEntries(nextSocialPosts);
      syncSyntheticWatchlistRows();
      syncStrongStockStatusControls();
      applyWatchlistVisibility();
      renderReportEntries();
      renderSocialTrackers();
      renderSocialEntries();
    }}

    async function postDashboardState(payload, allowRetry = true) {{
      const apiUrl = getDashboardStateApiUrl();
      if (!apiUrl) return null;
      const headers = {{
        'Content-Type': 'application/json',
      }};
      const adminToken = getDashboardAdminToken();
      if (adminToken) {{
        headers['X-Dashboard-Admin-Token'] = adminToken;
      }}
      let response;
      try {{
        response = await fetch(apiUrl, {{
          method: 'POST',
          headers,
          body: JSON.stringify(payload),
        }});
      }} catch (error) {{
        throw new Error(`Dashboard state sync request failed: ${{apiUrl}} (${{error?.message || 'Failed to fetch'}})`);
      }}
      if (response.status === 401 && allowRetry) {{
        const promptedToken = await requestDashboardAdminToken();
        if (promptedToken) {{
          headers['X-Dashboard-Admin-Token'] = promptedToken;
          try {{
            response = await fetch(apiUrl, {{
              method: 'POST',
              headers,
              body: JSON.stringify(payload),
            }});
          }} catch (error) {{
            throw new Error(`Dashboard state sync request failed: ${{apiUrl}} (${{error?.message || 'Failed to fetch'}})`);
          }}
        }}
      }}
      if (!response.ok) {{
        const detail = await response.text();
        throw new Error(`Dashboard state sync failed: ${{response.status}} ${{detail}}`);
      }}
      return response.json();
    }}

    function queueDashboardStateSync(payload = buildDashboardStatePayload(), options = {{}}) {{
      const apiUrl = getDashboardStateApiUrl();
      if (!apiUrl) return Promise.resolve(null);
      const promptForToken = options.promptForToken === true;
      dashboardStateSyncPromise = dashboardStateSyncPromise
        .catch(() => null)
        .then(async () => {{
          if (promptForToken) {{
            const token = await ensureDashboardAdminToken();
            if (!token) return null;
          }}
          return postDashboardState(payload);
        }})
        .then(data => {{
          if (data && typeof data === 'object') {{
            applyRemoteDashboardState(data);
          }}
          return data;
        }})
        .catch(error => {{
          console.warn(error);
          return null;
        }});
      return dashboardStateSyncPromise;
    }}

    async function bootstrapDashboardState() {{
      const apiUrl = getDashboardStateApiUrl();
      if (!apiUrl || dashboardStateBootstrapped) return;
      dashboardStateBootstrapped = true;
      try {{
        const response = await fetch(apiUrl, {{
          method: 'GET',
          headers: {{ Accept: 'application/json' }},
        }});
        if (!response.ok) {{
          throw new Error(`Dashboard state bootstrap failed: ${{response.status}}`);
        }}
        const payload = await response.json();
        if (payload && typeof payload === 'object') {{
          applyRemoteDashboardState(payload);
        }}
      }} catch (error) {{
        console.warn(error);
      }}
    }}

    async function analyzeResearchText(rawText, pickedDate) {{
      const apiUrl = getReportAnalyzeApiUrl();
      if (!apiUrl) {{
        throw new Error('未配置投研分析接口');
      }}
      const token = await ensureDashboardAdminToken();
      if (!token) {{
        throw new Error('缺少 Dashboard admin token');
      }}
      let response;
      try {{
        response = await fetch(apiUrl, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-Dashboard-Admin-Token': token,
          }},
          body: JSON.stringify({{ text: rawText, date: pickedDate }}),
        }});
      }} catch (error) {{
        throw new Error(`投研分析接口请求失败：${{apiUrl}}（${{error?.message || 'Failed to fetch'}}）`);
      }}
      const payload = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        throw new Error(payload?.detail || payload?.error || `分析失败: ${{response.status}}`);
      }}
      return normalizeReportEntry({{
        ...(payload.analysis || {{}}),
        date: payload?.analysis?.date || pickedDate,
        rawText: payload?.analysis?.rawText || rawText,
        createdAt: Date.now(),
      }}, `report-${{Date.now()}}`);
    }}

    async function analyzeSocialTweetUrl(tweetUrl) {{
      const apiUrl = getSocialAnalyzeApiUrl();
      if (!apiUrl) {{
        throw new Error('未配置社交媒体分析接口');
      }}
      const token = await ensureDashboardAdminToken();
      if (!token) {{
        throw new Error('缺少 Dashboard admin token');
      }}
      const response = await fetch(apiUrl, {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
          'X-Dashboard-Admin-Token': token,
        }},
        body: JSON.stringify({{ tweetUrl, date: '{AS_OF.isoformat()}' }}),
      }});
      const payload = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        throw new Error(payload?.detail || payload?.error || `分析失败: ${{response.status}}`);
      }}
      const analysis = payload.analysis || {{}};
      return normalizeSocialEntry({{
        ...analysis,
        id: analysis.id || `social-${{Date.now()}}`,
        date: analysis.date || '{AS_OF.isoformat()}',
        content: analysis.summary || analysis.content || '',
        sourceUrl: analysis.sourceUrl || analysis.tweetUrl || tweetUrl,
        tweetUrl: analysis.tweetUrl || analysis.sourceUrl || tweetUrl,
        createdAt: Date.now(),
      }}, `social-${{Date.now()}}`);
    }}

    function renderReportTargetCell(target) {{
      const display = escapeHtml(target);
      const matchedCode = modalCodeByAlias[normalizeModalAlias(target)];
      if (!matchedCode) {{
        return display;
      }}
      const matchedItem = modalItems[modalIndexByCode[matchedCode]];
      const codeText = matchedItem?.code ? `<span class="stock-code">${{escapeHtml(matchedItem.code)}}</span>` : '';
      return `<button class="report-target-trigger" type="button" data-code="${{escapeHtml(matchedCode)}}" ${{renderReportHoverAttrs(matchedItem)}}><span class="stock-name">${{display}}</span>${{codeText}}</button>`;
    }}

    function formatReportPrice(value) {{
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric.toFixed(2) : '暂无';
    }}

    function formatReportPct(value) {{
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return '暂无';
      return `${{numeric >= 0 ? '+' : ''}}${{numeric.toFixed(2)}}%`;
    }}

    function renderReportHoverAttrs(item) {{
      const title = item?.code ? `${{item.name || item.code}} · ${{item.code}}` : (item?.name || '标的信息');
      const pctValue = item?.todayPct == null ? '' : Number(item.todayPct);
      return [
        `data-hover-title="${{escapeHtml(title || '标的信息')}}"`,
        `data-hover-price="${{escapeHtml(formatReportPrice(item?.latestClose))}}"`,
        `data-hover-cap="${{escapeHtml(item?.totalMarketCap == null ? '暂无' : formatYiRmb(item.totalMarketCap))}}"`,
        `data-hover-pct="${{escapeHtml(formatReportPct(item?.todayPct))}}"`,
        `data-hover-pct-class="${{escapeHtml(pctClass(pctValue))}}"`,
      ].join(' ');
    }}

    function renderTargetTags(targets) {{
      if (!Array.isArray(targets) || !targets.length) return '';
      const html = targets.map(target => {{
        const label = escapeHtml(target);
        const matchedCode = modalCodeByAlias[normalizeModalAlias(target)];
        if (!matchedCode) {{
          return `<span class="report-tag" ${{renderReportHoverAttrs({{ name: target }})}}>${{label}}</span>`;
        }}
        const matchedItem = modalItems[modalIndexByCode[matchedCode]];
        return `<button class="report-target-trigger report-tag-button" type="button" data-code="${{escapeHtml(matchedCode)}}" ${{renderReportHoverAttrs(matchedItem)}}><span class="report-tag">${{label}}</span></button>`;
      }}).join('');
      return `<div class="report-tags">${{html}}</div>`;
    }}

    function openReportDetailDrawer(reportId) {{
      const entry = loadReportEntries().find(item => item.id === reportId);
      if (!entry) return;
      const title = entry.target || (Array.isArray(entry.targets) && entry.targets[0]) || entry.industry || '投研详情';
      const subtitleParts = [entry.industry || '未分类', entry.date || '无日期'].filter(Boolean);
      const metaItems = [
        entry.industry ? `行业：${{entry.industry}}` : '',
        entry.date ? `日期：${{entry.date}}` : '',
        Array.isArray(entry.targets) && entry.targets.length ? `标的：${{entry.targets.length}} 个` : '',
      ].filter(Boolean);
      reportDetailTitle.textContent = title;
      reportDetailSubtitle.textContent = subtitleParts.join(' · ');
      reportDetailMeta.innerHTML = metaItems.map(item => `<span>${{escapeHtml(item)}}</span>`).join('');
      reportDetailTargets.innerHTML = renderTargetTags(entry.targets) || '<span class="report-tag">未提及</span>';
      reportDetailSummary.textContent = entry.summary || entry.content || '暂无';
      reportDetailSource.textContent = entry.rawText || '暂无原文';
      reportDetailDrawer.classList.add('open');
      reportDetailDrawer.setAttribute('aria-hidden', 'false');
    }}

    function closeReportDetailDrawer() {{
      reportDetailDrawer.classList.remove('open');
      reportDetailDrawer.setAttribute('aria-hidden', 'true');
    }}

    function setSelectVisualState(select) {{
      select.dataset.state = select.value;
    }}

    function formatYi(value) {{
      const numeric = Number(value || 0);
      return (numeric / 1e8).toFixed(2) + '亿';
    }}

    function formatYiRmb(value) {{
      const numeric = Number(value || 0);
      return (numeric / 1e8).toFixed(2) + '亿';
    }}

    function pctClass(value) {{
      const numeric = Number(value);
      if (Number.isNaN(numeric)) return 'pct-flat';
      if (numeric > 0) return 'pct-rise';
      if (numeric < 0) return 'pct-fall';
      return 'pct-flat';
    }}

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    const watchlistSortState = {{ field: 'floatMarketCap', direction: 'desc' }};
    const CAP_GROUP_ORDER = ['mega', 'large', 'small'];

    function capGroupKeyFromValue(value) {{
      const cap = Number(value) || 0;
      if (cap >= 100000000000) return 'mega';
      if (cap >= 50000000000) return 'large';
      return 'small';
    }}

    function ensureRowCapGroup(row) {{
      if (!row.dataset.capGroup) {{
        row.dataset.capGroup = capGroupKeyFromValue(row.dataset.totalMarketCap);
      }}
      return row.dataset.capGroup;
    }}

    function rebuildGroupedTable(tbody, rowSelector, orderedRows) {{
      const rows = orderedRows || [...tbody.querySelectorAll(rowSelector)];
      const rowsByGroup = Object.fromEntries(CAP_GROUP_ORDER.map(key => [key, []]));
      rows.forEach(row => {{
        const key = ensureRowCapGroup(row);
        if (!rowsByGroup[key]) rowsByGroup[key] = [];
        rowsByGroup[key].push(row);
      }});
      CAP_GROUP_ORDER.forEach(key => {{
        const groupRows = rowsByGroup[key] || [];
        groupRows.forEach(row => tbody.appendChild(row));
      }});
      const emptyRow = tbody.querySelector('[data-empty-row="true"]');
      if (emptyRow) {{
        const visibleRows = rows.filter(row => row.style.display !== 'none');
        emptyRow.style.display = visibleRows.length ? 'none' : '';
      }}
    }}

    function refreshGroupedTables() {{
      rebuildGroupedTable(strongStocksTableBody, 'tr[data-strong-stock-row="true"]');
      rebuildGroupedTable(watchlistTableBody, 'tr[data-watchlist-row="true"]');
    }}

    function parseRowNumber(row, key) {{
      const raw = row.dataset[key];
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    }}

    function compareNullableNumbers(a, b, direction) {{
      const aMissing = a == null;
      const bMissing = b == null;
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      return direction === 'asc' ? a - b : b - a;
    }}

    function updateWatchlistSortIndicators() {{
      document.querySelectorAll('[data-watchlist-sort]').forEach(button => {{
        const active = button.dataset.watchlistSort === watchlistSortState.field;
        button.dataset.active = active ? 'true' : 'false';
        const indicator = button.querySelector('.sort-indicator');
        if (indicator) {{
          indicator.textContent = active ? (watchlistSortState.direction === 'asc' ? '↑' : '↓') : '↕';
        }}
      }});
    }}

    function applyWatchlistSort() {{
      const rows = [...watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]')];
      rows.sort((a, b) => {{
        const left = parseRowNumber(a, watchlistSortState.field);
        const right = parseRowNumber(b, watchlistSortState.field);
        const primary = compareNullableNumbers(left, right, watchlistSortState.direction);
        if (primary !== 0) return primary;
        return Number(b.dataset.floatMarketCap || 0) - Number(a.dataset.floatMarketCap || 0);
      }});
      rebuildGroupedTable(watchlistTableBody, 'tr[data-watchlist-row="true"]', rows);
      updateWatchlistSortIndicators();
      renumberTableRows();
    }}

    function renumberTableRows() {{
      let strongIndex = 1;
      strongStocksTableBody.querySelectorAll('tr[data-strong-stock-row="true"]').forEach(row => {{
        if (row.style.display === 'none') return;
        const cell = row.querySelector('[data-index-cell="strong"]');
        if (cell) cell.textContent = String(strongIndex++);
      }});
      let watchIndex = 1;
      watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]').forEach(row => {{
        if (row.style.display === 'none') return;
        const cell = row.querySelector('[data-index-cell="watchlist"]');
        if (cell) cell.textContent = String(watchIndex++);
      }});
    }}

    function createSyntheticWatchlistRow(item) {{
      const turnoverText = item.turnoverRate == null ? '-' : Number(item.turnoverRate).toFixed(2) + '%';
      const latestCloseText = item.latestClose == null ? '-' : Number(item.latestClose).toFixed(2);
      const todayPctText = item.todayPct == null ? '-' : (Number(item.todayPct) >= 0 ? '+' : '') + Number(item.todayPct).toFixed(2) + '%';
      const peText = item.peRatio == null ? '暂无' : Number(item.peRatio).toFixed(2);
      const pctCls = pctClass(item.todayPct);
      const wrapper = document.createElement('tbody');
      wrapper.innerHTML = `
        <tr data-watchlist-row="true" data-cap-group="${{capGroupKeyFromValue(item.totalMarketCap)}}" data-code="${{item.code}}" data-total-market-cap="${{Number(item.totalMarketCap || 0)}}" data-float-market-cap="${{Number(item.floatMarketCap || 0)}}" data-today-amount="${{Number(item.todayAmount || 0)}}" data-turnover-rate="${{item.turnoverRate == null ? '' : Number(item.turnoverRate)}}" data-today-pct="${{item.todayPct == null ? '' : Number(item.todayPct)}}" data-synthetic="true">
          <td class="index-cell" data-index-cell="watchlist"></td>
          <td data-search="${{item.name}} ${{item.code}}">
            <div class="strong-name-cell">
              <span class="stock-name">${{item.name}}</span>
              <span class="stock-code">${{item.code}}</span>
            </div>
          </td>
          <td>${{item.mainBusiness || '-'}}</td>
          <td>${{formatYi(item.totalMarketCap)}} / ${{formatYi(item.floatMarketCap)}}</td>
          <td>${{formatYiRmb(item.todayAmount)}}</td>
          <td>${{turnoverText}}</td>
          <td>${{latestCloseText}}</td>
          <td class="${{pctCls}}">${{todayPctText}}</td>
          <td class="nowrap-cell">${{peText}}</td>
          <td>
            <select class="status-select" data-code="${{item.code}}" aria-label="${{item.name}}状态">
              <option value="active" selected>跟踪中</option>
              <option value="removed">移除</option>
            </select>
          </td>
        </tr>
      `;
      return wrapper.firstElementChild;
    }}

    function bindWatchlistRow(row) {{
      if (row.dataset.boundRow === 'true') return;
      row.dataset.boundRow = 'true';
      row.addEventListener('click', event => {{
        if (event.target.closest('select, option, button, a, input, textarea, label')) {{
          return;
        }}
        openStockModalByCode(row.dataset.code);
      }});
      const select = row.querySelector('.status-select');
      if (!select || select.dataset.boundSelect === 'true') return;
      select.dataset.boundSelect = 'true';
      const code = select.dataset.code;
      const saved = watchlistStatusMap[code];
      if (saved === 'removed') {{
        select.value = 'removed';
      }}
      setSelectVisualState(select);
      select.addEventListener('change', () => {{
        watchlistStatusMap[code] = select.value;
        if (select.value === 'active') {{
          delete watchlistStatusMap[code];
        }}
        setSelectVisualState(select);
        saveWatchlistStatus(watchlistStatusMap);
        applyWatchlistVisibility();
        queueDashboardStateSync(undefined, {{ promptForToken: true }});
      }});
    }}

    function bindStrongStockRow(row) {{
      if (row.dataset.boundRow === 'true') return;
      row.dataset.boundRow = 'true';
      row.addEventListener('click', event => {{
        if (event.target.closest('select, option, button, a, input, textarea, label')) {{
          return;
        }}
        openStockModalByCode(row.dataset.code);
      }});
    }}

    function syncStrongStockStatusControls() {{
      document.querySelectorAll('.join-watchlist-select').forEach(select => {{
        const code = select.dataset.code;
        if (select.dataset.watchlistMember === 'true') {{
          const mainSelect = watchlistTableBody.querySelector(`.status-select[data-code="${{code}}"]`);
          const value = mainSelect ? mainSelect.value : (watchlistStatusMap[code] === 'removed' ? 'removed' : 'active');
          select.value = value === 'removed' ? 'removed' : 'active';
          setSelectVisualState(select);
          return;
        }}
        select.value = strongJoinMap[code] === 'joined' ? 'joined' : 'pending';
        setSelectVisualState(select);
      }});
    }}

    function collectReportWatchlistItems() {{
      const entries = loadReportEntries();
      const seenCodes = new Set();
      const items = [];
      entries.forEach(entry => {{
        const names = Array.isArray(entry.targets) && entry.targets.length ? entry.targets : [entry.target];
        names.forEach(name => {{
          const matchedCode = modalCodeByAlias[normalizeModalAlias(name)];
          if (!matchedCode || seenCodes.has(matchedCode)) return;
          const idx = modalIndexByCode[matchedCode];
          const item = modalItems[idx];
          if (!item) return;
          seenCodes.add(matchedCode);
          items.push(item);
        }});
      }});
      return items;
    }}

    function collectSocialWatchlistItems() {{
      const entries = loadSocialEntries();
      const seenCodes = new Set();
      const items = [];
      entries.forEach(entry => {{
        const names = Array.isArray(entry.targets) && entry.targets.length ? entry.targets : [entry.target];
        names.forEach(name => {{
          const matchedCode = modalCodeByAlias[normalizeModalAlias(name)];
          if (!matchedCode || seenCodes.has(matchedCode)) return;
          const idx = modalIndexByCode[matchedCode];
          const item = modalItems[idx];
          if (!item) return;
          seenCodes.add(matchedCode);
          items.push(item);
        }});
      }});
      return items;
    }}

    function syncSyntheticWatchlistRows() {{
      watchlistTableBody.querySelectorAll('tr[data-synthetic="true"]').forEach(row => row.remove());
      const existingCodes = new Set([...watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]:not([data-synthetic="true"])')].map(row => row.dataset.code));
      const syntheticItems = [];
      strongStocks.forEach(item => {{
        if (existingCodes.has(item.code)) return;
        if (strongJoinMap[item.code] !== 'joined') return;
        syntheticItems.push(item);
        existingCodes.add(item.code);
      }});
      collectReportWatchlistItems().forEach(item => {{
        if (existingCodes.has(item.code)) return;
        syntheticItems.push(item);
        existingCodes.add(item.code);
      }});
      collectSocialWatchlistItems().forEach(item => {{
        if (existingCodes.has(item.code)) return;
        syntheticItems.push(item);
        existingCodes.add(item.code);
      }});
      syntheticItems.forEach(item => {{
        const row = createSyntheticWatchlistRow(item);
        watchlistTableBody.appendChild(row);
        bindWatchlistRow(row);
      }});
      applyWatchlistSort();
      applyWatchlistVisibility();
      syncStrongStockStatusControls();
    }}

    function renderRemovedList() {{
      const removedRows = [...watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]')]
        .filter(row => row.querySelector('.status-select')?.value === 'removed');
      if (!removedRows.length) {{
        removedPanel.hidden = true;
        removedList.innerHTML = '';
        return;
      }}

      removedPanel.hidden = false;
      removedList.innerHTML = removedRows.map(row => {{
        const code = row.dataset.code || '';
        const name = row.querySelector('.stock-name')?.textContent || code;
        return `
          <div class="removed-item">
            <div class="removed-info">
              <span class="stock-name">${{name}}</span>
              <span class="stock-code">${{code}}</span>
            </div>
            <select class="status-select" data-code="${{code}}" data-state="removed" aria-label="${{name}}状态">
              <option value="active">跟踪中</option>
              <option value="removed" selected>移除</option>
            </select>
          </div>
        `;
      }}).join('');

      removedList.querySelectorAll('.status-select').forEach(select => {{
        setSelectVisualState(select);
        select.addEventListener('change', () => {{
          const code = select.dataset.code;
          const mainSelect = watchlistTableBody.querySelector(`.status-select[data-code="${{code}}"]`);
          if (mainSelect) {{
            mainSelect.value = select.value;
            mainSelect.dispatchEvent(new Event('change'));
          }}
        }});
      }});
    }}

    function applyWatchlistVisibility() {{
      const keyword = stockSearch.value.trim().toLowerCase();
      watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]').forEach(row => {{
        const select = row.querySelector('.status-select');
        const isRemoved = select && select.value === 'removed';
        const haystack = (row.querySelector('td[data-search]')?.dataset.search || '').toLowerCase();
        const matchesKeyword = !keyword || haystack.includes(keyword);
        row.style.display = !isRemoved && matchesKeyword ? '' : 'none';
      }});
      renderRemovedList();
      refreshGroupedTables();
      renumberTableRows();
    }}

    function renderReportEntries() {{
      const entries = loadReportEntries().sort((a, b) => {{
        const dateA = a.date || '';
        const dateB = b.date || '';
        if (dateA !== dateB) return dateB.localeCompare(dateA);
        return (b.createdAt || 0) - (a.createdAt || 0);
      }});
      const hiddenIds = loadHiddenReportIds();
      const hiddenCount = entries.filter(entry => hiddenIds.has(entry.id)).length;
      const visibleEntries = showHiddenReports ? entries : entries.filter(entry => !hiddenIds.has(entry.id));
      reportCountPill.textContent = hiddenCount
        ? `共 ${{visibleEntries.length}} 条 · 隐藏 ${{hiddenCount}} 条`
        : `共 ${{visibleEntries.length}} 条`;
      reportEmptyState.hidden = visibleEntries.length > 0;
      reportTableBody.innerHTML = visibleEntries.map((entry, index) => {{
        const isHidden = hiddenIds.has(entry.id);
        const rowClass = isHidden ? 'report-entry-row report-row-hidden' : 'report-entry-row';
        return `
          <tr class="${{rowClass}}" data-report-id="${{escapeHtml(entry.id)}}">
            <td class="index-cell">${{index + 1}}</td>
            <td>${{escapeHtml(entry.industry || '未分类')}}</td>
            <td>
              <div class="report-summary">
                <p>${{escapeHtml(entry.summary || entry.content)}}</p>
                ${{renderTargetTags(entry.targets)}}
              </div>
            </td>
            <td class="nowrap-cell">${{escapeHtml(entry.date)}}</td>
            <td class="report-hide-cell">
              <input class="report-hide-checkbox" type="checkbox" data-report-id="${{escapeHtml(entry.id)}}" ${{isHidden ? 'checked' : ''}} aria-label="隐藏此条研究" />
            </td>
          </tr>
        `;
      }}).join('');
    }}

    function renderSocialEntries() {{
      const entries = loadSocialEntries().sort((a, b) => {{
        const dateA = a.date || '';
        const dateB = b.date || '';
        if (dateA !== dateB) return dateB.localeCompare(dateA);
        return (b.createdAt || 0) - (a.createdAt || 0);
      }});
      socialCountPill.textContent = `共 ${{entries.length}} 条`;
      socialEmptyState.hidden = entries.length > 0;
      socialTableBody.innerHTML = entries.map((entry, index) => {{
        const sourceUrl = entry.sourceUrl || entry.tweetUrl || '';
        const sourceLink = sourceUrl
          ? `<a href="${{escapeHtml(sourceUrl)}}" target="_blank" rel="noopener noreferrer">原文</a>`
          : '-';
        return `
          <tr>
            <td class="index-cell">${{index + 1}}</td>
            <td class="nowrap-cell">${{escapeHtml(entry.date)}}</td>
            <td>
              <div class="report-summary">
                <strong>${{escapeHtml(entry.kol)}}</strong>
                <p>${{escapeHtml(entry.handle ? '@' + entry.handle : (entry.platform || 'X'))}}</p>
              </div>
            </td>
            <td class="nowrap-cell">${{sourceLink}}</td>
            <td>${{escapeHtml(entry.industry || '-')}}</td>
            <td>
              <div class="report-summary">
                <p>${{escapeHtml(entry.summary || entry.content)}}</p>
                ${{renderTargetTags(entry.targets)}}
              </div>
            </td>
            <td>
              <div class="report-summary">
                <p>${{escapeHtml(entry.translatedText || entry.rawText || '-')}}</p>
              </div>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    function renderSocialTrackers() {{
      const entries = loadSocialTrackers().sort((a, b) => {{
        const enabledA = a.enabled ? 1 : 0;
        const enabledB = b.enabled ? 1 : 0;
        if (enabledA !== enabledB) return enabledB - enabledA;
        return (a.createdAt || 0) - (b.createdAt || 0);
      }});
      socialTrackerCountPill.textContent = `共 ${{entries.length}} 个`;
      socialTrackerEmptyState.hidden = entries.length > 0;
      socialTrackerTableBody.innerHTML = entries.map((entry, index) => `
        <tr>
          <td class="index-cell">${{index + 1}}</td>
          <td>
            <div class="report-summary">
              <strong>${{escapeHtml(entry.name)}}</strong>
            </div>
          </td>
          <td class="nowrap-cell">@${{escapeHtml(entry.handle)}}</td>
          <td class="nowrap-cell">${{escapeHtml(entry.platform || 'X')}}</td>
          <td>
            <select class="status-select social-tracker-select" data-tracker-id="${{escapeHtml(entry.id)}}" aria-label="${{escapeHtml(entry.name)}}状态">
              <option value="enabled"${{entry.enabled ? ' selected' : ''}}>启用</option>
              <option value="disabled"${{entry.enabled ? '' : ' selected'}}>停用</option>
            </select>
          </td>
        </tr>
      `).join('');
      socialTrackerTableBody.querySelectorAll('.social-tracker-select').forEach(select => {{
        setSelectVisualState(select);
        select.addEventListener('change', () => {{
          const trackerId = select.dataset.trackerId;
          const nextEntries = loadSocialTrackers().map(entry => {{
            if (entry.id !== trackerId) return entry;
            return {{ ...entry, enabled: select.value === 'enabled' }};
          }});
          saveSocialTrackers(nextEntries);
          renderSocialTrackers();
          queueDashboardStateSync(undefined, {{ promptForToken: true }});
        }});
      }});
    }}

    const watchlistStatusMap = loadWatchlistStatus();
    const strongJoinMap = loadStrongJoinStatus();

    document.querySelectorAll('.stock-trigger').forEach(node => {{
      node.addEventListener('click', event => {{
        event.stopPropagation();
        openStockModalByCode(node.dataset.code);
      }});
    }});
    document.querySelectorAll('.market-card-button[data-index-code]').forEach(node => {{
      node.addEventListener('click', event => {{
        event.stopPropagation();
        openIndexModalByCode(node.dataset.indexCode);
      }});
    }});
    themeButtons.forEach(button => {{
      button.addEventListener('click', () => applyTheme(button.dataset.themeValue));
    }});
    sidebarLinks.forEach(link => {{
      link.addEventListener('click', () => setActiveView(link.dataset.viewTarget));
    }});

    watchlistTableBody.querySelectorAll('tr[data-watchlist-row="true"]').forEach(bindWatchlistRow);
    strongStocksTableBody.querySelectorAll('tr[data-strong-stock-row="true"]').forEach(bindStrongStockRow);

    document.querySelectorAll('.join-watchlist-select').forEach(select => {{
      const code = select.dataset.code;
      if (select.dataset.watchlistMember === 'true') {{
        const mainSelect = watchlistTableBody.querySelector(`.status-select[data-code="${{code}}"]`);
        select.value = mainSelect?.value === 'removed' ? 'removed' : 'active';
        setSelectVisualState(select);
        select.addEventListener('change', () => {{
          const targetValue = select.value === 'removed' ? 'removed' : 'active';
          setSelectVisualState(select);
          const nextMainSelect = watchlistTableBody.querySelector(`.status-select[data-code="${{code}}"]`);
          if (nextMainSelect) {{
            nextMainSelect.value = targetValue;
            nextMainSelect.dispatchEvent(new Event('change'));
          }}
        }});
        return;
      }}
      if (strongJoinMap[code] === 'joined') {{
        select.value = 'joined';
      }}
      setSelectVisualState(select);
      select.addEventListener('change', () => {{
        if (select.value === 'joined') {{
          strongJoinMap[code] = 'joined';
        }} else {{
          delete strongJoinMap[code];
        }}
        setSelectVisualState(select);
        saveStrongJoinStatus(strongJoinMap);
        syncSyntheticWatchlistRows();
        queueDashboardStateSync(undefined, {{ promptForToken: true }});
      }});
    }});

    document.querySelectorAll('[data-watchlist-sort]').forEach(button => {{
      button.addEventListener('click', () => {{
        const field = button.dataset.watchlistSort;
        const defaultDirection = button.dataset.defaultDirection || 'desc';
        if (watchlistSortState.field === field) {{
          watchlistSortState.direction = watchlistSortState.direction === 'desc' ? 'asc' : 'desc';
        }} else {{
          watchlistSortState.field = field;
          watchlistSortState.direction = defaultDirection;
        }}
        applyWatchlistSort();
        applyWatchlistVisibility();
      }});
    }});

    stockSearch.addEventListener('input', () => {{
      const keyword = stockSearch.value.trim().toLowerCase();
      document.querySelectorAll('.summary-table tbody tr').forEach(row => {{
        if (row.dataset.emptyRow === 'true') return;
        if (row.dataset.watchlistRow === 'true') return;
        const haystack = (row.querySelector('td[data-search]')?.dataset.search || '').toLowerCase();
        row.style.display = !keyword || haystack.includes(keyword) ? '' : 'none';
      }});
      applyWatchlistVisibility();
    }});

    reportSaveButton.addEventListener('click', async () => {{
      const rawText = reportContentInput.value.trim();
      const pickedDate = reportDateInput.value || '{AS_OF.isoformat()}';
      if (!rawText) {{
        window.alert('请先粘贴需要提取的投研内容。');
        return;
      }}
      const originalLabel = reportSaveButton.textContent;
      reportSaveButton.disabled = true;
      reportSaveButton.textContent = '提取中...';
      setReportActionNote('GPT 正在提取关键信息并保存到表格...', 'busy');
      try {{
        const analyzedEntry = await analyzeResearchText(rawText, pickedDate);
        const entries = loadReportEntries();
        entries.push(analyzedEntry);
        saveReportEntries(entries);
        reportContentInput.value = '';
        reportDateInput.value = pickedDate;
        renderReportEntries();
        syncSyntheticWatchlistRows();
        await queueDashboardStateSync(undefined, {{ promptForToken: false }});
        setReportActionNote(`已提取并保存：${{analyzedEntry.target}}`, '');
      }} catch (error) {{
        console.error(error);
        setReportActionNote(error?.message || '提取失败，请稍后重试。', 'error');
        window.alert(error?.message || '投研内容提取失败，请稍后重试。');
      }} finally {{
        reportSaveButton.disabled = false;
        reportSaveButton.textContent = originalLabel;
      }}
    }});

    socialTweetSaveButton.addEventListener('click', async () => {{
      const tweetUrl = socialTweetUrlInput.value.trim();
      if (!tweetUrl) {{
        window.alert('请先粘贴 X 推文链接。');
        return;
      }}
      if (!/(?:x\\.com|twitter\\.com)\\/[^/]+\\/status(?:es)?\\/\\d+/i.test(tweetUrl)) {{
        window.alert('请粘贴单条 X 推文链接，例如 https://x.com/user/status/123');
        return;
      }}
      const originalLabel = socialTweetSaveButton.textContent;
      socialTweetSaveButton.disabled = true;
      socialTweetSaveButton.textContent = '总结中...';
      setSocialTweetActionNote('正在抓取推文并生成中文总结...', 'busy');
      try {{
        const analyzedEntry = await analyzeSocialTweetUrl(tweetUrl);
        if (!analyzedEntry) {{
          throw new Error('推文分析结果无效');
        }}
        const entries = loadSocialEntries();
        const normalizedUrl = (analyzedEntry.sourceUrl || analyzedEntry.tweetUrl || tweetUrl).split('?')[0];
        const withoutSameUrl = entries.filter(entry => String(entry.sourceUrl || entry.tweetUrl || '').split('?')[0] !== normalizedUrl);
        withoutSameUrl.push(analyzedEntry);
        saveSocialEntries(withoutSameUrl);
        socialTweetUrlInput.value = '';
        renderSocialEntries();
        syncSyntheticWatchlistRows();
        await queueDashboardStateSync(undefined, {{ promptForToken: false }});
        setSocialTweetActionNote(`已记录：${{analyzedEntry.kol}} / ${{analyzedEntry.target || '未提及'}}`, '');
      }} catch (error) {{
        console.error(error);
        setSocialTweetActionNote(error?.message || '推文总结失败，请稍后重试。', 'error');
        window.alert(error?.message || '推文总结失败，请稍后重试。');
      }} finally {{
        socialTweetSaveButton.disabled = false;
        socialTweetSaveButton.textContent = originalLabel;
      }}
    }});

    socialSaveButton.addEventListener('click', async () => {{
      const name = socialKolNameInput.value.trim();
      const rawHandle = socialKolHandleInput.value.trim();
      const handle = rawHandle.replace(/^@+/, '');
      const platform = (socialKolPlatformInput.value || 'X').trim() || 'X';
      if (!name) {{
        window.alert('请先填写 KOL 名称。');
        return;
      }}
      if (!handle) {{
        window.alert('请先填写 KOL ID / @handle。');
        return;
      }}
      const entries = loadSocialTrackers();
      const exists = entries.some(entry => String(entry.handle || '').toLowerCase() === handle.toLowerCase());
      if (exists) {{
        setSocialActionNote(`@${{handle}} 已在追踪列表中。`, 'error');
        return;
      }}
      const nextEntry = normalizeSocialTracker({{
        id: `kol-${{Date.now()}}`,
        name,
        handle,
        platform,
        enabled: true,
        createdAt: Date.now(),
      }}, `kol-${{Date.now()}}`);
      if (!nextEntry) {{
        window.alert('KOL 信息无效，请重新填写。');
        return;
      }}
      const originalLabel = socialSaveButton.textContent;
      socialSaveButton.disabled = true;
      socialSaveButton.textContent = '保存中...';
      setSocialActionNote('正在保存追踪 KOL，并同步到云端状态...', 'busy');
      try {{
        entries.push(nextEntry);
        saveSocialTrackers(entries);
        socialKolNameInput.value = '';
        socialKolHandleInput.value = '';
        socialKolPlatformInput.value = 'X';
        renderSocialTrackers();
        await queueDashboardStateSync(undefined, {{ promptForToken: false }});
        setSocialActionNote(`已加入追踪：${{nextEntry.name}} (@${{nextEntry.handle}})`, '');
      }} catch (error) {{
        console.error(error);
        setSocialActionNote(error?.message || '保存失败，请稍后重试。', 'error');
        window.alert(error?.message || '保存追踪 KOL 失败，请稍后重试。');
      }} finally {{
        socialSaveButton.disabled = false;
        socialSaveButton.textContent = originalLabel;
      }}
    }});

    function positionReportTargetHover(trigger) {{
      if (!reportTargetHoverCard || reportTargetHoverCard.hidden) return;
      const rect = trigger.getBoundingClientRect();
      const gap = 8;
      const margin = 10;
      reportTargetHoverCard.style.left = '0px';
      reportTargetHoverCard.style.top = '0px';
      const cardRect = reportTargetHoverCard.getBoundingClientRect();
      let left = rect.left;
      let top = rect.bottom + gap;
      left = Math.max(margin, Math.min(left, window.innerWidth - cardRect.width - margin));
      if (top + cardRect.height > window.innerHeight - margin) {{
        top = rect.top - cardRect.height - gap;
      }}
      top = Math.max(margin, top);
      reportTargetHoverCard.style.left = `${{left}}px`;
      reportTargetHoverCard.style.top = `${{top}}px`;
    }}

    function showReportTargetHover(trigger) {{
      if (!reportTargetHoverCard || !trigger?.dataset.hoverTitle) return;
      reportTargetHoverCard.innerHTML = `
        <div class="report-hover-title">${{escapeHtml(trigger.dataset.hoverTitle)}}</div>
        <div class="report-hover-lines">
          <div class="report-hover-row"><span class="report-hover-label">股票市价</span><span class="report-hover-value">${{escapeHtml(trigger.dataset.hoverPrice || '暂无')}}</span></div>
          <div class="report-hover-row"><span class="report-hover-label">市值</span><span class="report-hover-value">${{escapeHtml(trigger.dataset.hoverCap || '暂无')}}</span></div>
          <div class="report-hover-row"><span class="report-hover-label">最近一日涨跌幅</span><span class="report-hover-value ${{escapeHtml(trigger.dataset.hoverPctClass || 'pct-flat')}}">${{escapeHtml(trigger.dataset.hoverPct || '暂无')}}</span></div>
        </div>
      `;
      reportTargetHoverCard.hidden = false;
      positionReportTargetHover(trigger);
    }}

    function hideReportTargetHover() {{
      if (reportTargetHoverCard) {{
        reportTargetHoverCard.hidden = true;
      }}
    }}

    reportHiddenToggle?.addEventListener('change', () => {{
      showHiddenReports = Boolean(reportHiddenToggle.checked);
      hideReportTargetHover();
      renderReportEntries();
    }});

    reportTableBody.addEventListener('mouseover', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger) showReportTargetHover(trigger);
    }});

    reportTableBody.addEventListener('mouseout', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger && !trigger.contains(event.relatedTarget)) hideReportTargetHover();
    }});

    reportTableBody.addEventListener('focusin', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger) showReportTargetHover(trigger);
    }});

    reportTableBody.addEventListener('focusout', hideReportTargetHover);

    reportDetailDrawer.addEventListener('mouseover', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger) showReportTargetHover(trigger);
    }});

    reportDetailDrawer.addEventListener('mouseout', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger && !trigger.contains(event.relatedTarget)) hideReportTargetHover();
    }});

    reportDetailDrawer.addEventListener('focusin', event => {{
      const trigger = event.target.closest('.report-target-trigger, .report-tag[data-hover-title]');
      if (trigger) showReportTargetHover(trigger);
    }});

    reportDetailDrawer.addEventListener('focusout', hideReportTargetHover);

    reportDetailDrawer.addEventListener('click', event => {{
      const trigger = event.target.closest('.report-target-trigger');
      if (trigger) {{
        event.preventDefault();
        event.stopPropagation();
        openStockModalByCode(trigger.dataset.code);
        return;
      }}
      if (event.target === reportDetailDrawer) {{
        closeReportDetailDrawer();
      }}
    }});

    reportTableBody.addEventListener('click', event => {{
      const hideCheckbox = event.target.closest('.report-hide-checkbox');
      if (hideCheckbox) {{
        event.stopPropagation();
        setReportEntryHidden(hideCheckbox.dataset.reportId, hideCheckbox.checked);
        hideReportTargetHover();
        renderReportEntries();
        return;
      }}
      const trigger = event.target.closest('.report-target-trigger');
      if (trigger) {{
        event.preventDefault();
        event.stopPropagation();
        openStockModalByCode(trigger.dataset.code);
        return;
      }}
      const row = event.target.closest('.report-entry-row');
      if (!row) return;
      openReportDetailDrawer(row.dataset.reportId);
    }});

    socialTableBody.addEventListener('click', event => {{
      const trigger = event.target.closest('.report-target-trigger');
      if (!trigger) return;
      event.preventDefault();
      event.stopPropagation();
      openStockModalByCode(trigger.dataset.code);
    }});

    syncSyntheticWatchlistRows();
    syncStrongStockStatusControls();
    updateWatchlistSortIndicators();
    applyWatchlistSort();
    renderReportEntries();
    renderSocialTrackers();
    renderSocialEntries();
    window.StockKillerDashboard = {{
      setStateApiUrl(url) {{
        const normalized = String(url || '').trim();
        if (normalized) {{
          window.localStorage.setItem(DASHBOARD_STATE_API_KEY, normalized);
        }} else {{
          window.localStorage.removeItem(DASHBOARD_STATE_API_KEY);
        }}
      }},
      setAnalyzeApiUrl(url) {{
        const normalized = String(url || '').trim();
        if (normalized) {{
          window.localStorage.setItem(DASHBOARD_ANALYZE_API_KEY, normalized);
        }} else {{
          window.localStorage.removeItem(DASHBOARD_ANALYZE_API_KEY);
        }}
      }},
      setAdminToken(token) {{
        setDashboardAdminToken(token);
      }},
      clearAdminToken() {{
        setDashboardAdminToken('');
      }},
      syncNow() {{
        return queueDashboardStateSync();
      }},
      refreshRemoteState() {{
        dashboardStateBootstrapped = false;
        return bootstrapDashboardState();
      }},
    }};
    bootstrapDashboardState();
    const savedTheme = window.localStorage.getItem(DASHBOARD_THEME_KEY);
    applyTheme(savedTheme || 'light');
    const savedView = window.localStorage.getItem(DASHBOARD_VIEW_KEY);
    const allowedViews = new Set(['market-view', 'institution-view', 'report-view', 'social-view']);
    setActiveView(allowedViews.has(savedView) ? savedView : 'market-view');

    modalClose.addEventListener('click', closeStockModal);
    modal.addEventListener('click', event => {{
      if (event.target === modal) closeStockModal();
    }});
    reportDetailClose.addEventListener('click', closeReportDetailDrawer);
    indexModalClose.addEventListener('click', closeIndexModal);
    indexModal.addEventListener('click', event => {{
      if (event.target === indexModal) closeIndexModal();
    }});
    window.addEventListener('keydown', event => {{
      if (event.key === 'Escape') {{
        closeStockModal();
        closeIndexModal();
        closeReportDetailDrawer();
      }}
    }});
    window.addEventListener('resize', () => modalChart.resize());
  </script>
</body>
</html>
"""


def hydrate_cached_dataset(dataset: list[dict]) -> list[dict]:
    for item in dataset:
        code = item.get("code", "")
        item["mainBusiness"] = resolve_main_business(
            code,
            industry=item.get("industry", ""),
            existing=item.get("mainBusiness", ""),
            business_segments=item.get("businessSegments"),
        )
        item["research"] = build_research_payload(code, item.get("research", {}))
        item["orderBook"] = item.get("orderBook") or {"time": "", "asks": [], "bids": []}
        item["peRatio"] = item.get("peRatio")
        item["marginFinancing"] = item.get("marginFinancing") or {
            "date": "",
            "finBalance": None,
            "loanBalance": None,
            "finBuyAmount": None,
        }
        item["topHolders"] = item.get("topHolders") or {"reportDate": "", "totalRatio": None, "holders": []}
        item["businessSegments"] = item.get("businessSegments") or {"reportDate": "", "category": "", "items": []}
        item["topCustomers"] = item.get("topCustomers") or {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []}
        item["latestNews"] = item.get("latestNews") or {"time": "", "summary": "", "title": "", "link": ""}
        holder_payload = item["topHolders"]
        if holder_payload.get("totalRatio") is None:
            ratio_sum = 0.0
            ratio_found = False
            for holder_line in holder_payload.get("holders", []):
                match = re.search(r"\(([-+]?\d+(?:\.\d+)?)%\)", str(holder_line))
                if not match:
                    continue
                ratio_sum += float(match.group(1))
                ratio_found = True
            if ratio_found:
                holder_payload["totalRatio"] = round(ratio_sum, 2)
        if item.get("turnoverRate") is None:
            latest_close = item.get("latestClose") or 0
            float_mcap = item.get("floatMarketCap") or 0
            today_volume = item.get("todayVolume") or 0
            free_float_shares = (float_mcap / latest_close) if latest_close else 0
            item["turnoverRate"] = (today_volume / free_float_shares * 100.0) if free_float_shares else None
        kline_amount_by_date = {}
        for row in item.get("kline", []):
            if len(row) >= 7:
                kline_amount_by_date[row[0]] = row[6]

        last5 = item.get("last5", [])
        for row in last5:
            amount = row.get("amount")
            if amount in (None, "", "NaN") or (isinstance(amount, float) and amount != amount):
                row["amount"] = kline_amount_by_date.get(row.get("date"), 0)

        if not item.get("todayAmount") and last5:
            item["todayAmount"] = last5[-1].get("amount", 0)
    return dataset


def hydrate_cached_strong_stocks(rows: list[dict]) -> list[dict]:
    for item in rows:
        code = item.get("code", "")
        industry = item.get("industry") or fetch_industry(code) or ""
        latest_news = item.get("latestNews") or {"time": "", "summary": "", "title": "", "link": ""}
        item["mainBusiness"] = resolve_main_business(
            code,
            industry=industry,
            existing=item.get("mainBusiness", ""),
            business_segments=item.get("businessSegments"),
            news_text=" ".join(
                [
                    latest_news.get("title", ""),
                    latest_news.get("summary", ""),
                ]
            ),
        )
        item["latestNews"] = latest_news
        item["research"] = build_research_payload(code, item.get("research", {}))
        item["peRatio"] = item.get("peRatio")
        item["marginFinancing"] = item.get("marginFinancing") or {
            "date": "",
            "finBalance": None,
            "loanBalance": None,
            "finBuyAmount": None,
        }
        item["topHolders"] = item.get("topHolders") or {"reportDate": "", "totalRatio": None, "holders": []}
        item["businessSegments"] = item.get("businessSegments") or {"reportDate": "", "category": "", "items": []}
        item["topCustomers"] = item.get("topCustomers") or {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []}
        item["kline"] = item.get("kline") or []
        item["last5"] = item.get("last5") or []
    return rows


def merge_strong_stock_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in secondary + primary:
        code = row.get("code")
        if not code:
            continue
        existing = merged.get(code)
        if not existing:
            merged[code] = dict(row)
            continue
        combined = dict(existing)
        combined.update({k: v for k, v in row.items() if v not in (None, "", [], {})})
        if (row.get("todayAmount") or 0) > (existing.get("todayAmount") or 0):
            combined["todayAmount"] = row.get("todayAmount")
        if (row.get("totalMarketCap") or 0) > (existing.get("totalMarketCap") or 0):
            combined["totalMarketCap"] = row.get("totalMarketCap")
        if (row.get("floatMarketCap") or 0) > (existing.get("floatMarketCap") or 0):
            combined["floatMarketCap"] = row.get("floatMarketCap")
        merged[code] = combined
    return list(merged.values())


def backfill_market_caps(rows: list[dict]) -> list[dict]:
    for item in rows:
        total_cap = float(item.get("totalMarketCap") or 0.0)
        float_cap = float(item.get("floatMarketCap") or 0.0)
        if total_cap <= 0 and float_cap > 0:
            item["totalMarketCap"] = float_cap
    return rows


def sort_watchlist_dataset(dataset: list[dict]) -> list[dict]:
    return sorted(dataset, key=lambda item: float(item.get("floatMarketCap") or 0), reverse=True)


def supplement_strong_stocks_from_watchlist(dataset: list[dict], strong_stocks: list[dict]) -> list[dict]:
    if strong_stocks:
        return strong_stocks
    watch_codes = {row["code"] for row in dataset}
    supplemental: list[dict] = []
    for item in backfill_market_caps(dataset):
        row = dict(item)
        row["latestNews"] = row.get("latestNews") or {"time": "", "summary": "", "title": "", "link": "", "isRecent": False}
        if strong_stock_passes_final_filters(row):
            supplemental.append(row)
    return finalize_strong_stocks(supplemental, watch_codes)


def quick_refresh_dashboard(as_of: date) -> tuple[Path, Path, Path]:
    out_dir = OUT_DIR
    prev_date = as_of - timedelta(days=1)
    prev_watch_path = out_dir / f"watchlist_dashboard_{prev_date.isoformat()}.json"
    if prev_watch_path.exists():
        prev_watch = json.loads(prev_watch_path.read_text(encoding="utf-8"))
    else:
        prev_watch = build_dataset()

    access_token = get_access_token()
    watch_codes = [row["code"] for row in prev_watch]
    history_map = fetch_history(access_token, watch_codes)
    basic_map = fetch_basic(access_token, watch_codes)
    realtime_map = fetch_realtime_map(access_token, watch_codes, as_of)
    pe_map = fetch_pe_ratios(access_token, watch_codes)
    margin_map = fetch_margin_financing_map(watch_codes)

    watch = []
    for item in prev_watch:
        row = dict(item)
        code = row["code"]
        history = history_map.get(code)
        basic = basic_map.get(code, {})
        realtime = realtime_map.get(code) or {}
        if history:
            table = history.get("table") or {}
            times = (history.get("time") or [])[-30:]
            open_list = (table.get("open") or [])[-30:]
            high_list = (table.get("high") or [])[-30:]
            low_list = (table.get("low") or [])[-30:]
            close_list = (table.get("close") or [])[-30:]
            volume_list = (table.get("volume") or [])[-30:]
            amount_list = (table.get("amount") or [])[-30:]
            if times and close_list and len(times) == len(close_list):
                close_field = basic.get("ths_close_price_stock") or []
                total_field = basic.get("ths_total_shares_stock") or []
                float_field = basic.get("ths_free_float_shares_stock") or []
                latest_close = float((close_field[0] if close_field else close_list[-1]) or close_list[-1])
                total_shares = float((total_field[0] if total_field else 0) or 0)
                free_float_shares = float((float_field[0] if float_field else 0) or 0)
                row["latestClose"] = latest_close
                row["todayVolume"] = volume_list[-1]
                row["todayAmount"] = amount_list[-1]
                row["turnoverRate"] = (volume_list[-1] / free_float_shares * 100.0) if free_float_shares else None
                row["todayPct"] = None if len(close_list) < 2 or not close_list[-2] else (close_list[-1] / close_list[-2] - 1.0) * 100.0
                row["totalMarketCap"] = total_shares * latest_close
                row["floatMarketCap"] = free_float_shares * latest_close
                row["kline"] = [
                    [times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]]
                    for i in range(len(times))
                ]
                prev = None
                last5 = []
                for i in range(max(0, len(times) - 5), len(times)):
                    pct = None if prev is None else (close_list[i] / prev - 1.0) * 100.0
                    last5.append(
                        {
                            "date": times[i],
                            "close": close_list[i],
                            "volume": volume_list[i],
                            "amount": amount_list[i],
                            "pct": pct,
                        }
                    )
                    prev = close_list[i]
                row["last5"] = last5
        close_field = basic.get("ths_close_price_stock") or []
        total_field = basic.get("ths_total_shares_stock") or []
        float_field = basic.get("ths_free_float_shares_stock") or []
        rt_latest = float(((realtime.get("latest") or [0])[-1]) or 0) if realtime.get("latest") else None
        rt_volume_hands = float(((realtime.get("volume") or [0])[-1]) or 0) if realtime.get("volume") else 0.0
        rt_amount = float(((realtime.get("amount") or [0])[-1]) or 0) if realtime.get("amount") else 0.0
        rt_pct = to_float(((realtime.get("changeRatio") or [None])[-1])) if realtime.get("changeRatio") else None
        total_shares = float((total_field[0] if total_field else 0) or 0)
        free_float_shares = float((float_field[0] if float_field else 0) or 0)
        latest_close = float((close_field[0] if close_field else 0) or 0)
        if rt_latest:
            latest_close = rt_latest
            row["latestClose"] = latest_close
        if rt_amount:
            row["todayAmount"] = rt_amount
        if rt_pct is not None:
            row["todayPct"] = rt_pct
        if rt_volume_hands:
            row["todayVolume"] = rt_volume_hands * 100.0
        if free_float_shares and rt_volume_hands:
            row["turnoverRate"] = rt_volume_hands * 100.0 / free_float_shares * 100.0
        if total_shares and latest_close:
            row["totalMarketCap"] = total_shares * latest_close
        if free_float_shares and latest_close:
            row["floatMarketCap"] = free_float_shares * latest_close
        fallback_total_cap = fetch_total_market_cap(code)
        if fallback_total_cap:
            row["totalMarketCap"] = fallback_total_cap
        elif row.get("floatMarketCap"):
            row["totalMarketCap"] = row["floatMarketCap"]
        row["peRatio"] = pe_map.get(code)
        row["marginFinancing"] = margin_map.get(code, {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None})
        watch.append(row)
    watch = sort_watchlist_dataset(backfill_market_caps(watch))

    resolved_trade_date, candidates = resolve_strong_stock_candidates(as_of)
    strong_codes = [row["code"] for row in candidates]
    strong = []
    if strong_codes:
        history_map = fetch_history(access_token, strong_codes)
        basic_map = fetch_basic(access_token, strong_codes)
        try:
            strong_pe_map = fetch_pe_ratios(access_token, strong_codes)
        except Exception:
            strong_pe_map = {}
        prev_strong_path = out_dir / f"watchlist_strong_stocks_{resolved_trade_date.isoformat()}.json"
        prev_strong_map = {}
        if prev_strong_path.exists():
            prev_strong_map = {row["code"]: row for row in json.loads(prev_strong_path.read_text(encoding="utf-8"))}
        news_map = fetch_latest_news_map([(row["name"], row["code"]) for row in candidates])

        for candidate in candidates:
            code = candidate["code"]
            history = history_map.get(code)
            table = (history or {}).get("table") or {}
            times = ((history or {}).get("time") or [])[-30:]
            open_list = (table.get("open") or [])[-30:]
            high_list = (table.get("high") or [])[-30:]
            low_list = (table.get("low") or [])[-30:]
            close_list = (table.get("close") or [])[-30:]
            volume_list = (table.get("volume") or [])[-30:]
            amount_list = (table.get("amount") or [])[-30:]
            basic = basic_map.get(code, {})
            close_field = basic.get("ths_close_price_stock") or []
            total_field = basic.get("ths_total_shares_stock") or []
            float_field = basic.get("ths_free_float_shares_stock") or []
            snap = fetch_quote_snapshot(code)
            latest_close = float(
                (close_field[0] if close_field else None)
                or (close_list[-1] if close_list else None)
                or snap.get("latestClose")
                or 0.0
            )
            total_shares = float((total_field[0] if total_field else 0) or 0)
            free_float_shares = float((float_field[0] if float_field else 0) or 0)
            total_cap = total_shares * latest_close if total_shares else (snap.get("totalMarketCap") or 0.0)
            float_cap = free_float_shares * latest_close if free_float_shares else (snap.get("floatMarketCap") or 0.0)
            if total_cap <= 0 and float_cap > 0:
                total_cap = float_cap
            today_amount = amount_list[-1] if amount_list else (snap.get("todayAmount") or 0.0)
            turnover_rate = (
                (volume_list[-1] / free_float_shares * 100.0) if volume_list and free_float_shares else snap.get("turnoverRate")
            )
            prev_row = prev_strong_map.get(code, {})
            industry = prev_row.get("industry") or fetch_industry(code) or ""
            latest_news = news_map.get(code) or prev_row.get("latestNews") or {"time": "", "summary": "", "title": "", "link": ""}
            row = {
                "code": code,
                "name": candidate["name"],
                "industry": industry,
                "mainBusiness": resolve_main_business(
                    code,
                    industry=industry,
                    existing=prev_row.get("mainBusiness"),
                    business_segments=prev_row.get("businessSegments"),
                    news_text=" ".join(
                        [
                            latest_news.get("title", ""),
                            latest_news.get("summary", ""),
                        ]
                    ),
                ),
                "latestClose": latest_close,
                "totalMarketCap": total_cap,
                "floatMarketCap": float_cap,
                "todayAmount": today_amount,
                "turnoverRate": turnover_rate,
                "todayPct": candidate["todayPct"],
                "latestNews": latest_news,
                "research": empty_research_payload(),
                "peRatio": strong_pe_map.get(code) if strong_pe_map.get(code) is not None else prev_row.get("peRatio"),
                "marginFinancing": {"date": "", "finBalance": None, "loanBalance": None, "finBuyAmount": None},
                "topHolders": {"reportDate": "", "totalRatio": None, "holders": []},
                "businessSegments": {"reportDate": "", "category": "", "items": []},
                "topCustomers": {"reportDate": "", "totalAmount": None, "totalRatio": None, "customers": []},
                "kline": [
                    [times[i], open_list[i], close_list[i], low_list[i], high_list[i], volume_list[i], amount_list[i]]
                    for i in range(len(times))
                ] if times and close_list and len(times) == len(close_list) else [],
                "last5": [],
            }
            if strong_stock_passes_final_filters(row):
                strong.append(row)
    strong = finalize_strong_stocks(strong, {row["code"] for row in watch})
    strong = supplement_strong_stocks_from_watchlist(watch, strong)
    market_overview = fetch_market_overview(access_token)
    institution_holdings = fetch_institution_holdings(access_token)

    json_path = out_dir / f"watchlist_dashboard_{as_of.isoformat()}.json"
    strong_json_path = out_dir / f"watchlist_strong_stocks_{as_of.isoformat()}.json"
    institution_json_path = out_dir / f"institution_holdings_{as_of.isoformat()}.json"
    html_path = out_dir / f"watchlist_dashboard_{as_of.isoformat()}.html"
    json_path.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_json_path.write_text(json.dumps(strong, ensure_ascii=False, indent=2), encoding="utf-8")
    institution_json_path.write_text(json.dumps(institution_holdings, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(build_html(watch, strong, market_overview, institution_holdings), encoding="utf-8")
    return html_path, json_path, strong_json_path


def main() -> int:
    json_path = OUT_DIR / f"watchlist_dashboard_{AS_OF.isoformat()}.json"
    strong_json_path = OUT_DIR / f"watchlist_strong_stocks_{AS_OF.isoformat()}.json"
    access_token = None
    try:
        dataset = build_dataset()
    except Exception:
        if not json_path.exists():
            raise
        dataset = json.loads(json_path.read_text(encoding="utf-8"))
        dataset = hydrate_cached_dataset(dataset)

    dataset = sort_watchlist_dataset(backfill_market_caps(dataset))

    try:
        strong_stocks = fetch_strong_stocks(AS_OF)
    except Exception:
        if not strong_json_path.exists():
            raise
        strong_stocks = json.loads(strong_json_path.read_text(encoding="utf-8"))
        strong_stocks = hydrate_cached_strong_stocks(strong_stocks)
    else:
        try:
            access_token = get_access_token()
            ifind_strong_stocks = fetch_strong_stocks_via_ifind(access_token, AS_OF)
            strong_stocks = merge_strong_stock_rows(ifind_strong_stocks, strong_stocks)
            strong_stocks = enrich_strong_stocks_for_modal(access_token, strong_stocks)
        except Exception:
            strong_stocks = hydrate_cached_strong_stocks(strong_stocks)

    all_news_targets: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for row in dataset + strong_stocks:
        code = row["code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)
        all_news_targets.append((row["name"], code))
    news_map = fetch_latest_news_map(all_news_targets)
    for row in dataset:
        row["latestNews"] = news_map.get(row["code"], {"time": "", "summary": "", "title": "", "link": ""})
        row["mainBusiness"] = resolve_main_business(
            row["code"],
            industry=row.get("industry"),
            existing=row.get("mainBusiness"),
            business_segments=row.get("businessSegments"),
            news_text=" ".join(
                [
                    row["latestNews"].get("title", ""),
                    row["latestNews"].get("summary", ""),
                ]
            ),
        )
    for row in strong_stocks:
        row["latestNews"] = news_map.get(row["code"], {"time": "", "summary": "", "title": "", "link": ""})
        row["mainBusiness"] = resolve_main_business(
            row["code"],
            industry=row.get("industry"),
            existing=row.get("mainBusiness"),
            business_segments=row.get("businessSegments"),
            news_text=" ".join(
                [
                    row["latestNews"].get("title", ""),
                    row["latestNews"].get("summary", ""),
                ]
            ),
        )

    strong_stocks = backfill_market_caps(strong_stocks)
    strong_stocks = finalize_strong_stocks(strong_stocks, {row["code"] for row in dataset})
    strong_stocks = supplement_strong_stocks_from_watchlist(dataset, strong_stocks)
    if access_token is None:
        try:
            access_token = get_access_token()
        except Exception:
            access_token = None
    if access_token is not None:
        try:
            strong_stocks = backfill_pe_ratios(access_token, strong_stocks)
        except Exception:
            pass
    market_overview = fetch_market_overview(access_token)
    institution_holdings = fetch_institution_holdings(access_token)

    json_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_json_path.write_text(json.dumps(strong_stocks, ensure_ascii=False, indent=2), encoding="utf-8")
    institution_json_path = OUT_DIR / f"institution_holdings_{AS_OF.isoformat()}.json"
    institution_json_path.write_text(json.dumps(institution_holdings, ensure_ascii=False, indent=2), encoding="utf-8")

    html = build_html(dataset, strong_stocks, market_overview, institution_holdings)
    html_path = OUT_DIR / f"watchlist_dashboard_{AS_OF.isoformat()}.html"
    html_path.write_text(html, encoding="utf-8")

    print(html_path)
    print(json_path)
    print(strong_json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
