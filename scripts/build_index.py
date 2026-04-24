"""
data/*.json 에 쌓인 일별 수집 결과를 하나의 대시보드용 인덱스로 합친다.

산출물:
    docs/data/index.json
        {
          "generated_at": ...,
          "config": {...},
          "stats": {"open": N, "closing_soon": N, "closed": N, "total": N},
          "open": [...],            # 아직 마감 안 된 공고 (마감 임박순)
          "closing_soon": [...],    # 24시간 이내 마감
          "closed": [...]           # 이미 마감 (최근순)
        }

config.keep_days 보다 오래된 data/ 파일은 스킵한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
OUT_PATH = ROOT / "docs" / "data" / "index.json"


def parse_bid_dt(s: str) -> datetime | None:
    """나라장터 API의 날짜 문자열을 KST datetime으로 파싱.

    관측된 포맷: '2026-04-30 18:00:00', '202604301800', '2026-04-30 18:00'
    """
    if not s:
        return None
    s = s.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def iter_daily_files(keep_days: int) -> list[Path]:
    if not DATA_DIR.exists():
        return []
    cutoff = (datetime.now(tz=KST) - timedelta(days=keep_days)).date()
    kept: list[Path] = []
    for p in sorted(DATA_DIR.glob("*.json")):
        try:
            file_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            kept.append(p)
    return kept


def match_keywords(title: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    """제목에 매치되는 모든 키워드를 원본 순서대로 반환."""
    if not title or not keywords:
        return []
    haystack = title if case_sensitive else title.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in haystack]


def merge_all(files: list[Path], keywords: list[str], case_sensitive: bool) -> list[dict]:
    merged: dict[str, dict] = {}
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        collected_at = payload.get("date") or path.stem
        for item in payload.get("items", []):
            key = f"{item.get('bidNtceNo')}-{item.get('bidNtceOrd')}"
            merged_item = dict(item)
            existing_seen = merged.get(key, {}).get("first_seen_date", collected_at)
            merged_item["first_seen_date"] = (
                existing_seen if existing_seen < collected_at else collected_at
            )
            merged_item["last_seen_date"] = collected_at
            merged_item["matched_keywords"] = match_keywords(
                str(item.get("bidNtceNm") or ""), keywords, case_sensitive,
            )
            merged[key] = merged_item
    return list(merged.values())


def classify(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    now = datetime.now(tz=KST)
    soon_cutoff = now + timedelta(hours=24)

    open_list: list[dict] = []
    soon_list: list[dict] = []
    closed_list: list[dict] = []

    for it in items:
        close_dt = parse_bid_dt(it.get("bidClseDt", ""))
        enriched = dict(it)
        if close_dt is not None:
            enriched["_bidClseDt_iso"] = close_dt.isoformat()
            remaining = close_dt - now
            enriched["_hours_remaining"] = round(remaining.total_seconds() / 3600, 1)
        else:
            enriched["_bidClseDt_iso"] = ""
            enriched["_hours_remaining"] = None

        if close_dt is None:
            open_list.append(enriched)
        elif close_dt < now:
            closed_list.append(enriched)
        elif close_dt <= soon_cutoff:
            soon_list.append(enriched)
        else:
            open_list.append(enriched)

    def key_open(x: dict) -> float:
        h = x.get("_hours_remaining")
        return h if h is not None else float("inf")

    open_list.sort(key=key_open)
    soon_list.sort(key=key_open)
    closed_list.sort(key=lambda x: x.get("_bidClseDt_iso") or "", reverse=True)
    return open_list, soon_list, closed_list


def main() -> int:
    cfg = load_config()
    keep_days = int(cfg.get("keep_days", 60))
    keywords = [k.strip() for k in cfg.get("keywords", []) if k and k.strip()]
    case_sensitive = bool(cfg.get("case_sensitive", False))
    files = iter_daily_files(keep_days)
    print(f"사용할 일별 파일: {len(files)}개")

    merged = merge_all(files, keywords, case_sensitive)
    print(f"고유 공고 총합: {len(merged)}건")

    open_list, soon_list, closed_list = classify(merged)

    payload = {
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "config": {
            "keywords": cfg.get("keywords", []),
            "exclude_keywords": cfg.get("exclude_keywords", []),
            "match_mode": cfg.get("match_mode", "any"),
            "keep_days": keep_days,
        },
        "stats": {
            "open": len(open_list),
            "closing_soon": len(soon_list),
            "closed": len(closed_list),
            "total": len(merged),
        },
        "closing_soon": soon_list,
        "open": open_list,
        "closed": closed_list,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"저장: {OUT_PATH.relative_to(ROOT)}")
    print(f"  open={len(open_list)}  closing_soon={len(soon_list)}  closed={len(closed_list)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
