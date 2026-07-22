#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://quantapi.51ifind.com/api/v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
FONT_CANDIDATES = [
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
]
THEMES = [
    ("算力 / 半导体产业链", "算力租赁与数据中心活跃，芯片材料同步扩散", ["算力", "数据中心", "CPO", "半导体", "芯片", "AI", "光网络", "液冷", "超节点"]),
    ("电力 / 新能源", "火电、热电与风光储多线共振", ["电力", "火电", "热电", "风电", "光伏", "储能", "新能源", "电缆"]),
    ("医药", "创新药、中药与业绩改善催化", ["创新药", "医药", "中药", "抗肿瘤", "P-CAB"]),
    ("有色金属", "黄金、白银与小金属价格主题走强", ["黄金", "白银", "铅锌", "氧化锆", "电解铝", "稀土", "钾肥"]),
    ("重组 / 控制权", "收购、重整与控制权变更保持活跃", ["收购", "重整", "控制权", "协议转让"]),
    ("油气 / 煤化工", "油气、煤炭及甲醇方向轮动", ["油气", "煤炭", "甲醇"]),
    ("其他概念", "航运、消费与事件驱动分支", []),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 iFinD A股涨停热点复盘长图并推送 Telegram")
    parser.add_argument("--date", help="交易日期 YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--output-dir", default="reports/hot_recap", help="图片与 JSON 输出目录")
    parser.add_argument("--dry-run", action="store_true", help="生成文件但不发送 Telegram")
    return parser.parse_args()


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = os.getenv("ASTOCK_TRUST_ENV", "0").lower() in {"1", "true", "yes", "on"}
    return session


def api_post(session: requests.Session, path: str, *, payload=None, headers=None, timeout=60) -> dict:
    response = session.post(BASE_URL + path, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if result.get("errorcode") not in (None, 0):
        raise RuntimeError(f"iFinD {path} failed: {result.get('errmsg') or result.get('errorcode')}")
    return result


def fetch_rows(session: requests.Session, trade_date: date) -> list[dict]:
    refresh_token = os.getenv("ASTOCK_IFIND_REFRESH_TOKEN", "").strip() or os.getenv("IFIND_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        raise RuntimeError("missing ASTOCK_IFIND_REFRESH_TOKEN")
    auth = api_post(session, "/get_access_token", headers={"refresh_token": refresh_token}, timeout=30)
    access_token = auth["data"]["access_token"]
    headers = {"Content-Type": "application/json", "access_token": access_token, "ifindlang": "cn"}
    date_cn = f"{trade_date.year}年{trade_date.month}月{trade_date.day}日"
    query = f"{date_cn}涨停股票，首次涨停时间，最终涨停时间，开板次数，连续涨停天数，涨停原因，总市值，所属概念，所属同花顺行业"
    result = api_post(
        session,
        "/smart_stock_picking",
        payload={"searchstring": query, "searchtype": "stock"},
        headers=headers,
    )
    tables = result.get("tables") or []
    if not tables or not tables[0].get("table"):
        return []
    table = tables[0]["table"]
    code_values = table.get("股票代码") or []
    rows = [
        {key: (values[index] if index < len(values) else None) for key, values in table.items()}
        for index in range(len(code_values))
    ]
    if not rows:
        return []
    date_key = trade_date.strftime("%Y%m%d")
    pe_result = api_post(
        session,
        "/basic_data_service",
        payload={
            "codes": ",".join(row["股票代码"] for row in rows),
            "indipara": [{"indicator": "ths_pe_ttm_stock", "indiparams": [date_key]}],
        },
        headers=headers,
    )
    pe_map = {}
    for item in pe_result.get("tables", []):
        values = item.get("table", {}).get("ths_pe_ttm_stock") or [None]
        pe_map[item["thscode"]] = values[0]
    for row in rows:
        row[f"市盈率(pe,ttm)[{date_key}]"] = pe_map.get(row["股票代码"])
    return rows


def find_font() -> Path:
    custom = os.getenv("ASTOCK_CJK_FONT", "").strip()
    candidates = ([Path(custom)] if custom else []) + FONT_CANDIDATES
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("No CJK font found; install fonts-noto-cjk or set ASTOCK_CJK_FONT")


def group_rows(rows: list[dict], date_key: str) -> list[tuple[tuple[str, str, list[str]], list[dict]]]:
    reason_key = f"涨停原因类别[{date_key}]"
    buckets = [[] for _ in THEMES]
    for row in rows:
        reason = str(row.get(reason_key) or "")
        target = len(THEMES) - 1
        for index, (_, _, keys) in enumerate(THEMES[:-1]):
            if any(key in reason for key in keys):
                target = index
                break
        buckets[target].append(row)
    return [(THEMES[index], bucket) for index, bucket in enumerate(buckets) if bucket]


def render_image(rows: list[dict], trade_date: date, output_path: Path) -> None:
    date_key = trade_date.strftime("%Y%m%d")
    reason_key = f"涨停原因类别[{date_key}]"
    time_key = f"最终涨停时间[{date_key}]"
    open_key = f"涨停开板次数[{date_key}]"
    board_key = f"连续涨停天数[{date_key}]"
    cap_key = f"总市值[{date_key}]"
    pe_key = f"市盈率(pe,ttm)[{date_key}]"
    font_path = find_font()
    width, margin, row_h = 1680, 70, 54
    grouped = group_rows(rows, date_key)
    height = 360 + sum(76 + len(bucket) * row_h + 30 for _, bucket in grouped) + 100
    image = Image.new("RGB", (width, height), "#F5F7FA")
    draw = ImageDraw.Draw(image)

    def face(size: int, bold=False):
        try:
            return ImageFont.truetype(str(font_path), size=size, index=1 if bold else 0)
        except OSError:
            return ImageFont.truetype(str(font_path), size=size)

    def pe_label(value) -> str:
        if value in (None, "", "-"):
            return "—"
        number = float(value)
        return "亏损" if number < 0 else f"{number:.1f}x"

    draw.rounded_rectangle((40, 35, width - 40, 275), radius=28, fill="#D93A32")
    draw.text((margin, 64), trade_date.strftime("%m月%d日"), font=face(74, True), fill="#FFFFFF")
    draw.text((margin, 145), "A股涨停热点复盘", font=face(58, True), fill="#FFFFFF")
    max_board = max(int(row.get(board_key) or 1) for row in rows)
    sealed = sum(str(row.get(open_key)) == "0" for row in rows)
    draw.text((margin, 228), f"涨停 {len(rows)}只   最高 {max_board}板   零开板 {sealed}只   数据源 iFinD QuantAPI", font=face(27), fill="#FFE9D6")

    y = 315
    for (title, note, _), bucket in grouped:
        bucket.sort(key=lambda row: (-int(row.get(board_key) or 1), str(row.get(time_key) or "")))
        draw.rounded_rectangle((40, y, width - 40, y + 62), radius=14, fill="#F2C96D")
        draw.text((margin, y + 13), f"{title}：{note}", font=face(27, True), fill="#7A301C")
        count_text = f"{len(bucket)}只"
        draw.text((width - margin - draw.textlength(count_text, font=face(25)), y + 15), count_text, font=face(25), fill="#7A301C")
        y += 66
        for index, row in enumerate(bucket):
            draw.rectangle((40, y, width - 40, y + row_h), fill="#FFFFFF" if index % 2 == 0 else "#FAFBFC")
            board_n = int(row.get(board_key) or 1)
            board = "首板" if board_n == 1 else f"{board_n}板"
            dot = "#D93A32" if str(row.get(open_key)) == "0" else "#E49A38"
            draw.ellipse((margin, y + 20, margin + 13, y + 33), fill=dot)
            draw.text((margin + 22, y + 11), board, font=face(23, True), fill="#343A40")
            draw.text((175, y + 11), row["股票代码"].split(".")[0], font=face(22), fill="#D34A37")
            draw.text((285, y + 11), str(row.get("股票简称") or ""), font=face(23, True), fill="#343A40")
            final_time = str(row.get(time_key) or "--").split()[-1]
            draw.text((430, y + 11), final_time, font=face(22), fill="#59636E")
            cap = float(row.get(cap_key) or 0) / 1e8
            draw.text((555, y + 11), f"{cap:.2f}亿", font=face(22), fill="#59636E")
            draw.text((685, y + 11), pe_label(row.get(pe_key)), font=face(22), fill="#59636E")
            reason = str(row.get(reason_key) or "")
            max_reason_width = width - 805 - margin
            while draw.textlength(reason, font=face(21)) > max_reason_width and len(reason) > 4:
                reason = reason[:-1]
            if reason != str(row.get(reason_key) or ""):
                reason += "…"
            draw.text((805, y + 12), reason, font=face(21), fill="#3F4852")
            y += row_h
        y += 25
    draw.text((margin, height - 65), "注：列顺序为连板/代码/简称/最终封板/总市值/PE TTM/涨停原因；负 PE 显示为亏损。", font=face(22), fill="#6B7580")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, optimize=True)


def send_document(session: requests.Session, image_path: Path, trade_date: date) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    caption = f"{trade_date.month}月{trade_date.day}日 A股涨停热点复盘｜总市值 + PE TTM｜完整高清原始 PNG｜iFinD QuantAPI"
    with image_path.open("rb") as image_file:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (image_path.name, image_file, "image/png")},
            timeout=120,
        )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {payload.get('description', 'unknown error')}")
    return int(payload["result"]["message_id"])


def main() -> int:
    args = parse_args()
    trade_date = date.fromisoformat(args.date) if args.date else datetime.now(SHANGHAI).date()
    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    session = build_session()
    rows = fetch_rows(session, trade_date)
    if not rows:
        print(json.dumps({"status": "skipped", "date": trade_date.isoformat(), "reason": "no limit-up rows"}, ensure_ascii=False))
        return 0
    json_path = output_dir / f"ifind_hot_recap_{trade_date.isoformat()}.json"
    image_path = output_dir / f"ifind_hot_recap_{trade_date.isoformat()}.png"
    json_path.write_text(json.dumps({"date": trade_date.isoformat(), "count": len(rows), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    render_image(rows, trade_date, image_path)
    message_id = None if args.dry_run else send_document(session, image_path, trade_date)
    print(json.dumps({"status": "ok", "date": trade_date.isoformat(), "count": len(rows), "image": str(image_path), "message_id": message_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
