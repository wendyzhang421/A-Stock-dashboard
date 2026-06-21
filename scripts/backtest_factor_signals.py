#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_factor_table import build_factor_rows, load_json_list, normalize_kline, to_float  # noqa: E402


REPORTS_DIR = ROOT / "reports"
HORIZONS = (5, 10, 20)
TOP_NS = (10, 20)
GROUP_COUNT = 5
FACTOR_COLUMNS = (
    "final_score_v0",
    "trend_relative_score_35",
    "quality_score_15",
    "research_score_25",
    "theme_score_20",
    "entry_timing_score_5",
    "risk_penalty",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest local factor tables against future local kline returns.")
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output-labels", default="")
    parser.add_argument("--output-summary-csv", default="")
    parser.add_argument("--output-summary-md", default="")
    return parser.parse_args()


def parse_date_from_report(path: Path, prefix: str) -> date | None:
    stem = path.stem
    if not stem.startswith(prefix + "_"):
        return None
    try:
        return date.fromisoformat(stem.removeprefix(prefix + "_"))
    except ValueError:
        return None


def available_signal_dates(reports_dir: Path, start: date | None, end: date | None) -> list[date]:
    dates = []
    for path in reports_dir.glob("watchlist_dashboard_*.json"):
        parsed = parse_date_from_report(path, "watchlist_dashboard")
        if parsed is None:
            continue
        if start and parsed < start:
            continue
        if end and parsed > end:
            continue
        dates.append(parsed)
    return sorted(set(dates))


def build_price_index(reports_dir: Path) -> dict[str, dict[date, float]]:
    prices: dict[str, dict[date, float]] = defaultdict(dict)
    for prefix in ("watchlist_dashboard", "watchlist_strong_stocks"):
        for path in sorted(reports_dir.glob(f"{prefix}_*.json")):
            report_date = parse_date_from_report(path, prefix)
            rows = load_json_list(path)
            for row in rows:
                code = str(row.get("code") or "").strip()
                if not code:
                    continue
                for bar in normalize_kline(row.get("kline")):
                    try:
                        bar_date = date.fromisoformat(str(bar["date"]))
                    except ValueError:
                        continue
                    close = to_float(bar.get("close"))
                    if close is not None and close > 0:
                        prices[code][bar_date] = close
                latest = to_float(row.get("latestClose"))
                if report_date and latest is not None and latest > 0:
                    prices[code][report_date] = latest
    return prices


def forward_label(
    code: str,
    signal_date: date,
    base_close: float | None,
    price_index: dict[str, dict[date, float]],
    horizon: int,
) -> dict[str, Any]:
    by_date = price_index.get(code) or {}
    if base_close is None or base_close <= 0:
        base_close = by_date.get(signal_date)
    if base_close is None or base_close <= 0:
        return {f"fwd_ret_{horizon}d": None, f"fwd_max_drawdown_{horizon}d": None, f"fwd_date_{horizon}d": ""}
    future_dates = sorted(d for d in by_date if d > signal_date)
    if len(future_dates) < horizon:
        return {f"fwd_ret_{horizon}d": None, f"fwd_max_drawdown_{horizon}d": None, f"fwd_date_{horizon}d": ""}
    target_date = future_dates[horizon - 1]
    window = future_dates[:horizon]
    target_close = by_date[target_date]
    returns = [(by_date[d] / base_close - 1.0) * 100.0 for d in window if by_date.get(d)]
    max_dd = min(returns) if returns else None
    return {
        f"fwd_ret_{horizon}d": (target_close / base_close - 1.0) * 100.0,
        f"fwd_max_drawdown_{horizon}d": max_dd,
        f"fwd_date_{horizon}d": target_date.isoformat(),
    }


def rank_values(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda idx: values[idx])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    den_y = sum((y - mean_y) ** 2 for y in ys)
    if den_x <= 0 or den_y <= 0:
        return None
    return num / math.sqrt(den_x * den_y)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rank_values(xs), rank_values(ys))


def safe_mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.fmean(clean) if clean else None


def win_rate(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(1 for v in clean if v > 0) / len(clean) * 100.0


def max_loss(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return min(clean) if clean else None


def percentile_groups(rows: list[dict[str, Any]], score_key: str) -> dict[int, list[dict[str, Any]]]:
    sorted_rows = sorted(rows, key=lambda row: to_float(row.get(score_key)) or -1e9, reverse=True)
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not sorted_rows:
        return groups
    for idx, row in enumerate(sorted_rows):
        group = min(GROUP_COUNT, int(idx * GROUP_COUNT / len(sorted_rows)) + 1)
        groups[group].append(row)
    return groups


def fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def build_backtest(reports_dir: Path, start: date | None, end: date | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    dates = available_signal_dates(reports_dir, start, end)
    price_index = build_price_index(reports_dir)
    labeled: list[dict[str, Any]] = []
    per_date: dict[date, list[dict[str, Any]]] = {}

    for signal_date in dates:
        rows, _ = build_factor_rows(reports_dir, signal_date, skip_index_fetch=True)
        day_rows: list[dict[str, Any]] = []
        for row in rows:
            code = row["code"]
            base_close = to_float(row.get("latest_close")) or (price_index.get(code) or {}).get(signal_date)
            item = {
                **row,
                "as_of": signal_date.isoformat(),
                "base_close": base_close,
                "risk_flags": "|".join(row.get("risk_flags") or []) if isinstance(row.get("risk_flags"), list) else row.get("risk_flags", ""),
            }
            for horizon in HORIZONS:
                item.update(forward_label(code, signal_date, base_close, price_index, horizon))
            labeled.append(item)
            day_rows.append(item)
        per_date[signal_date] = day_rows

    summary: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        ret_key = f"fwd_ret_{horizon}d"
        dd_key = f"fwd_max_drawdown_{horizon}d"
        for top_n in TOP_NS:
            picks: list[dict[str, Any]] = []
            for day_rows in per_date.values():
                eligible = [row for row in day_rows if row.get(ret_key) is not None]
                eligible.sort(key=lambda row: to_float(row.get("final_score_v0")) or -1e9, reverse=True)
                picks.extend(eligible[:top_n])
            returns = [float(row[ret_key]) for row in picks if row.get(ret_key) is not None]
            drawdowns = [float(row[dd_key]) for row in picks if row.get(dd_key) is not None]
            summary.append(
                {
                    "section": "topn",
                    "metric": f"top{top_n}",
                    "horizon": horizon,
                    "observations": len(returns),
                    "avg_return_pct": safe_mean(returns),
                    "win_rate_pct": win_rate(returns),
                    "avg_max_drawdown_pct": safe_mean(drawdowns),
                    "worst_return_pct": max_loss(returns),
                }
            )

        grouped_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for day_rows in per_date.values():
            eligible = [row for row in day_rows if row.get(ret_key) is not None]
            for group, group_rows in percentile_groups(eligible, "final_score_v0").items():
                grouped_rows[group].extend(group_rows)
        for group, group_rows in sorted(grouped_rows.items()):
            returns = [float(row[ret_key]) for row in group_rows if row.get(ret_key) is not None]
            summary.append(
                {
                    "section": "score_group",
                    "metric": f"group{group}",
                    "horizon": horizon,
                    "observations": len(returns),
                    "avg_return_pct": safe_mean(returns),
                    "win_rate_pct": win_rate(returns),
                    "avg_max_drawdown_pct": None,
                    "worst_return_pct": max_loss(returns),
                }
            )

        for factor in FACTOR_COLUMNS:
            daily_ics: list[float] = []
            for day_rows in per_date.values():
                pairs = [
                    (to_float(row.get(factor)), to_float(row.get(ret_key)))
                    for row in day_rows
                    if row.get(ret_key) is not None and to_float(row.get(factor)) is not None
                ]
                if len(pairs) < 5:
                    continue
                xs = [pair[0] for pair in pairs if pair[0] is not None and pair[1] is not None]
                ys = [pair[1] for pair in pairs if pair[0] is not None and pair[1] is not None]
                ic = spearman(xs, ys)
                if ic is not None:
                    daily_ics.append(ic)
            summary.append(
                {
                    "section": "rank_ic",
                    "metric": factor,
                    "horizon": horizon,
                    "observations": len(daily_ics),
                    "avg_return_pct": safe_mean(daily_ics),
                    "win_rate_pct": win_rate(daily_ics),
                    "avg_max_drawdown_pct": None,
                    "worst_return_pct": min(daily_ics) if daily_ics else None,
                }
            )

    markdown = render_markdown(summary, labeled, dates)
    return labeled, summary, markdown


def render_markdown(summary: list[dict[str, Any]], labeled: list[dict[str, Any]], dates: list[date]) -> str:
    lines = [
        "# 因子信号离线回测摘要",
        "",
        f"- 信号日期数：{len(dates)}",
        f"- 信号记录数：{len(labeled)}",
        f"- 日期范围：{dates[0].isoformat() if dates else '-'} 至 {dates[-1].isoformat() if dates else '-'}",
        "- 数据来源：本地 `watchlist_dashboard_*.json` 与 `watchlist_strong_stocks_*.json` 聚合 K 线",
        "- 说明：第一版为横截面验证，不模拟真实组合换仓、涨跌停成交、滑点和手续费。",
        "",
        "## Top N 表现",
        "",
        "| 组合 | 周期 | 样本 | 平均收益 | 胜率 | 平均持有期最大回撤 | 最差收益 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        if row["section"] != "topn":
            continue
        lines.append(
            "| {metric} | T+{horizon} | {obs} | {avg}% | {win}% | {dd}% | {worst}% |".format(
                metric=row["metric"],
                horizon=row["horizon"],
                obs=row["observations"],
                avg=fmt(row["avg_return_pct"]),
                win=fmt(row["win_rate_pct"]),
                dd=fmt(row["avg_max_drawdown_pct"]),
                worst=fmt(row["worst_return_pct"]),
            )
        )

    lines += [
        "",
        "## 分组收益",
        "",
        "| 分组 | 周期 | 样本 | 平均收益 | 胜率 | 最差收益 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        if row["section"] != "score_group":
            continue
        lines.append(
            "| {metric} | T+{horizon} | {obs} | {avg}% | {win}% | {worst}% |".format(
                metric=row["metric"],
                horizon=row["horizon"],
                obs=row["observations"],
                avg=fmt(row["avg_return_pct"]),
                win=fmt(row["win_rate_pct"]),
                worst=fmt(row["worst_return_pct"]),
            )
        )

    lines += [
        "",
        "## Rank IC",
        "",
        "| 因子 | 周期 | 日数 | 平均 Rank IC | 正 IC 比例 | 最差日 IC |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        if row["section"] != "rank_ic":
            continue
        lines.append(
            "| {metric} | T+{horizon} | {obs} | {avg} | {win}% | {worst} |".format(
                metric=row["metric"],
                horizon=row["horizon"],
                obs=row["observations"],
                avg=fmt(row["avg_return_pct"], 4),
                win=fmt(row["win_rate_pct"]),
                worst=fmt(row["worst_return_pct"], 4),
            )
        )
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    args = parse_args()
    reports_dir = Path(args.reports_dir).resolve()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    labels_path = Path(args.output_labels).resolve() if args.output_labels else reports_dir / "factor_backtest_labels.csv"
    summary_csv_path = (
        Path(args.output_summary_csv).resolve() if args.output_summary_csv else reports_dir / "factor_backtest_summary.csv"
    )
    summary_md_path = (
        Path(args.output_summary_md).resolve() if args.output_summary_md else reports_dir / "factor_backtest_summary.md"
    )
    labeled, summary, markdown = build_backtest(reports_dir, start, end)
    write_csv(labels_path, labeled)
    write_csv(summary_csv_path, summary)
    summary_md_path.write_text(markdown, encoding="utf-8")
    print(f"signals={len(labeled)}")
    print(f"labels={labels_path}")
    print(f"summary_csv={summary_csv_path}")
    print(f"summary_md={summary_md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
