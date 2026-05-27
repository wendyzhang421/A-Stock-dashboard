#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as dashboard


def main() -> int:
    prev_watch_path = ROOT / "reports" / "watchlist_dashboard_2026-04-29.json"
    watch_path = ROOT / "reports" / "watchlist_dashboard_2026-04-30.json"
    strong_path = ROOT / "reports" / "watchlist_strong_stocks_2026-04-30.json"
    html_path = ROOT / "reports" / "watchlist_dashboard_2026-04-30.html"

    prev_watch = {row["code"]: row for row in json.loads(prev_watch_path.read_text(encoding="utf-8"))}
    watch = json.loads(watch_path.read_text(encoding="utf-8"))
    strong = json.loads(strong_path.read_text(encoding="utf-8"))

    def fix_row(row: dict) -> dict:
        prev = prev_watch.get(row.get("code"))
        if not prev:
            return row
        prev_close = float(prev.get("latestClose") or 0.0)
        new_close = float(row.get("latestClose") or 0.0)
        if not prev_close or not new_close:
            return row
        total_shares = float(prev.get("totalMarketCap") or 0.0) / prev_close
        float_shares = float(prev.get("floatMarketCap") or 0.0) / prev_close
        if total_shares > 0:
            row["totalMarketCap"] = total_shares * new_close
        if float_shares > 0:
            row["floatMarketCap"] = float_shares * new_close
        elif row.get("totalMarketCap"):
            row["floatMarketCap"] = row["totalMarketCap"]
        return row

    watch = [fix_row(row) for row in watch]
    watch = dashboard.sort_watchlist_dataset(dashboard.backfill_market_caps(watch))
    strong = [fix_row(row) for row in strong]

    watch_path.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_path.write_text(json.dumps(strong, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(dashboard.build_html(watch, strong), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
