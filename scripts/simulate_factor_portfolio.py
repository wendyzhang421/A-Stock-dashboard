#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from backtest_factor_signals import available_signal_dates  # noqa: E402
from build_factor_table import build_factor_rows, load_json_list, normalize_kline, to_float  # noqa: E402


REPORTS_DIR = ROOT / "reports"


@dataclass
class Position:
    code: str
    name: str
    shares: float
    entry_date: date
    entry_price: float
    entry_score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a low-turnover factor portfolio using local AStock reports.")
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR))
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--min-hold-days", type=int, default=5)
    parser.add_argument("--max-hold-days", type=int, default=20)
    parser.add_argument("--buy-quantile", type=float, default=0.20)
    parser.add_argument("--sell-quantile", type=float, default=0.50)
    parser.add_argument("--cost-bps", type=float, default=15.0)
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--output-nav", default="")
    parser.add_argument("--output-trades", default="")
    parser.add_argument("--output-positions", default="")
    parser.add_argument("--output-summary", default="")
    return parser.parse_args()


def parse_report_date(path: Path, prefix: str) -> date | None:
    if not path.stem.startswith(prefix + "_"):
        return None
    try:
        return date.fromisoformat(path.stem.removeprefix(prefix + "_"))
    except ValueError:
        return None


def build_ohlc_index(reports_dir: Path) -> dict[str, dict[date, dict[str, float]]]:
    ohlc: dict[str, dict[date, dict[str, float]]] = defaultdict(dict)
    for prefix in ("watchlist_dashboard", "watchlist_strong_stocks"):
        for path in sorted(reports_dir.glob(f"{prefix}_*.json")):
            for row in load_json_list(path):
                code = str(row.get("code") or "").strip()
                if not code:
                    continue
                for bar in normalize_kline(row.get("kline")):
                    try:
                        bar_date = date.fromisoformat(str(bar["date"]))
                    except ValueError:
                        continue
                    values = {
                        "open": to_float(bar.get("open")),
                        "high": to_float(bar.get("high")),
                        "low": to_float(bar.get("low")),
                        "close": to_float(bar.get("close")),
                    }
                    if all(value is not None and value > 0 for value in values.values()):
                        ohlc[code][bar_date] = {key: float(value) for key, value in values.items() if value is not None}
    return ohlc


def all_trade_dates(ohlc: dict[str, dict[date, dict[str, float]]]) -> list[date]:
    dates = sorted({day for by_day in ohlc.values() for day in by_day if day.weekday() < 5})
    return dates


def next_trade_date(trade_dates: list[date], current: date) -> date | None:
    for day in trade_dates:
        if day > current:
            return day
    return None


def prev_or_same_trade_date(trade_dates: list[date], current: date) -> date | None:
    out = None
    for day in trade_dates:
        if day > current:
            break
        out = day
    return out


def price_on(
    ohlc: dict[str, dict[date, dict[str, float]]],
    code: str,
    day: date,
    field: str,
) -> float | None:
    if day.weekday() >= 5:
        return None
    bar = (ohlc.get(code) or {}).get(day)
    if not bar:
        return None
    return to_float(bar.get(field)) or to_float(bar.get("close"))


def latest_price_on_or_before(
    ohlc: dict[str, dict[date, dict[str, float]]],
    code: str,
    day: date,
) -> float | None:
    by_day = ohlc.get(code) or {}
    available = [item for item in by_day if item <= day and item.weekday() < 5]
    if not available:
        return None
    return price_on(ohlc, code, max(available), "close")


def portfolio_value(
    cash: float,
    positions: dict[str, Position],
    ohlc: dict[str, dict[date, dict[str, float]]],
    day: date,
) -> float:
    value = cash
    for pos in positions.values():
        px = latest_price_on_or_before(ohlc, pos.code, day)
        if px is not None:
            value += pos.shares * px
    return value


def holding_trade_days(trade_dates: list[date], start: date, end: date) -> int:
    return sum(1 for day in trade_dates if start < day <= end)


def rank_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    scored = [row for row in rows if to_float(row.get("final_score_v0")) is not None]
    scored.sort(key=lambda row: to_float(row.get("final_score_v0")) or -1e9, reverse=True)
    if not scored:
        return {}
    denom = max(len(scored) - 1, 1)
    return {str(row["code"]): idx / denom for idx, row in enumerate(scored)}


def signal_market_date(rows: list[dict[str, Any]], fallback: date) -> date:
    values = []
    for row in rows:
        raw = str(row.get("latest_kline_date") or "")
        try:
            values.append(date.fromisoformat(raw))
        except ValueError:
            continue
    if not values:
        chosen = fallback
    else:
        chosen = Counter(values).most_common(1)[0][0]
    while chosen.weekday() >= 5:
        chosen = date.fromordinal(chosen.toordinal() - 1)
    return chosen


def load_signal_snapshots(reports_dir: Path, start: date | None, end: date | None) -> list[dict[str, Any]]:
    snapshots_by_market_date: dict[date, dict[str, Any]] = {}
    for report_date in available_signal_dates(reports_dir, start, end):
        rows, _ = build_factor_rows(reports_dir, report_date, skip_index_fetch=True)
        if not rows:
            continue
        market_date = signal_market_date(rows, report_date)
        snapshots_by_market_date[market_date] = {
            "report_date": report_date,
            "market_date": market_date,
            "rows": rows,
            "ranks": rank_map(rows),
        }
    return [snapshots_by_market_date[key] for key in sorted(snapshots_by_market_date)]


def risk_flags(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    value = row.get("risk_flags")
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    return str(value or "")


def can_buy(row: dict[str, Any], rank_pct: float, args: argparse.Namespace) -> bool:
    if rank_pct > args.buy_quantile:
        return False
    if risk_flags(row):
        return False
    close_to_ma20 = to_float(row.get("close_to_ma20"))
    if close_to_ma20 is None or close_to_ma20 < 1.0:
        return False
    trend = to_float(row.get("trend_relative_score_35"))
    if trend is None or trend < 12:
        return False
    return True


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
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


def max_drawdown(nav_rows: list[dict[str, Any]]) -> float:
    peak = -math.inf
    worst = 0.0
    for row in nav_rows:
        value = float(row["total_value"])
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def render_summary(
    args: argparse.Namespace,
    snapshots: list[dict[str, Any]],
    nav_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    positions: dict[str, Position],
) -> str:
    if not nav_rows:
        return "# 持仓组合模拟摘要\n\n无可用净值记录。\n"
    start_value = float(nav_rows[0]["total_value"])
    end_value = float(nav_rows[-1]["total_value"])
    total_return = (end_value / start_value - 1.0) * 100.0 if start_value else 0.0
    daily_returns = []
    prev = None
    for row in nav_rows:
        value = float(row["total_value"])
        if prev:
            daily_returns.append(value / prev - 1.0)
        prev = value
    win_rate = (sum(1 for item in daily_returns if item > 0) / len(daily_returns) * 100.0) if daily_returns else 0.0
    buys = [row for row in trades if row["side"] == "BUY"]
    sells = [row for row in trades if row["side"] == "SELL"]
    realized = [to_float(row.get("realized_return_pct")) for row in sells]
    realized = [item for item in realized if item is not None]
    avg_realized = statistics.fmean(realized) if realized else None
    realized_win = (sum(1 for item in realized if item > 0) / len(realized) * 100.0) if realized else None
    return "\n".join(
        [
            "# 持仓组合模拟摘要",
            "",
            f"- 信号期数：{len(snapshots)}",
            f"- 日期范围：{nav_rows[0]['date']} 至 {nav_rows[-1]['date']}",
            f"- 初始资金：{args.initial_cash:,.2f}",
            f"- 期末资产：{end_value:,.2f}",
            f"- 累计收益：{total_return:.2f}%",
            f"- 最大回撤：{max_drawdown(nav_rows):.2f}%",
            f"- 净值上涨日比例：{win_rate:.2f}%",
            f"- 当前持仓数：{len(positions)}",
            f"- 买入次数：{len(buys)}",
            f"- 卖出次数：{len(sells)}",
            f"- 已实现平均收益：{'-' if avg_realized is None else f'{avg_realized:.2f}%'}",
            f"- 已实现胜率：{'-' if realized_win is None else f'{realized_win:.2f}%'}",
            "",
            "## 参数",
            "",
            f"- 最大持仓：{args.max_positions}",
            f"- 最短持有：{args.min_hold_days} 个交易日",
            f"- 最长持有：{args.max_hold_days} 个交易日",
            f"- 买入分位：前 {args.buy_quantile * 100:.0f}%",
            f"- 卖出分位：跌出前 {args.sell_quantile * 100:.0f}%",
            f"- 冷却期：{args.cooldown_days} 个交易日",
            f"- 单边成本：{args.cost_bps:.1f} bps",
            "",
            "## 说明",
            "",
            "- 本模拟使用本地 JSON 的日 K 线，按信号日收盘后生成信号、下一交易日开盘执行。",
            "- 不模拟涨跌停无法成交、盘口冲击、真实整手限制和分红复权误差。",
        ]
    ) + "\n"


def simulate(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    reports_dir = Path(args.reports_dir).resolve()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    ohlc = build_ohlc_index(reports_dir)
    trade_dates = all_trade_dates(ohlc)
    snapshots = load_signal_snapshots(reports_dir, start, end)
    cash = float(args.initial_cash)
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, date] = {}
    nav_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    cost_rate = args.cost_bps / 10000.0

    for snapshot in snapshots:
        signal_day = snapshot["market_date"]
        mark_day = prev_or_same_trade_date(trade_dates, signal_day)
        exec_day = next_trade_date(trade_dates, signal_day)
        if mark_day is None or exec_day is None:
            continue
        rows = snapshot["rows"]
        row_by_code = {str(row["code"]): row for row in rows}
        ranks = snapshot["ranks"]

        pre_value = portfolio_value(cash, positions, ohlc, mark_day)
        nav_rows.append(
            {
                "date": mark_day.isoformat(),
                "phase": "signal_close",
                "cash": round(cash, 4),
                "position_value": round(pre_value - cash, 4),
                "total_value": round(pre_value, 4),
                "positions": len(positions),
                "signal_report_date": snapshot["report_date"].isoformat(),
            }
        )

        sell_codes: list[tuple[str, str]] = []
        for code, pos in list(positions.items()):
            row = row_by_code.get(code)
            rank_pct = ranks.get(code, 1.0)
            held_days = holding_trade_days(trade_dates, pos.entry_date, signal_day)
            close_to_ma20 = to_float(row.get("close_to_ma20")) if row else None
            reason = ""
            if risk_flags(row):
                reason = "risk_flag"
            elif close_to_ma20 is not None and close_to_ma20 < 1.0:
                reason = "below_ma20"
            elif held_days >= args.max_hold_days:
                reason = "max_hold"
            elif held_days >= args.min_hold_days and rank_pct > args.sell_quantile:
                reason = "rank_decay"
            if reason:
                sell_codes.append((code, reason))

        for code, reason in sell_codes:
            pos = positions.get(code)
            if not pos:
                continue
            px = price_on(ohlc, code, exec_day, "open")
            if px is None:
                continue
            gross = pos.shares * px
            cost = gross * cost_rate
            cash += gross - cost
            realized = (px / pos.entry_price - 1.0) * 100.0 if pos.entry_price else None
            trades.append(
                {
                    "date": exec_day.isoformat(),
                    "side": "SELL",
                    "code": code,
                    "name": pos.name,
                    "price": round(px, 4),
                    "shares": round(pos.shares, 6),
                    "gross": round(gross, 4),
                    "cost": round(cost, 4),
                    "cash_after": round(cash, 4),
                    "reason": reason,
                    "entry_date": pos.entry_date.isoformat(),
                    "entry_price": round(pos.entry_price, 4),
                    "realized_return_pct": "" if realized is None else round(realized, 4),
                    "signal_date": signal_day.isoformat(),
                }
            )
            del positions[code]
            cooldown_until[code] = exec_day

        post_sell_value = portfolio_value(cash, positions, ohlc, exec_day)
        slots = max(args.max_positions - len(positions), 0)
        candidates = sorted(
            rows,
            key=lambda row: to_float(row.get("final_score_v0")) or -1e9,
            reverse=True,
        )
        for row in candidates:
            if slots <= 0:
                break
            code = str(row["code"])
            if code in positions:
                continue
            cooldown_day = cooldown_until.get(code)
            if cooldown_day and holding_trade_days(trade_dates, cooldown_day, exec_day) <= args.cooldown_days:
                continue
            rank_pct = ranks.get(code, 1.0)
            if not can_buy(row, rank_pct, args):
                continue
            px = price_on(ohlc, code, exec_day, "open")
            if px is None or px <= 0:
                continue
            target_value = post_sell_value / args.max_positions
            spend = min(cash, target_value)
            if spend <= 0:
                break
            shares = spend / (px * (1.0 + cost_rate))
            gross = shares * px
            cost = gross * cost_rate
            total = gross + cost
            if total > cash + 1e-6:
                continue
            cash -= total
            positions[code] = Position(
                code=code,
                name=str(row.get("name") or code),
                shares=shares,
                entry_date=exec_day,
                entry_price=px,
                entry_score=float(to_float(row.get("final_score_v0")) or 0.0),
            )
            trades.append(
                {
                    "date": exec_day.isoformat(),
                    "side": "BUY",
                    "code": code,
                    "name": row.get("name") or code,
                    "price": round(px, 4),
                    "shares": round(shares, 6),
                    "gross": round(gross, 4),
                    "cost": round(cost, 4),
                    "cash_after": round(cash, 4),
                    "reason": "top_score",
                    "entry_date": "",
                    "entry_price": "",
                    "realized_return_pct": "",
                    "signal_date": signal_day.isoformat(),
                }
            )
            slots -= 1

        post_trade_value = portfolio_value(cash, positions, ohlc, exec_day)
        nav_rows.append(
            {
                "date": exec_day.isoformat(),
                "phase": "after_trade",
                "cash": round(cash, 4),
                "position_value": round(post_trade_value - cash, 4),
                "total_value": round(post_trade_value, 4),
                "positions": len(positions),
                "signal_report_date": snapshot["report_date"].isoformat(),
            }
        )

    position_rows: list[dict[str, Any]] = []
    final_day = date.fromisoformat(nav_rows[-1]["date"]) if nav_rows else None
    for pos in positions.values():
        px = latest_price_on_or_before(ohlc, pos.code, final_day) if final_day else None
        position_rows.append(
            {
                "code": pos.code,
                "name": pos.name,
                "shares": round(pos.shares, 6),
                "entry_date": pos.entry_date.isoformat(),
                "entry_price": round(pos.entry_price, 4),
                "entry_score": round(pos.entry_score, 4),
                "last_price": "" if px is None else round(px, 4),
                "market_value": "" if px is None else round(px * pos.shares, 4),
                "unrealized_return_pct": "" if px is None else round((px / pos.entry_price - 1.0) * 100.0, 4),
            }
        )
    summary = render_summary(args, snapshots, nav_rows, trades, positions)
    return nav_rows, trades, position_rows, summary


def main() -> int:
    args = parse_args()
    reports_dir = Path(args.reports_dir).resolve()
    nav_path = Path(args.output_nav).resolve() if args.output_nav else reports_dir / "portfolio_nav.csv"
    trades_path = Path(args.output_trades).resolve() if args.output_trades else reports_dir / "portfolio_trades.csv"
    positions_path = Path(args.output_positions).resolve() if args.output_positions else reports_dir / "portfolio_positions.csv"
    summary_path = Path(args.output_summary).resolve() if args.output_summary else reports_dir / "portfolio_summary.md"
    nav_rows, trades, position_rows, summary = simulate(args)
    write_csv(nav_path, nav_rows)
    write_csv(trades_path, trades)
    write_csv(positions_path, position_rows)
    summary_path.write_text(summary, encoding="utf-8")
    print(f"nav_rows={len(nav_rows)}")
    print(f"trades={len(trades)}")
    print(f"positions={len(position_rows)}")
    print(f"nav={nav_path}")
    print(f"trades_csv={trades_path}")
    print(f"positions_csv={positions_path}")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
