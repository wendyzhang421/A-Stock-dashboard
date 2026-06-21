#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = ROOT / "reports"
INDEX_CODE = "000300.SH"
RISK_NEWS_KEYWORDS = (
    "减持",
    "问询",
    "监管",
    "风险提示",
    "立案",
    "处罚",
    "退市",
    "异常波动",
)
THEME_KEYWORDS = (
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
    "半导体",
    "芯片",
    "风电",
    "锂电",
    "电池",
    "自动化",
    "激光",
    "光学",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a medium-horizon factor table from local AStock reports.")
    parser.add_argument("--as-of", default="", help="Trade date, e.g. 2026-06-05. Defaults to latest local report.")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument("--output-csv", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--skip-index-fetch", action="store_true", help="Do not fetch HS300 public kline for relative strength.")
    return parser.parse_args()


def to_float(value: Any) -> float | None:
    if value in (None, "", "-", "NaN"):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def parse_pct_text(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return to_float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return to_float(match.group(0)) if match else None


def parse_report_date_from_name(path: Path, prefix: str) -> date | None:
    stem = path.stem
    if not stem.startswith(prefix + "_"):
        return None
    try:
        return date.fromisoformat(stem.removeprefix(prefix + "_"))
    except ValueError:
        return None


def latest_report_path(reports_dir: Path, prefix: str, as_of: date | None) -> tuple[date, Path]:
    candidates: list[tuple[date, Path]] = []
    for path in reports_dir.glob(f"{prefix}_*.json"):
        parsed = parse_report_date_from_name(path, prefix)
        if parsed is None:
            continue
        if as_of is None or parsed <= as_of:
            candidates.append((parsed, path))
    if not candidates:
        raise FileNotFoundError(f"No {prefix}_*.json reports found in {reports_dir}")
    return max(candidates, key=lambda item: item[0])


def load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def normalize_kline(rows: Any) -> list[dict[str, float | str]]:
    out: list[dict[str, float | str]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        open_px = to_float(row[1])
        close_px = to_float(row[2])
        low_px = to_float(row[3])
        high_px = to_float(row[4])
        volume = to_float(row[5])
        amount = to_float(row[6])
        if open_px is None or close_px is None or low_px is None or high_px is None:
            continue
        out.append(
            {
                "date": str(row[0]),
                "open": open_px,
                "close": close_px,
                "low": low_px,
                "high": high_px,
                "volume": volume or 0.0,
                "amount": amount or 0.0,
            }
        )
    return out


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def pct_change(values: list[float], periods: int) -> float | None:
    if len(values) <= periods:
        return None
    base = values[-periods - 1]
    if not base:
        return None
    return (values[-1] / base - 1.0) * 100.0


def max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def volatility(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev:
            returns.append(cur / prev - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252.0) * 100.0


def fetch_public_index_kline(as_of: date) -> list[dict[str, float | str]]:
    raw = INDEX_CODE.split(".")[0]
    secid = f"1.{raw}"
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
        "fqt": "1",
        "beg": "20200101",
        "end": as_of.strftime("%Y%m%d"),
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        resp.raise_for_status()
        rows = ((resp.json().get("data") or {}).get("klines") or [])[-80:]
    except Exception:
        rows = []
    if rows:
        kline = []
        for row in rows:
            parts = str(row).split(",")
            if len(parts) < 7:
                continue
            kline.append([parts[0], parts[1], parts[2], parts[4], parts[3], parts[5], parts[6]])
        parsed = normalize_kline(kline)
        if parsed:
            return parsed

    symbol = "sh" + raw
    try:
        resp = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,2020-01-01,{as_of.isoformat()},640,qfq"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        resp.raise_for_status()
        data = (((resp.json().get("data") or {}).get(symbol) or {}).get("qfqday") or [])[-80:]
    except Exception:
        return []
    return normalize_kline(
        [[row[0], row[1], row[2], row[4], row[3], row[5], 0.0] for row in data if isinstance(row, list) and len(row) >= 6]
    )


def infer_theme_bucket(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("mainBusiness", "industry", "name"))
    upper_text = text.upper()
    for keyword in THEME_KEYWORDS:
        if keyword.upper() in upper_text:
            return keyword
    fallback = str(row.get("industry") or row.get("mainBusiness") or "").strip()
    return fallback if fallback and fallback != "-" else "未分类"


def report_target_set(reports_dir: Path) -> tuple[set[str], dict[str, dict[str, Any]]]:
    path = reports_dir / "research_reports.json"
    targets: set[str] = set()
    meta: dict[str, dict[str, Any]] = {}
    for item in load_json_list(path):
        target = str(item.get("target") or "").strip()
        if not target:
            continue
        targets.add(target)
        meta[target] = item
    return targets, meta


def has_research_payload(row: dict[str, Any]) -> bool:
    research = row.get("research") or {}
    if not isinstance(research, dict):
        return False
    keys = ("coreLogic", "newOrders2026", "coreEdge", "revenue2025", "q1Revenue2026")
    return any(str(research.get(key) or "").strip() for key in keys)


def calc_raw_factors(row: dict[str, Any], index_returns: dict[int, float | None]) -> dict[str, Any]:
    kline = normalize_kline(row.get("kline"))
    closes = [float(item["close"]) for item in kline]
    highs = [float(item["high"]) for item in kline]
    lows = [float(item["low"]) for item in kline]
    opens = [float(item["open"]) for item in kline]
    latest = kline[-1] if kline else {}
    latest_close = to_float(row.get("latestClose")) or (closes[-1] if closes else None)
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma20_prev = None
    if len(closes) >= 30:
        ma20_prev = sum(closes[-30:-10]) / 20.0
    elif len(closes) >= 25:
        ma20_prev = sum(closes[:20]) / 20.0
    ma20_slope_10d = None
    if ma20 is not None and ma20_prev not in (None, 0):
        ma20_slope_10d = (ma20 / ma20_prev - 1.0) * 100.0
    high20_prev = max(highs[-21:-1]) if len(highs) >= 21 else (max(highs[:-1]) if len(highs) > 1 else None)
    high20_distance_pct = None
    breakout_20d = False
    if latest_close is not None and high20_prev:
        high20_distance_pct = (latest_close / high20_prev - 1.0) * 100.0
        breakout_20d = latest_close >= high20_prev

    close_position = None
    upper_shadow_ratio = None
    close_gt_open = None
    if latest:
        day_high = float(latest["high"])
        day_low = float(latest["low"])
        day_open = float(latest["open"])
        day_close = float(latest["close"])
        if day_high > day_low:
            close_position = (day_close - day_low) / (day_high - day_low)
        if day_close:
            upper_shadow_ratio = max(day_high - max(day_open, day_close), 0.0) / day_close * 100.0
        close_gt_open = day_close > day_open

    recent = kline[-20:]
    long_upper_count = 0
    large_down_count = 0
    green_count = 0
    prev_close = None
    for item in recent:
        open_px = float(item["open"])
        close_px = float(item["close"])
        high_px = float(item["high"])
        low_px = float(item["low"])
        if close_px > open_px:
            green_count += 1
        if close_px and high_px > low_px:
            upper = max(high_px - max(open_px, close_px), 0.0)
            amplitude = high_px - low_px
            if amplitude and upper / amplitude >= 0.45:
                long_upper_count += 1
        if prev_close and close_px / prev_close - 1.0 <= -0.05:
            large_down_count += 1
        prev_close = close_px

    ret5 = pct_change(closes, 5)
    ret10 = pct_change(closes, 10)
    ret20 = pct_change(closes, 20)
    rel_ret5 = ret5 - index_returns[5] if ret5 is not None and index_returns.get(5) is not None else None
    rel_ret10 = ret10 - index_returns[10] if ret10 is not None and index_returns.get(10) is not None else None
    rel_ret20 = ret20 - index_returns[20] if ret20 is not None and index_returns.get(20) is not None else None

    research = row.get("research") or {}
    latest_news = row.get("latestNews") or {}
    risk_text = " ".join(
        [
            str(row.get("name") or ""),
            str(latest_news.get("title") or ""),
            str(latest_news.get("summary") or ""),
        ]
    )
    risk_flags = [keyword for keyword in RISK_NEWS_KEYWORDS if keyword in risk_text]
    if "ST" in str(row.get("name") or "").upper():
        risk_flags.append("ST")

    return {
        "latest_kline_date": str(latest.get("date") or ""),
        "latest_close": latest_close,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "close_to_ma20": (latest_close / ma20) if latest_close is not None and ma20 else None,
        "ma20_slope_10d_pct": ma20_slope_10d,
        "ma_alignment": bool(latest_close and ma5 and ma10 and ma20 and latest_close > ma5 > ma10 > ma20),
        "ret5_pct": ret5,
        "ret10_pct": ret10,
        "ret20_pct": ret20,
        "index_ret5_pct": index_returns.get(5),
        "index_ret10_pct": index_returns.get(10),
        "index_ret20_pct": index_returns.get(20),
        "rel_ret5_pct": rel_ret5,
        "rel_ret10_pct": rel_ret10,
        "rel_ret20_pct": rel_ret20,
        "high20_distance_pct": high20_distance_pct,
        "breakout_20d": breakout_20d,
        "close_position": close_position,
        "upper_shadow_ratio_pct": upper_shadow_ratio,
        "close_gt_open": close_gt_open,
        "max_drawdown_20_pct": max_drawdown([float(item["close"]) for item in recent]),
        "volatility_20_ann_pct": volatility([float(item["close"]) for item in recent]),
        "green_ratio_20": (green_count / len(recent)) if recent else None,
        "long_upper_count_20": long_upper_count,
        "large_down_count_20": large_down_count,
        "q1_revenue_yoy_pct": parse_pct_text(research.get("q1RevenueYoY2026")),
        "q1_net_profit_yoy_pct": parse_pct_text(research.get("q1NetProfitYoY2026")),
        "risk_flags": sorted(set(risk_flags)),
    }


def score_row(row: dict[str, Any]) -> dict[str, float]:
    trend = 0.0
    close_to_ma20 = to_float(row.get("close_to_ma20"))
    if close_to_ma20 is not None:
        if close_to_ma20 >= 1.08:
            trend += 5
        elif close_to_ma20 >= 1.03:
            trend += 4
        elif close_to_ma20 >= 1.0:
            trend += 2
    slope = to_float(row.get("ma20_slope_10d_pct"))
    if slope is not None:
        trend += 5 if slope >= 5 else 4 if slope > 1 else 2 if slope > 0 else 0
    if row.get("ma_alignment"):
        trend += 5
    distance = to_float(row.get("high20_distance_pct"))
    if row.get("breakout_20d"):
        trend += 6
    elif distance is not None:
        trend += 4 if distance >= -3 else 2 if distance >= -8 else 0
    ret20 = to_float(row.get("ret20_pct"))
    if ret20 is not None and ret20 > 0:
        trend += min(4, ret20 / 6)
    trend = min(trend, 25.0)

    relative_values_found = False
    relative = 0.0
    for key, weight in (("rel_ret5_pct", 3), ("rel_ret10_pct", 3), ("rel_ret20_pct", 4)):
        value = to_float(row.get(key))
        if value is None:
            continue
        relative_values_found = True
        relative += weight if value >= 5 else weight * 0.7 if value > 2 else weight * 0.4 if value > 0 else 0
    relative_score = min(relative, 10.0) if relative_values_found else None
    trend_and_relative = min(trend + (relative_score or 0.0), 35.0)

    quality = 0.0
    drawdown = to_float(row.get("max_drawdown_20_pct"))
    if drawdown is not None:
        quality += 5 if drawdown >= -8 else 3 if drawdown >= -15 else 1 if drawdown >= -25 else 0
    vol = to_float(row.get("volatility_20_ann_pct"))
    if vol is not None:
        quality += 4 if vol <= 45 else 3 if vol <= 70 else 1 if vol <= 100 else 0
    green_ratio = to_float(row.get("green_ratio_20"))
    if green_ratio is not None:
        quality += 4 if green_ratio >= 0.6 else 2 if green_ratio >= 0.5 else 0
    quality += max(0.0, 2.0 - float(row.get("long_upper_count_20") or 0) * 0.5)
    quality += max(0.0, 2.0 - float(row.get("large_down_count_20") or 0) * 0.7)
    quality = min(quality, 15.0)

    research = 0.0
    if row.get("research_report_covered"):
        research += 10
    if row.get("research_payload_covered"):
        research += 5
    q1_rev = to_float(row.get("q1_revenue_yoy_pct"))
    q1_np = to_float(row.get("q1_net_profit_yoy_pct"))
    if q1_rev is not None and q1_rev > 0:
        research += 4
    if q1_np is not None and q1_np > 0:
        research += 4
    if str(row.get("mainBusiness") or "").strip() not in {"", "-"}:
        research += 2
    research = min(research, 25.0)

    theme = 0.0
    strength_count = int(row.get("theme_strength_count") or 0)
    candidate_count = int(row.get("theme_candidate_count") or 0)
    avg_ret10 = to_float(row.get("theme_avg_ret10_pct"))
    if strength_count >= 5:
        theme += 10
    elif strength_count >= 3:
        theme += 7
    elif strength_count >= 2:
        theme += 4
    if candidate_count >= 5:
        theme += 3
    if avg_ret10 is not None and avg_ret10 > 3:
        theme += 5
    elif avg_ret10 is not None and avg_ret10 > 0:
        theme += 2
    theme = min(theme, 20.0)

    entry = 0.0
    close_position = to_float(row.get("close_position"))
    if close_position is not None:
        entry += 3 if close_position >= 0.75 else 2 if close_position >= 0.55 else 0
    if row.get("close_gt_open"):
        entry += 2
    entry = min(entry, 5.0)

    penalty = 0.0
    penalty -= 6.0 * len(row.get("risk_flags") or [])
    pe = to_float(row.get("peRatio"))
    q1_np = to_float(row.get("q1_net_profit_yoy_pct"))
    if pe is not None and (pe < 0 or pe > 200) and not (q1_np is not None and q1_np > 30):
        penalty -= 5.0
    turnover = to_float(row.get("turnoverRate"))
    float_cap = to_float(row.get("floatMarketCap")) or 0.0
    if turnover is not None:
        if float_cap < 1e10 and turnover > 30:
            penalty -= 8.0
        elif float_cap < 5e10 and turnover > 25:
            penalty -= 8.0
        elif float_cap >= 5e10 and turnover > 18:
            penalty -= 8.0
    if drawdown is not None and drawdown < -25:
        penalty -= 5.0
    ret20 = to_float(row.get("ret20_pct"))
    if ret20 is not None and ret20 > 80:
        penalty -= 5.0
    upper = to_float(row.get("upper_shadow_ratio_pct"))
    if upper is not None and upper > 6:
        penalty -= 4.0

    final = trend_and_relative + quality + research + theme + entry + penalty
    return {
        "trend_relative_score_35": round(trend_and_relative, 4),
        "relative_score_10": None if relative_score is None else round(relative_score, 4),
        "quality_score_15": round(quality, 4),
        "research_score_25": round(research, 4),
        "theme_score_20": round(theme, 4),
        "entry_timing_score_5": round(entry, 4),
        "risk_penalty": round(penalty, 4),
        "final_score_v0": round(final, 4),
    }


def merge_rows(watch_rows: list[dict[str, Any]], strong_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source, rows in (("watchlist", watch_rows), ("strong", strong_rows)):
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            item = merged.setdefault(code, {})
            item.update({key: value for key, value in row.items() if value not in (None, "", [], {})})
            tags = set(item.get("_pool_tags") or [])
            tags.add(source)
            item["_pool_tags"] = sorted(tags)
    return list(merged.values())


def build_factor_rows(reports_dir: Path, as_of: date, skip_index_fetch: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    watch_date, watch_path = latest_report_path(reports_dir, "watchlist_dashboard", as_of)
    try:
        strong_date, strong_path = latest_report_path(reports_dir, "watchlist_strong_stocks", as_of)
        strong_rows = load_json_list(strong_path)
    except FileNotFoundError:
        strong_date, strong_path, strong_rows = None, None, []
    watch_rows = load_json_list(watch_path)
    combined = merge_rows(watch_rows, strong_rows)
    targets, target_meta = report_target_set(reports_dir)
    name_to_code = {str(row.get("name") or ""): str(row.get("code") or "") for row in combined}

    index_kline = [] if skip_index_fetch else fetch_public_index_kline(as_of)
    index_closes = [float(item["close"]) for item in index_kline]
    index_returns = {period: pct_change(index_closes, period) for period in (5, 10, 20)}

    rows: list[dict[str, Any]] = []
    for row in combined:
        name = str(row.get("name") or "")
        code = str(row.get("code") or "")
        raw = calc_raw_factors(row, index_returns)
        report_meta = target_meta.get(name) or {}
        report_covered = name in targets or code in targets
        pool_tags = set(row.get("_pool_tags") or [])
        if report_covered:
            pool_tags.add("research")
        item = {
            "as_of": as_of.isoformat(),
            "watch_report_date": watch_date.isoformat(),
            "strong_report_date": strong_date.isoformat() if strong_date else "",
            "code": code,
            "name": name,
            "pool_tags": "|".join(sorted(pool_tags)),
            "research_report_covered": report_covered,
            "research_report_date": str(report_meta.get("date") or ""),
            "research_payload_covered": has_research_payload(row),
            "theme_bucket": infer_theme_bucket(row),
            "totalMarketCap": to_float(row.get("totalMarketCap")),
            "floatMarketCap": to_float(row.get("floatMarketCap")),
            "todayAmount": to_float(row.get("todayAmount")),
            "turnoverRate": to_float(row.get("turnoverRate")),
            "todayPct": to_float(row.get("todayPct")),
            "peRatio": to_float(row.get("peRatio")),
            "mainBusiness": row.get("mainBusiness") or "",
            "industry": row.get("industry") or "",
            **raw,
        }
        rows.append(item)

    theme_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"candidate_count": 0, "strength_count": 0, "ret10_values": []})
    for item in rows:
        bucket = item["theme_bucket"]
        theme_stats[bucket]["candidate_count"] += 1
        if (to_float(item.get("ret10_pct")) or 0.0) > 0 and (to_float(item.get("close_to_ma20")) or 0.0) >= 1.0:
            theme_stats[bucket]["strength_count"] += 1
        ret10 = to_float(item.get("ret10_pct"))
        if ret10 is not None:
            theme_stats[bucket]["ret10_values"].append(ret10)

    for item in rows:
        stats = theme_stats[item["theme_bucket"]]
        values = stats["ret10_values"]
        item["theme_candidate_count"] = stats["candidate_count"]
        item["theme_strength_count"] = stats["strength_count"]
        item["theme_avg_ret10_pct"] = (sum(values) / len(values)) if values else None
        item.update(score_row(item))
        item["risk_flags"] = "|".join(item.get("risk_flags") or [])

    rows.sort(key=lambda item: to_float(item.get("final_score_v0")) or -999, reverse=True)
    unmapped_reports = sorted(target for target in targets if target not in name_to_code and target not in {row.get("code") for row in combined})
    metadata = {
        "as_of": as_of.isoformat(),
        "watch_report": str(watch_path),
        "strong_report": str(strong_path) if strong_path else "",
        "row_count": len(rows),
        "index_code": INDEX_CODE,
        "index_fetch_ok": bool(index_kline),
        "index_returns": index_returns,
        "unmapped_research_targets": unmapped_reports,
    }
    return rows, metadata


def format_cell(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return ""
    return value


def write_outputs(rows: list[dict[str, Any]], metadata: dict[str, Any], csv_path: Path, json_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "as_of",
        "code",
        "name",
        "pool_tags",
        "research_report_covered",
        "research_payload_covered",
        "research_report_date",
        "theme_bucket",
        "theme_candidate_count",
        "theme_strength_count",
        "theme_avg_ret10_pct",
        "totalMarketCap",
        "floatMarketCap",
        "todayAmount",
        "turnoverRate",
        "todayPct",
        "peRatio",
        "latest_kline_date",
        "latest_close",
        "ma5",
        "ma10",
        "ma20",
        "close_to_ma20",
        "ma20_slope_10d_pct",
        "ma_alignment",
        "ret5_pct",
        "ret10_pct",
        "ret20_pct",
        "index_ret5_pct",
        "index_ret10_pct",
        "index_ret20_pct",
        "rel_ret5_pct",
        "rel_ret10_pct",
        "rel_ret20_pct",
        "high20_distance_pct",
        "breakout_20d",
        "close_position",
        "upper_shadow_ratio_pct",
        "close_gt_open",
        "max_drawdown_20_pct",
        "volatility_20_ann_pct",
        "green_ratio_20",
        "long_upper_count_20",
        "large_down_count_20",
        "q1_revenue_yoy_pct",
        "q1_net_profit_yoy_pct",
        "trend_relative_score_35",
        "relative_score_10",
        "quality_score_15",
        "research_score_25",
        "theme_score_20",
        "entry_timing_score_5",
        "risk_penalty",
        "final_score_v0",
        "risk_flags",
        "mainBusiness",
        "industry",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: format_cell(row.get(field)) for field in fields})
    json_path.write_text(
        json.dumps({"metadata": metadata, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    reports_dir = Path(args.reports_dir).resolve()
    as_of = date.fromisoformat(args.as_of) if args.as_of else latest_report_path(reports_dir, "watchlist_dashboard", None)[0]
    default_csv = reports_dir / f"factor_table_{as_of.isoformat()}.csv"
    default_json = reports_dir / f"factor_table_{as_of.isoformat()}.json"
    csv_path = Path(args.output_csv).resolve() if args.output_csv else default_csv
    json_path = Path(args.output_json).resolve() if args.output_json else default_json
    rows, metadata = build_factor_rows(reports_dir, as_of, args.skip_index_fetch)
    write_outputs(rows, metadata, csv_path, json_path)
    print(f"factor_rows={len(rows)}")
    print(f"csv={csv_path}")
    print(f"json={json_path}")
    print(f"index_fetch_ok={metadata['index_fetch_ok']}")
    if metadata["unmapped_research_targets"]:
        print("unmapped_research_targets=" + ",".join(metadata["unmapped_research_targets"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
