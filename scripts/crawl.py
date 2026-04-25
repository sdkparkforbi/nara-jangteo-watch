"""
나라장터 용역 입찰공고 크롤러

공공데이터포털의 나라장터 입찰공고정보서비스에서 [나라장터검색조건에 의한 입찰공고용역조회]
오퍼레이션(getBidPblancListInfoServcPPSSrch)을 사용하여 각 키워드별로 공고명 부분검색을
수행하고, 결과를 합쳐 data/YYYY-MM-DD.json 에 저장한다.

환경변수:
    NARA_SERVICE_KEY : data.go.kr 에서 발급받은 Decoding 인증키 (필수)

사용법:
    python scripts/crawl.py
    python scripts/crawl.py --days 3
    python scripts/crawl.py --date 2026-04-23
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"


def _load_dotenv(path: Path) -> None:
    """의존성 없는 간단한 .env 로더. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


@dataclass
class Config:
    keywords: list[str]
    exclude_keywords: list[str]
    service_divs: list[str]
    lookback_days: int
    match_mode: str
    case_sensitive: bool
    endpoint: str
    operation: str
    num_of_rows: int
    max_pages: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        api = raw.get("api", {})
        return cls(
            keywords=[k.strip() for k in raw.get("keywords", []) if k and k.strip()],
            exclude_keywords=[k.strip() for k in raw.get("exclude_keywords", []) if k and k.strip()],
            service_divs=[v.strip() for v in raw.get("service_divs", []) if v and v.strip()],
            lookback_days=int(raw.get("lookback_days", 1)),
            match_mode=raw.get("match_mode", "any"),
            case_sensitive=bool(raw.get("case_sensitive", False)),
            endpoint=api.get("endpoint", "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"),
            operation=api.get("operation", "getBidPblancListInfoServcPPSSrch"),
            num_of_rows=int(api.get("num_of_rows", 500)),
            max_pages=int(api.get("max_pages", 20)),
        )


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def fmt_api_dt(dt: datetime) -> str:
    """나라장터 API는 YYYYMMDDHHMM 12자리 문자열을 요구한다."""
    return dt.strftime("%Y%m%d%H%M")


def fetch_page(
    cfg: Config,
    service_key: str,
    begin: datetime,
    end: datetime,
    page_no: int,
    bid_ntce_nm: str = "",
) -> dict:
    """단일 페이지 조회. bid_ntce_nm 이 주어지면 공고명 부분검색으로 필터링."""
    url = f"{cfg.endpoint.rstrip('/')}/{cfg.operation}"
    params = {
        "serviceKey": service_key,
        "pageNo": page_no,
        "numOfRows": cfg.num_of_rows,
        "type": "json",
        "inqryDiv": 1,
        "inqryBgnDt": fmt_api_dt(begin),
        "inqryEndDt": fmt_api_dt(end),
    }
    if bid_ntce_nm:
        params["bidNtceNm"] = bid_ntce_nm

    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code == 401:
        raise RuntimeError(
            "API가 401 Unauthorized를 반환했습니다. 가능한 원인:\n"
            "  1) data.go.kr에서 '조달청_나라장터 입찰공고정보서비스' 활용신청이 승인 대기 중\n"
            "     → 마이페이지 → 오픈API → 활용신청현황 에서 상태를 확인\n"
            "  2) 승인 직후 서비스 반영까지 최대 2시간 정도 지연될 수 있음\n"
            "  3) NARA_SERVICE_KEY 값이 잘못 복사됨 (Decoding 키, 공백/개행 없이)"
        )
    resp.raise_for_status()

    ctype = resp.headers.get("Content-Type", "")
    text = resp.text
    if "json" not in ctype.lower() and not text.lstrip().startswith("{"):
        snippet = text[:400].replace("\n", " ")
        raise RuntimeError(
            f"API가 JSON이 아닌 응답을 반환했습니다 (SERVICE_KEY 오류 또는 요청 형식 오류일 가능성). "
            f"Content-Type={ctype}, 본문 일부={snippet!r}"
        )
    data = resp.json()

    header = (data.get("response") or {}).get("header", {})
    code = header.get("resultCode")
    msg = header.get("resultMsg", "")
    if code not in ("00", "0", None):
        raise RuntimeError(f"API 오류: resultCode={code}, resultMsg={msg}")
    return data


def extract_items(resp_json: dict) -> list[dict]:
    body = (resp_json.get("response") or {}).get("body") or {}
    items = body.get("items")
    if not items:
        return []
    if isinstance(items, dict) and "item" in items:
        items = items["item"]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return items


def total_count(resp_json: dict) -> int:
    body = (resp_json.get("response") or {}).get("body") or {}
    try:
        return int(body.get("totalCount", 0))
    except (TypeError, ValueError):
        return 0


def contains_kw(text: str, kw: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return kw in text
    return kw.lower() in text.lower()


def is_excluded(text: str, cfg: Config) -> bool:
    return any(contains_kw(text, kw, cfg.case_sensitive) for kw in cfg.exclude_keywords)


def passes_service_div(item: dict, cfg: Config) -> bool:
    """srvceDivNm(용역구분명) 화이트리스트 필터. 빈 리스트면 항상 통과."""
    if not cfg.service_divs:
        return True
    val = str(item.get("srvceDivNm") or "").strip()
    return val in cfg.service_divs


def all_keywords_match(text: str, cfg: Config) -> bool:
    return all(contains_kw(text, kw, cfg.case_sensitive) for kw in cfg.keywords)


def normalize_item(item: dict) -> dict:
    """대시보드에서 쓰기 편하게 주요 필드만 뽑아 정리."""
    def pick(*keys: str) -> str:
        for k in keys:
            v = item.get(k)
            if v not in (None, "", "null"):
                return str(v).strip()
        return ""

    return {
        "bidNtceNo": pick("bidNtceNo"),
        "bidNtceOrd": pick("bidNtceOrd"),
        "bidNtceNm": pick("bidNtceNm"),
        "ntceInsttNm": pick("ntceInsttNm"),
        "ntceInsttCd": pick("ntceInsttCd"),
        "dminsttNm": pick("dminsttNm"),
        "dminsttCd": pick("dminsttCd"),
        "bidMethdNm": pick("bidMethdNm"),
        "cntrctCnclsMthdNm": pick("cntrctCnclsMthdNm"),
        "ntceKindNm": pick("ntceKindNm"),
        "bsnsDivNm": pick("bsnsDivNm"),
        "srvceDivNm": pick("srvceDivNm"),
        "ppswGnrlSrvceYn": pick("ppswGnrlSrvceYn"),
        "bidNtceDt": pick("bidNtceDt"),
        "bidBeginDt": pick("bidBeginDt"),
        "bidClseDt": pick("bidClseDt"),
        "opengDt": pick("opengDt"),
        "asignBdgtAmt": pick("asignBdgtAmt"),
        "presmptPrce": pick("presmptPrce"),
        "bidNtceDtlUrl": pick("bidNtceDtlUrl"),
        "ntceInsttOfclNm": pick("ntceInsttOfclNm"),
        "ntceInsttOfclTelNo": pick("ntceInsttOfclTelNo"),
        "sucsfbidMthdNm": pick("sucsfbidMthdNm"),
    }


def fetch_by_keyword(
    cfg: Config,
    service_key: str,
    begin: datetime,
    end: datetime,
    keyword: str,
) -> list[dict]:
    """특정 키워드(공백이면 전체)로 페이지를 끝까지 순회."""
    collected: list[dict] = []
    label = f"'{keyword}'" if keyword else "(전체)"
    for page in range(1, cfg.max_pages + 1):
        print(f"    page {page} … ", end="", flush=True)
        data = fetch_page(cfg, service_key, begin, end, page, keyword)
        items = extract_items(data)
        tc = total_count(data)
        print(f"{len(items)} items (totalCount={tc})")
        if not items:
            break
        collected.extend(items)
        if len(items) < cfg.num_of_rows or len(collected) >= tc:
            break
        time.sleep(0.3)
    print(f"  키워드 {label}: 원본 {len(collected)}건")
    return collected


def merge_and_save(new_items: list[dict], date_str: str) -> tuple[int, int]:
    """같은 날짜 파일이 이미 있으면 merge, 없으면 새로 생성."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{date_str}.json"

    existing: dict[str, dict] = {}
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    key = f"{it.get('bidNtceNo')}-{it.get('bidNtceOrd')}"
                    existing[key] = it
        except (json.JSONDecodeError, OSError):
            existing = {}

    before = len(existing)
    for it in new_items:
        key = f"{it.get('bidNtceNo')}-{it.get('bidNtceOrd')}"
        existing[key] = it
    added = len(existing) - before

    payload = {
        "generated_at": now_kst().isoformat(timespec="seconds"),
        "date": date_str,
        "count": len(existing),
        "items": list(existing.values()),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return added, len(existing)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=None, help="최근 N일 조회 (config.lookback_days 대신)")
    p.add_argument("--date", type=str, default=None, help="YYYY-MM-DD 저장 파일명 지정")
    p.add_argument("--dry-run", action="store_true", help="저장 없이 콘솔로 요약만 출력")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config.load(CONFIG_PATH)

    service_key = os.environ.get("NARA_SERVICE_KEY", "").strip()
    if not service_key:
        print("ERROR: NARA_SERVICE_KEY 환경변수가 비어있습니다.", file=sys.stderr)
        print("  .env 에 NARA_SERVICE_KEY=... 로 추가하거나, GitHub Secrets 에 등록하세요.", file=sys.stderr)
        return 2

    days = args.days if args.days is not None else cfg.lookback_days
    now = now_kst()
    end = now
    begin = now - timedelta(days=max(days, 1))
    save_date = args.date or now.strftime("%Y-%m-%d")

    print(f"엔드포인트: {cfg.endpoint}/{cfg.operation}")
    print(f"조회 범위 (KST): {begin:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    print(f"키워드({cfg.match_mode}): {cfg.keywords or '(없음 → 전체 조회)'}")
    if cfg.exclude_keywords:
        print(f"제외 키워드: {cfg.exclude_keywords}")
    if cfg.service_divs:
        print(f"용역구분 화이트리스트(srvceDivNm): {cfg.service_divs}")
    print()

    # API 호출 전략:
    #  - 키워드가 없으면 단일 호출 (전체)
    #  - match_mode=any 이면 키워드별로 API 순회 후 병합 (부분검색 OR)
    #  - match_mode=all 이면 첫 키워드로만 API 조회 후 로컬에서 나머지도 포함되는지 필터
    if not cfg.keywords:
        search_keywords = [""]
    elif cfg.match_mode == "all":
        search_keywords = [cfg.keywords[0]]
    else:
        search_keywords = cfg.keywords

    merged_raw: dict[str, dict] = {}
    try:
        for kw in search_keywords:
            print(f"▶ 검색: {kw or '(전체)'}")
            for raw in fetch_by_keyword(cfg, service_key, begin, end, kw):
                key = f"{raw.get('bidNtceNo')}-{raw.get('bidNtceOrd')}"
                merged_raw[key] = raw
    except requests.RequestException as e:
        print(f"ERROR: 네트워크 오류 - {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    print(f"\n전체 중복제거 후 원본: {len(merged_raw)}건")

    filtered: list[dict] = []
    skipped_div = 0
    for raw in merged_raw.values():
        title = str(raw.get("bidNtceNm") or "")
        if not title:
            continue
        if is_excluded(title, cfg):
            continue
        if not passes_service_div(raw, cfg):
            skipped_div += 1
            continue
        if cfg.match_mode == "all" and cfg.keywords and not all_keywords_match(title, cfg):
            continue
        filtered.append(normalize_item(raw))

    if cfg.service_divs:
        print(f"용역구분 필터 제외: {skipped_div}건")
    print(f"최종 매치: {len(filtered)}건")

    if args.dry_run:
        for it in filtered[:20]:
            print(f"  - [{it['bidClseDt']}] {it['bidNtceNm']} / {it['ntceInsttNm']}")
        if len(filtered) > 20:
            print(f"  ... (+{len(filtered) - 20}건)")
        return 0

    added, total = merge_and_save(filtered, save_date)
    print(f"저장: data/{save_date}.json  (신규 {added}건, 총 {total}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
