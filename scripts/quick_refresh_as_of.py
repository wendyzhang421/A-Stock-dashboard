#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as dashboard


def main() -> int:
    as_of_raw = sys.argv[1] if len(sys.argv) > 1 else dashboard.AS_OF.isoformat()
    as_of = date.fromisoformat(as_of_raw)
    html_path, json_path, strong_json_path = dashboard.quick_refresh_dashboard(as_of)
    print(html_path)
    print(json_path)
    print(strong_json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
