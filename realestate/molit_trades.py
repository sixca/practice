#!/usr/bin/env python3
"""국토교통부 실거래가 공개 API로 관심 구역의 매매 실거래를 CSV로 뽑는다.

재개발 구역은 대부분 빌라(연립·다세대)·단독·다가구라서 아파트 API만으로는 부족하다.
이 스크립트는 연립다세대 / 단독다가구 / 아파트 / 토지 매매 API를 한 번에 조회하고,
zones.toml 의 법정동·지번으로 구역 내 거래만 남긴 뒤 "대지지분 평당가"를 계산한다.

사용 예:
    export MOLIT_API_KEY="공공데이터포털_일반인증키(Decoding)"
    python molit_trades.py --months 24
    python molit_trades.py --zones 한남4구역 한남5구역 --types rh sh --from 202401 --to 202609

Python 3.11+ , 외부 패키지 없음.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYEONG = 3.3058  # 1평 = 3.3058㎡

BASE = "https://apis.data.go.kr/1613000"
# 공공데이터포털에서 각각 "활용신청"을 해야 한다(같은 인증키로 4개 모두 사용 가능).
ENDPOINTS = {
    "rh": ("연립다세대", f"{BASE}/RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade"),
    "sh": ("단독다가구", f"{BASE}/RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade"),
    "apt": ("아파트", f"{BASE}/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"),
    "land": ("토지", f"{BASE}/RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade"),
}

# 2024년 영문 태그 개편 전후를 모두 받기 위해 후보 태그를 순서대로 본다.
FIELD_ALIASES = {
    "year": ["dealYear", "년"],
    "month": ["dealMonth", "월"],
    "day": ["dealDay", "일"],
    "dong": ["umdNm", "법정동"],
    "jibun": ["jibun", "지번"],
    "name": ["aptNm", "mhouseNm", "아파트", "연립다세대"],
    "house_type": ["houseType", "주택유형", "jimok", "지목"],
    "excl_area": ["excluUseAr", "전용면적"],
    "land_area": ["landAr", "plottageAr", "dealArea", "대지권면적", "대지면적", "거래면적"],
    "floor_area": ["totalFloorAr", "연면적"],
    "floor": ["floor", "층"],
    "build_year": ["buildYear", "건축년도"],
    "amount": ["dealAmount", "거래금액"],
    "cancel": ["cdealType", "해제여부"],
    "deal_type": ["dealingGbn", "거래유형"],
    "share": ["shareDealingType", "구분"],  # 토지 지분거래 여부
}

OUT_COLUMNS = [
    "구역", "유형", "계약일", "법정동", "지번", "건물명", "주택유형/지목",
    "전용㎡", "대지㎡", "연면적㎡", "층", "건축년도", "거래금액(만원)",
    "대지평당가(만원)", "전용평당가(만원)", "해제여부", "거래유형", "지번매칭",
]


@dataclass
class Zone:
    name: str
    lawd_cd: str
    dongs: list[str]
    bonbun: list[str] = field(default_factory=list)
    note: str = ""


def load_zones(path: Path) -> list[Zone]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return [
        Zone(
            name=z["name"],
            lawd_cd=str(z["lawd_cd"]),
            dongs=list(z.get("dongs", [])),
            bonbun=[str(b) for b in z.get("bonbun", [])],
            note=z.get("note", ""),
        )
        for z in raw.get("zone", [])
    ]


def month_range(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:])
    ey, em = int(end[:4]), int(end[4:])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def shift_month(ym: str, delta: int) -> str:
    idx = int(ym[:4]) * 12 + int(ym[4:]) - 1 + delta
    return f"{idx // 12}{idx % 12 + 1:02d}"


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_items(xml_text: str) -> tuple[list[dict], int]:
    """API 응답 XML → (item dict 목록, totalCount). 오류 응답이면 RuntimeError."""
    root = ET.fromstring(xml_text)
    code = (root.findtext(".//resultCode") or "").strip()
    msg = (root.findtext(".//resultMsg") or "").strip()
    # 인증 실패 등은 cmmMsgHeader 로 온다.
    err = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg")
    if err or (code and code not in ("00", "000")):
        raise RuntimeError(f"API 오류: {code} {msg} {err or ''}".strip())
    items = []
    for it in root.iter("item"):
        items.append({c.tag: (c.text or "").strip() for c in it})
    total = int(_num(root.findtext(".//totalCount")) or 0)
    return items, total


def normalize(raw: dict) -> dict:
    def pick(key):
        for tag in FIELD_ALIASES[key]:
            if raw.get(tag):
                return raw[tag]
        return ""

    y, m, d = pick("year"), pick("month"), pick("day")
    return {
        "date": f"{y}-{int(m):02d}-{int(d):02d}" if y and m and d else "",
        "dong": pick("dong"),
        "jibun": pick("jibun"),
        "name": pick("name"),
        "house_type": pick("house_type"),
        "excl_area": _num(pick("excl_area")),
        "land_area": _num(pick("land_area")),
        "floor_area": _num(pick("floor_area")),
        "floor": pick("floor"),
        "build_year": pick("build_year"),
        "amount": _num(pick("amount")),
        "cancel": pick("cancel"),
        "deal_type": pick("deal_type"),
        "share": pick("share"),
    }


def match_bonbun(jibun: str, bonbun: list[str]) -> str:
    """구역 지번 매칭 결과: '일치' / '후보'(마스킹 지번이라 확정 불가) / '' (불일치).

    bonbun 이 비어 있으면 법정동만으로 판단하므로 '동전체'를 돌려준다.
    단독다가구·토지는 지번이 '3**' 처럼 일부 가려져 나오므로 앞자리만 비교한다.
    """
    if not bonbun:
        return "동전체"
    main = jibun.split("-")[0].strip()
    if not main:
        return ""
    if "*" in main:
        visible = main.split("*")[0]
        width = len(main)
        if any(len(b) == width and b.startswith(visible) for b in bonbun):
            return "후보"
        return ""
    return "일치" if main in bonbun else ""


class Client:
    def __init__(self, key: str, cache_dir: Path | None, sleep: float = 0.15):
        # 포털의 Encoding 키를 넣어도 이중 인코딩되지 않도록 한 번 풀어준다.
        self.key = urllib.parse.unquote(key)
        self.cache_dir = cache_dir
        self.sleep = sleep
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str, params: dict) -> str:
        q = urllib.parse.urlencode({"serviceKey": self.key, **params})
        req = urllib.request.Request(f"{url}?{q}", headers={"Accept": "application/xml"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read().decode("utf-8")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise
                wait = 2 ** (attempt + 1)
                print(f"  ! 요청 실패({e}), {wait}초 후 재시도", file=sys.stderr)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def fetch_month(self, kind: str, lawd_cd: str, ym: str, use_cache: bool) -> list[dict]:
        cache = self.cache_dir / f"{kind}_{lawd_cd}_{ym}.json" if self.cache_dir else None
        if use_cache and cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))

        url = ENDPOINTS[kind][1]
        items: list[dict] = []
        page = 1
        while True:
            text = self._get(url, {"LAWD_CD": lawd_cd, "DEAL_YMD": ym,
                                   "pageNo": page, "numOfRows": 1000})
            batch, total = parse_items(text)
            items.extend(batch)
            time.sleep(self.sleep)
            if not batch or len(items) >= total:
                break
            page += 1

        if cache:
            cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return items


def to_row(zone: Zone, kind: str, t: dict, matched: str) -> dict:
    amt = t["amount"]
    land, excl = t["land_area"], t["excl_area"]
    return {
        "구역": zone.name,
        "유형": ENDPOINTS[kind][0],
        "계약일": t["date"],
        "법정동": t["dong"],
        "지번": t["jibun"],
        "건물명": t["name"],
        "주택유형/지목": t["house_type"],
        "전용㎡": excl or "",
        "대지㎡": land or "",
        "연면적㎡": t["floor_area"] or "",
        "층": t["floor"],
        "건축년도": t["build_year"],
        "거래금액(만원)": int(amt) if amt else "",
        "대지평당가(만원)": round(amt / (land / PYEONG)) if amt and land else "",
        "전용평당가(만원)": round(amt / (excl / PYEONG)) if amt and excl else "",
        "해제여부": t["cancel"],
        "거래유형": t["deal_type"] or t["share"],
        "지번매칭": matched,
    }


def summarize(rows: list[dict]) -> list[dict]:
    """구역 × 유형별 대지평당가 통계. 해제 거래는 제외."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["해제여부"] == "O":
            continue
        groups.setdefault((r["구역"], r["유형"]), []).append(r)
    out = []
    for (zone, kind), rs in sorted(groups.items()):
        ppp = sorted(r["대지평당가(만원)"] for r in rs if r["대지평당가(만원)"] != "")
        out.append({
            "구역": zone,
            "유형": kind,
            "거래건수": len(rs),
            "대지평당가_중위(만원)": round(statistics.median(ppp)) if ppp else "",
            "대지평당가_최저(만원)": ppp[0] if ppp else "",
            "대지평당가_최고(만원)": ppp[-1] if ppp else "",
            "최근거래일": max(r["계약일"] for r in rs),
        })
    return out


def bonbun_counts(rows: list[dict]) -> list[dict]:
    """구역 × 법정동 × 본번별 거래 건수. zones.toml 의 bonbun 을 채울 때 지도와 대조하는 용도."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        main = r["지번"].split("-")[0].strip() or "(없음)"
        groups.setdefault((r["구역"], r["법정동"], main), []).append(r)
    out = []
    for (zone, dong, main), rs in sorted(groups.items()):
        ppp = [r["대지평당가(만원)"] for r in rs if r["대지평당가(만원)"] != ""]
        out.append({
            "구역": zone, "법정동": dong, "본번": main, "거래건수": len(rs),
            "대지평당가_중위(만원)": round(statistics.median(ppp)) if ppp else "",
            "지번매칭": rs[0]["지번매칭"],
        })
    return out


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    # utf-8-sig: 엑셀에서 바로 열어도 한글이 깨지지 않는다.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    today = date.today()
    this_ym = f"{today.year}{today.month:02d}"

    p = argparse.ArgumentParser(description="관심 재개발·재건축 구역 매매 실거래 수집")
    p.add_argument("--key", default=os.environ.get("MOLIT_API_KEY"),
                   help="공공데이터포털 인증키 (기본: 환경변수 MOLIT_API_KEY)")
    p.add_argument("--config", type=Path, default=HERE / "zones.toml")
    p.add_argument("--zones", nargs="*", help="특정 구역만 (기본: 전체)")
    p.add_argument("--types", nargs="*", default=["rh", "sh", "apt"],
                   choices=list(ENDPOINTS), help="rh=연립다세대 sh=단독다가구 apt=아파트 land=토지")
    p.add_argument("--months", type=int, default=24, help="최근 N개월 (기본 24)")
    p.add_argument("--from", dest="ym_from", help="시작 계약월 YYYYMM (--months 대신)")
    p.add_argument("--to", dest="ym_to", default=this_ym, help="끝 계약월 YYYYMM (기본: 이번 달)")
    p.add_argument("--strict", action="store_true",
                   help="지번이 '일치'한 거래만 남김 (마스킹된 '후보'·'동전체' 제외)")
    p.add_argument("--out", type=Path, default=Path("trades.csv"))
    p.add_argument("--no-cache", action="store_true", help="캐시 무시하고 다시 조회")
    args = p.parse_args(argv)

    if not args.key:
        p.error("인증키가 없습니다. --key 또는 환경변수 MOLIT_API_KEY 를 지정하세요.")

    zones = load_zones(args.config)
    if args.zones:
        wanted = set(args.zones)
        zones = [z for z in zones if z.name in wanted]
        missing = wanted - {z.name for z in zones}
        if missing:
            p.error(f"zones.toml 에 없는 구역: {', '.join(sorted(missing))}")

    ym_from = args.ym_from or shift_month(args.ym_to, -(args.months - 1))
    months = month_range(ym_from, args.ym_to)
    # 신고기한(계약 후 30일) 때문에 최근 2개월치는 계속 늘어나므로 캐시하지 않는다.
    fresh = {args.ym_to, shift_month(args.ym_to, -1)}

    client = Client(args.key, None if args.no_cache else HERE / ".cache")
    lawd_codes = sorted({z.lawd_cd for z in zones})
    rows: list[dict] = []

    for kind in args.types:
        for lawd in lawd_codes:
            zs = [z for z in zones if z.lawd_cd == lawd]
            print(f"[{ENDPOINTS[kind][0]}] {lawd} {months[0]}~{months[-1]} ({len(months)}개월)")
            for ym in months:
                try:
                    raw = client.fetch_month(kind, lawd, ym, use_cache=ym not in fresh)
                except RuntimeError as e:
                    print(f"  ! {ym}: {e}", file=sys.stderr)
                    if "SERVICE" in str(e).upper() or "KEY" in str(e).upper():
                        print("  → 해당 API '활용신청' 여부와 인증키를 확인하세요.", file=sys.stderr)
                        return 2
                    continue
                for t in map(normalize, raw):
                    for z in zs:
                        if t["dong"] not in z.dongs:
                            continue
                        m = match_bonbun(t["jibun"], z.bonbun)
                        if not m or (args.strict and m != "일치"):
                            continue
                        rows.append(to_row(z, kind, t, m))

    rows.sort(key=lambda r: (r["구역"], r["유형"], r["계약일"]), reverse=False)
    write_csv(args.out, rows, OUT_COLUMNS)
    summary = summarize(rows)
    summary_path = args.out.with_name(args.out.stem + "_summary.csv")
    if summary:
        write_csv(summary_path, summary, list(summary[0].keys()))

    bonbun_rows = bonbun_counts(rows)
    bonbun_path = args.out.with_name(args.out.stem + "_bonbun.csv")
    if bonbun_rows:
        write_csv(bonbun_path, bonbun_rows, list(bonbun_rows[0].keys()))

    print(f"\n{len(rows)}건 → {args.out}")
    for s in summary:
        print(f"  {s['구역']:<8} {s['유형']:<6} {s['거래건수']:>4}건  "
              f"대지평당 중위 {s['대지평당가_중위(만원)']}만원  (최근 {s['최근거래일']})")
    if summary:
        print(f"요약 → {summary_path}")
    if any(not z.bonbun for z in zones) and bonbun_rows:
        print(f"본번별 건수 → {bonbun_path}  (bonbun 이 빈 구역은 이 파일을 지도와 대조해 zones.toml 에 채우세요)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
