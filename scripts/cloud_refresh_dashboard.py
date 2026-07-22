#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_watchlist_dashboard as d

REFRESH_TIMEOUT_SECONDS = int(os.environ.get("ASTOCK_REFRESH_TIMEOUT_SECONDS", "600"))
ENABLE_LIVE_LOOKUPS = os.environ.get("ASTOCK_CLOUD_LIVE_LOOKUPS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SKIP_LIVE_NEWS = os.environ.get("ASTOCK_SKIP_LIVE_NEWS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ENABLE_STRONG_LIVE_LOOKUPS = os.environ.get("ASTOCK_STRONG_LIVE_LOOKUPS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STRONG_BUSINESS_WORKERS = max(1, int(os.environ.get("ASTOCK_STRONG_BUSINESS_WORKERS", "8")))


def run_refresh(script_name: str, as_of: date) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), as_of.isoformat()],
        cwd=ROOT,
        check=True,
        timeout=REFRESH_TIMEOUT_SECONDS,
    )


def load_json(path: Path) -> list[dict] | dict:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_prior_path(reports: Path, prefix: str, suffix: str, as_of: date) -> Path | None:
    matched: list[tuple[date, Path]] = []
    for path in reports.glob(f"{prefix}_*{suffix}"):
        stem = path.name.removeprefix(f"{prefix}_").removesuffix(suffix)
        try:
            path_date = date.fromisoformat(stem)
        except ValueError:
            continue
        if path_date < as_of:
            matched.append((path_date, path))
    if not matched:
        return None
    matched.sort(key=lambda item: item[0])
    return matched[-1][1]


def extract_market_overview(html_path: Path) -> dict[str, object]:
    if not html_path.exists():
        return {}
    text = html_path.read_text(encoding="utf-8")
    match = re.search(r"const marketOverview = (.*?);\n", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except Exception:
        return {}


def patch_watchlist_to_today(access_token: str | None, watch: list[dict], as_of: date) -> list[dict]:
    if not access_token:
        return watch
    realtime = d.fetch_realtime_map(access_token, [row["code"] for row in watch], as_of)
    for row in watch:
        code = row["code"]
        rt = realtime.get(code) or {}
        latests = rt.get("latest") or []
        volumes = rt.get("volume") or []
        amounts = rt.get("amount") or []
        pcts = rt.get("changeRatio") or []
        latest = d.to_float(latests[-1]) if latests else None
        volume_hands = d.to_float(volumes[-1]) if volumes else None
        amount = d.to_float(amounts[-1]) if amounts else None
        pct = d.to_float(pcts[-1]) if pcts else None
        kline = row.get("kline") or []
        if kline and kline[-1][0] < as_of.isoformat() and latest not in (None, 0):
            prev_close = float(kline[-1][2])
            kline.append(
                [
                    as_of.isoformat(),
                    prev_close,
                    float(latest),
                    min(prev_close, float(latest)),
                    max(prev_close, float(latest)),
                    float(volume_hands or 0.0) * 100.0,
                    float(amount or 0.0),
                ]
            )
            row["kline"] = kline[-30:]
        if latest not in (None, 0):
            row["latestClose"] = float(latest)
        if amount not in (None, 0):
            row["todayAmount"] = float(amount)
        if volume_hands not in (None, 0):
            row["todayVolume"] = float(volume_hands) * 100.0
        if pct is not None:
            row["todayPct"] = float(pct)
        if row.get("kline"):
            last5 = []
            prev = None
            for item in row["kline"][-5:]:
                close = float(item[2])
                day_pct = None if prev in (None, 0) else (close / prev - 1.0) * 100.0
                last5.append(
                    {
                        "date": item[0],
                        "close": close,
                        "volume": float(item[5]),
                        "amount": float(item[6]),
                        "pct": day_pct,
                    }
                )
                prev = close
            row["last5"] = last5
    return watch


def patch_strong_main_business(strong: list[dict]) -> list[dict]:
    if not strong:
        return strong
    with ThreadPoolExecutor(max_workers=min(STRONG_BUSINESS_WORKERS, len(strong))) as executor:
        return list(
            executor.map(
                lambda row: d.resolve_row_main_business(
                    row,
                    allow_live_lookup=ENABLE_STRONG_LIVE_LOOKUPS,
                ),
                strong,
            )
        )


def patch_main_business_and_news(watch: list[dict], strong: list[dict]) -> tuple[list[dict], list[dict]]:
    all_rows = watch + strong
    news_targets: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for row in all_rows:
        code = row.get("code")
        name = row.get("name")
        if not code or not name or code in seen_codes:
            continue
        seen_codes.add(code)
        news_targets.append((name, code))

    if SKIP_LIVE_NEWS:
        news_map = {}
    else:
        try:
            news_map = d.fetch_latest_news_map(news_targets)
        except Exception:
            news_map = {}

    for row in all_rows:
        code = row.get("code", "")
        historical = d.load_historical_report_row(code) or {}
        latest_news = news_map.get(code) or historical.get("latestNews") or row.get("latestNews") or {
            "time": "",
            "summary": "",
            "title": "",
            "link": "",
            "isRecent": False,
        }
        row["latestNews"] = latest_news
        if not row.get("businessSegments") and historical.get("businessSegments"):
            row["businessSegments"] = historical.get("businessSegments")
        if not row.get("industry") and historical.get("industry"):
            row["industry"] = historical.get("industry")
        d.resolve_row_main_business(row, allow_live_lookup=ENABLE_LIVE_LOOKUPS)
    return watch, strong


def main() -> int:
    as_of_raw = sys.argv[1] if len(sys.argv) > 1 else d.AS_OF.isoformat()
    as_of = date.fromisoformat(as_of_raw)
    reports = ROOT / "reports"
    json_path = reports / f"watchlist_dashboard_{as_of.isoformat()}.json"
    strong_path = reports / f"watchlist_strong_stocks_{as_of.isoformat()}.json"
    institution_path = reports / f"institution_holdings_{as_of.isoformat()}.json"
    html_path = reports / f"watchlist_dashboard_{as_of.isoformat()}.html"
    index_path = reports / "share_dashboard" / "index.html"
    root_index_path = reports / "index.html"

    for generated_path in (json_path, strong_path, institution_path, html_path):
        generated_path.unlink(missing_ok=True)

    used_public_fallback = False
    used_cached_fallback = False
    refresh_errors: list[str] = []
    try:
        run_refresh("refresh_ifind_20260512.py", as_of)
    except Exception as exc:
        refresh_errors.append(f"refresh_ifind_20260512.py: {exc}")
        print("iFinD refresh failed", file=sys.stderr)
        traceback.print_exc()
        if json_path.exists() and strong_path.exists():
            print("Using partial iFinD core reports; skipping public refresh", file=sys.stderr)
        else:
            used_public_fallback = True
            try:
                run_refresh("refresh_public_as_of.py", as_of)
            except Exception as public_exc:
                refresh_errors.append(f"refresh_public_as_of.py: {public_exc}")
                print("Public refresh failed; falling back to cached baseline", file=sys.stderr)
                traceback.print_exc()
                used_cached_fallback = True

    if not json_path.exists() or not strong_path.exists() or not institution_path.exists():
        prior_json = latest_prior_path(reports, "watchlist_dashboard", ".json", as_of)
        prior_strong = latest_prior_path(reports, "watchlist_strong_stocks", ".json", as_of)
        prior_institution = latest_prior_path(reports, "institution_holdings", ".json", as_of)
        if not prior_json or not prior_strong:
            raise FileNotFoundError(f"No cached baseline available before {as_of.isoformat()}")
        if not json_path.exists():
            json_path.write_text(prior_json.read_text(encoding="utf-8"), encoding="utf-8")
        if not strong_path.exists():
            strong_path.write_text(prior_strong.read_text(encoding="utf-8"), encoding="utf-8")
        if not institution_path.exists():
            institution_holdings = load_json(prior_institution) if prior_institution else {
                "date": as_of.isoformat(),
                "source": "cached fallback",
                "rows": [],
            }
            institution_path.write_text(
                json.dumps(institution_holdings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    access_token = None
    try:
        access_token = d.get_access_token()
    except Exception:
        access_token = None

    watch = load_json(json_path)
    strong = load_json(strong_path)
    watch = patch_watchlist_to_today(access_token, watch, as_of)
    watch = d.sort_watchlist_dataset(d.backfill_market_caps(d.hydrate_cached_dataset(watch)))
    strong = patch_strong_main_business(d.hydrate_cached_strong_stocks(strong))
    watch, strong = patch_main_business_and_news(watch, strong)

    prior_institution = latest_prior_path(reports, "institution_holdings", ".json", as_of)
    if institution_path.exists():
        institution_holdings = load_json(institution_path)
    else:
        try:
            institution_holdings = d.fetch_institution_holdings(access_token)
        except Exception:
            institution_holdings = load_json(prior_institution) if prior_institution else {"date": as_of.isoformat(), "source": "cached fallback", "rows": []}
        institution_path.write_text(json.dumps(institution_holdings, ensure_ascii=False, indent=2), encoding="utf-8")

    market_overview = d.fetch_market_overview(access_token) if access_token else extract_market_overview(html_path)
    if not market_overview:
        prior_html = latest_prior_path(reports, "watchlist_dashboard", ".html", as_of)
        market_overview = extract_market_overview(prior_html) if prior_html else {}

    json_path.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
    strong_path.write_text(json.dumps(strong, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(d.build_html(watch, strong, market_overview, institution_holdings), encoding="utf-8")
    index_path.write_text(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=../watchlist_dashboard_{as_of.isoformat()}.html">',
        encoding="utf-8",
    )
    root_index_path.write_text(
        '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=share_dashboard/">',
        encoding="utf-8",
    )

    tail_set = sorted({(row.get("kline") or [[""]])[-1][0] for row in watch if row.get("kline")})
    blank_main_business = sum(1 for row in strong if not row.get("mainBusiness") or row.get("mainBusiness") == "-")
    print(json.dumps(
        {
            "as_of": as_of.isoformat(),
            "used_public_fallback": used_public_fallback,
            "used_cached_fallback": used_cached_fallback,
            "refresh_errors": refresh_errors,
            "watch_count": len(watch),
            "watch_tail_set": tail_set,
            "strong_count": len(strong),
            "strong_blank_main_business": blank_main_business,
            "institution_rows": len((institution_holdings or {}).get("rows") or []),
            "html": str(html_path),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
