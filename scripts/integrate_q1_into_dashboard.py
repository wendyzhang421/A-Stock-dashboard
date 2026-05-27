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
    watch_path = Path("reports/watchlist_dashboard_2026-04-29.json")
    strong_path = Path("reports/watchlist_strong_stocks_2026-04-29.json")
    html_path = Path("reports/watchlist_dashboard_2026-04-29.html")

    dataset = json.loads(watch_path.read_text(encoding="utf-8"))
    strong_stocks = json.loads(strong_path.read_text(encoding="utf-8"))

    dataset = dashboard.sort_watchlist_dataset(
        dashboard.backfill_market_caps(dashboard.hydrate_cached_dataset(dataset))
    )
    for item in strong_stocks:
        code = item.get("code", "")
        item["research"] = dashboard.build_research_payload(code, item.get("research", {}))

    watch_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_path.write_text(json.dumps(strong_stocks, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(dashboard.build_html(dataset, strong_stocks), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
