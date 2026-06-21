#!/usr/bin/env python3
from __future__ import annotations

import csv
import re
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def grab_summary_value(path: Path, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"{re.escape(label)}：([^\n]+)", text)
    return match.group(1).strip() if match else "-"


def main() -> int:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": "1.000688",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
        "fqt": "1",
        "beg": "20260428",
        "end": "20260619",
    }
    session = requests.Session()
    session.trust_env = False
    resp = session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    klines = ((resp.json().get("data") or {}).get("klines") or [])
    rows = []
    closes = []
    prev_close = None
    up_days = 0
    daily_count = 0
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 7:
            continue
        trade_date = parts[0]
        open_px = float(parts[1])
        close_px = float(parts[2])
        high_px = float(parts[3])
        low_px = float(parts[4])
        closes.append(close_px)
        nav = close_px / closes[0]
        if prev_close is not None:
            daily_count += 1
            if close_px > prev_close:
                up_days += 1
        running_dd = max_drawdown(closes)
        rows.append(
            {
                "date": trade_date,
                "open": open_px,
                "close": close_px,
                "high": high_px,
                "low": low_px,
                "benchmark_nav": nav,
                "benchmark_return_pct": (nav - 1.0) * 100.0,
                "drawdown_pct": running_dd,
            }
        )
        prev_close = close_px

    if not rows:
        raise RuntimeError("No KC50 kline rows returned")

    REPORTS.mkdir(exist_ok=True)
    csv_path = REPORTS / "benchmark_kc50.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    total_return = (closes[-1] / closes[0] - 1.0) * 100.0
    dd = max_drawdown(closes)
    up_ratio = up_days / daily_count * 100.0 if daily_count else 0.0
    strategy_summary = REPORTS / "portfolio_summary_hold10_10.md"
    strategy_return_text = grab_summary_value(strategy_summary, "累计收益")
    strategy_dd_text = grab_summary_value(strategy_summary, "最大回撤")
    strategy_return = float(strategy_return_text.rstrip("%")) if strategy_return_text.endswith("%") else None
    strategy_dd = float(strategy_dd_text.rstrip("%")) if strategy_dd_text.endswith("%") else None
    excess = strategy_return - total_return if strategy_return is not None else None
    dd_diff = abs(strategy_dd - dd) if strategy_dd is not None else None
    dd_label = "低" if strategy_dd is not None and strategy_dd > dd else "高"

    md = "\n".join(
        [
            "# 科创50基准对照",
            "",
            f"- 区间：{rows[0]['date']} 至 {rows[-1]['date']}",
            f"- 科创50起始收盘：{closes[0]:.2f}",
            f"- 科创50期末收盘：{closes[-1]:.2f}",
            f"- 科创50累计收益：{total_return:.2f}%",
            f"- 科创50最大回撤：{dd:.2f}%",
            f"- 科创50上涨日比例：{up_ratio:.2f}%",
            "",
            "## 策略对照",
            "",
            f"- hold10_10 策略累计收益：{strategy_return_text}",
            f"- hold10_10 策略最大回撤：{strategy_dd_text}",
            f"- 超额收益：{'-' if excess is None else f'{excess:.2f}%'}",
            f"- 回撤差异：策略回撤比科创50{dd_label} {'-' if dd_diff is None else f'{dd_diff:.2f}'} 个百分点",
            "",
            "说明：基准使用东方财富科创50日 K；策略仍未模拟涨跌停不可成交、盘口冲击、真实整手和复权误差。",
            "",
        ]
    )
    md_path = REPORTS / "benchmark_kc50_summary.md"
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"csv={csv_path}")
    print(f"summary={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
