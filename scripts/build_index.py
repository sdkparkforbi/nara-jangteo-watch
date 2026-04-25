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
CACHE_PATH = DATA_DIR / "_relevance_cache.json"
OUT_PATH = ROOT / "docs" / "data" / "index.json"


def load_relevance_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


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


def merge_all(
    files: list[Path],
    keywords: list[str],
    case_sensitive: bool,
    service_divs: list[str] | None = None,
    relevance_cache: dict | None = None,
) -> list[dict]:
    div_set = set(service_divs) if service_divs else None
    cache = relevance_cache or {}
    merged: dict[str, dict] = {}
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        collected_at = payload.get("date") or path.stem
        for item in payload.get("items", []):
            if div_set is not None:
                div = str(item.get("srvceDivNm") or "").strip()
                if div not in div_set:
                    continue
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
            score_entry = cache.get(key)
            if score_entry:
                merged_item["_relevance_score"] = score_entry.get("score")
                merged_item["_relevance_reason"] = score_entry.get("reason", "")
            merged[key] = merged_item
    return list(merged.values())


def dedup_by_bid_no(items: list[dict]) -> tuple[list[dict], int]:
    """같은 bidNtceNo 그룹에서 bidNtceOrd 가 가장 큰(최신 차수) 항목만 남긴다.

    Returns:
        (남은 항목 리스트, 제거된 항목 수)
    """
    # bidNtceNo 별로 가장 큰 차수의 항목을 추적
    latest: dict[str, dict] = {}
    for it in items:
        bid_no = str(it.get("bidNtceNo") or "")
        if not bid_no:
            # 공고번호 없는 항목은 그냥 통과 (방어적 처리)
            latest[f"_no_id_{id(it)}"] = it
            continue
        # bidNtceOrd 는 보통 "000", "001" 같은 문자열 — 정수 비교 위해 변환
        try:
            ord_n = int(it.get("bidNtceOrd") or 0)
        except (ValueError, TypeError):
            ord_n = 0
        prev = latest.get(bid_no)
        if prev is None:
            latest[bid_no] = it
            continue
        try:
            prev_ord = int(prev.get("bidNtceOrd") or 0)
        except (ValueError, TypeError):
            prev_ord = 0
        if ord_n > prev_ord:
            latest[bid_no] = it
    kept = list(latest.values())
    removed = len(items) - len(kept)
    return kept, removed


SOON_THRESHOLD_DAYS = 7


def classify(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    now = datetime.now(tz=KST)
    soon_cutoff = now + timedelta(days=SOON_THRESHOLD_DAYS)

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
    service_divs = [v.strip() for v in cfg.get("service_divs", []) if v and v.strip()]
    rf = cfg.get("relevance_filter") or {}
    rf_enabled = bool(rf.get("enabled", False))
    min_score = int(rf.get("min_score", 0)) if rf_enabled else None

    files = iter_daily_files(keep_days)
    print(f"사용할 일별 파일: {len(files)}개")
    if service_divs:
        print(f"용역구분 화이트리스트(srvceDivNm): {service_divs}")

    cache = load_relevance_cache() if rf_enabled else {}
    if rf_enabled:
        print(f"관련성 캐시: {len(cache)}건 / min_score={min_score}")

    merged = merge_all(files, keywords, case_sensitive, service_divs, cache)
    print(f"고유 공고 총합 (관련성 필터 전): {len(merged)}건")

    # 관련성 필터 적용 (점수 없는 항목은 통과시킴 — 실패해도 데이터를 잃지 않음)
    skipped_low = 0
    skipped_unscored = 0
    if rf_enabled and min_score is not None:
        kept: list[dict] = []
        for it in merged:
            sc = it.get("_relevance_score")
            if sc is None:
                kept.append(it)
                skipped_unscored += 1  # 통계용 — 통과는 시킴
                continue
            if sc < min_score:
                skipped_low += 1
                continue
            kept.append(it)
        merged = kept
        print(f"관련성 필터 적용 후: {len(merged)}건 (저점수 제외 {skipped_low}, 미평가 통과 {skipped_unscored})")

    # 같은 bidNtceNo 내 최신 차수만 남기기 (재제출/취소공고 정리)
    merged, removed_dup = dedup_by_bid_no(merged)
    if removed_dup > 0:
        print(f"중복 차수 제거 후: {len(merged)}건 (구 차수 {removed_dup}건 제거)")

    open_list, soon_list, closed_list = classify(merged)

    payload = {
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "config": {
            "keywords": cfg.get("keywords", []),
            "exclude_keywords": cfg.get("exclude_keywords", []),
            "service_divs": service_divs,
            "match_mode": cfg.get("match_mode", "any"),
            "keep_days": keep_days,
            "relevance_filter": {
                "enabled": rf_enabled,
                "min_score": min_score,
                "model": rf.get("model") if rf_enabled else None,
                "provider": rf.get("provider") if rf_enabled else None,
            },
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
