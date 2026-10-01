#!/usr/bin/env python3
"""매물 CSV(중개사에게 받은 재개발·재건축 매물)를 같은 잣대로 환산해 가격 메리트 순으로 줄 세운다.

핵심 아이디어: 재개발 매물은 매매가만 보면 비교가 안 된다.
  권리가액     = 감정가 × 비례율
  프리미엄     = 매매가 − 권리가액
  추가분담금   = 조합원분양가 − 권리가액            (음수면 환급)
  총매입가     = 매매가 + 추가분담금                  (= 프리미엄 + 조합원분양가)
  신축환산평당가 = 총매입가 ÷ 배정 신축 공급평수      ← 구역이 달라도 비교 가능
  안전마진     = 1 − 신축환산평당가 ÷ 주변 신축 시세 평당가
  초기투자금   = 매매가 − 전세보증금 − 승계대출(이주비 등)

감정가가 아직 없는 구역(감정평가 전)은 대지지분 평당가로만 비교하고,
--trades 로 molit_trades.py 결과를 주면 구역 실거래 중위값 대비 몇 %인지도 붙인다.

사용 예:
    python rank_listings.py listings.csv
    python rank_listings.py listings.csv --trades trades.csv --out ranked.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYEONG = 3.3058

# 입력 CSV 컬럼 (금액은 모두 만원)
INPUT_COLUMNS = [
    "구역", "매물명", "유형", "매매가", "감정가", "비례율",
    "대지지분㎡", "전용㎡", "신축공급㎡", "조합원분양가",
    "전세보증금", "승계대출", "지위양도", "링크", "메모",
]

DERIVED_COLUMNS = [
    "순위", "권리가액", "프리미엄", "추가분담금", "총매입가", "신축환산평당가",
    "안전마진%", "초기투자금", "대지평당가", "실거래중위대비%", "점수기준", "경고",
]


def num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.replace(",", "").replace("%", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict]:
    # 엑셀에서 저장하면 cp949 로 저장되는 경우가 많아 둘 다 시도한다.
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{path}: 인코딩을 읽을 수 없습니다 (UTF-8 또는 CP949 로 저장하세요)")


def load_zone_settings(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return {z["name"]: z for z in tomllib.load(f).get("zone", [])}


def trade_medians(trades: list[dict]) -> dict[tuple[str, str], float]:
    """(구역, 유형) → 대지평당가 중위값. 해제 거래 제외."""
    groups: dict[tuple[str, str], list[float]] = {}
    for t in trades:
        if t.get("해제여부") == "O":
            continue
        v = num(t.get("대지평당가(만원)"))
        if v:
            groups.setdefault((t["구역"], t["유형"]), []).append(v)
            groups.setdefault((t["구역"], "*"), []).append(v)
    return {k: statistics.median(v) for k, v in groups.items()}


def evaluate(row: dict, zone: dict, medians: dict) -> dict:
    price = num(row.get("매매가"))
    appraisal = num(row.get("감정가"))
    ratio = num(row.get("비례율")) or num(str(zone.get("default_ratio", ""))) or 100.0
    land = num(row.get("대지지분㎡"))
    supply = num(row.get("신축공급㎡"))
    member_price = num(row.get("조합원분양가"))
    deposit = num(row.get("전세보증금")) or 0.0
    loan = num(row.get("승계대출")) or 0.0
    nearby = num(str(zone.get("nearby_new_ppp", ""))) or None

    out: dict = {c: "" for c in DERIVED_COLUMNS}
    warnings = []

    if price is None:
        out["경고"] = "매매가 없음"
        return out

    out["초기투자금"] = round(price - deposit - loan)

    if land:
        land_ppp = price / (land / PYEONG)
        out["대지평당가"] = round(land_ppp)
        med = medians.get((row.get("구역", ""), row.get("유형", ""))) \
            or medians.get((row.get("구역", ""), "*"))
        if med:
            out["실거래중위대비%"] = round(land_ppp / med * 100, 1)

    if appraisal is not None:
        rights = appraisal * ratio / 100
        out["권리가액"] = round(rights)
        out["프리미엄"] = round(price - rights)
        if member_price is not None:
            extra = member_price - rights
            total = price + extra
            out["추가분담금"] = round(extra)
            out["총매입가"] = round(total)
            if supply:
                new_ppp = total / (supply / PYEONG)
                out["신축환산평당가"] = round(new_ppp)
                if nearby:
                    out["안전마진%"] = round((1 - new_ppp / nearby) * 100, 1)
            else:
                warnings.append("신축공급㎡ 없음")
        else:
            warnings.append("조합원분양가 없음")
    else:
        warnings.append("감정가 없음→대지평당가로만 비교")

    if (row.get("지위양도") or "").strip().upper() in ("N", "X", "불가", "NO"):
        warnings.append("조합원 지위양도 불가(현금청산 위험)")
    if not row.get("지위양도"):
        warnings.append("지위양도 가능여부 미확인")

    out["경고"] = "; ".join(warnings)
    return out


def sort_key(r: dict):
    """정렬 우선순위: 지위양도 불가는 맨 뒤 → 안전마진 높은 순 → 신축환산평당가 낮은 순
    → 실거래중위대비 낮은 순 → 대지평당가 낮은 순."""
    blocked = "지위양도 불가" in r["경고"]
    if r["안전마진%"] != "":
        return (blocked, 0, -r["안전마진%"])
    if r["신축환산평당가"] != "":
        return (blocked, 1, r["신축환산평당가"])
    if r["실거래중위대비%"] != "":
        return (blocked, 2, r["실거래중위대비%"])
    if r["대지평당가"] != "":
        return (blocked, 3, r["대지평당가"])
    return (blocked, 4, 0)


BASIS = {0: "안전마진", 1: "신축환산평당가", 2: "실거래중위대비", 3: "대지평당가", 4: "-"}


def rank(listings: list[dict], zones: dict, medians: dict) -> list[dict]:
    rows = []
    for l in listings:
        if not (l.get("매물명") or l.get("매매가")):
            continue  # 빈 줄
        zone = zones.get(l.get("구역", ""), {})
        rows.append({**l, **evaluate(l, zone, medians)})
    rows.sort(key=sort_key)
    for i, r in enumerate(rows, 1):
        r["순위"] = i
        r["점수기준"] = BASIS[sort_key(r)[1]]
    return rows


def fmt(v) -> str:
    if v == "" or v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def print_table(rows: list[dict]) -> None:
    cols = [("순위", 4), ("구역", 10), ("매물명", 14), ("매매가", 9), ("프리미엄", 9),
            ("총매입가", 9), ("신축환산평당가", 9), ("안전마진%", 7), ("대지평당가", 9),
            ("초기투자금", 9)]
    print("  ".join(name[:w].ljust(w) for name, w in cols))
    for r in rows:
        cells = []
        for name, w in cols:
            v = r.get(name)
            if name == "매매가":
                v = int(num(v)) if num(v) is not None else ""
            cells.append(fmt(v)[:w].ljust(w))
        print("  ".join(cells))
        if r["경고"]:
            print(f"      ⚠ {r['경고']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="재개발·재건축 매물 가격 메리트 순위")
    p.add_argument("listings", type=Path, help="매물 CSV (listings_template.csv 참고)")
    p.add_argument("--trades", type=Path, help="molit_trades.py 가 만든 trades.csv")
    p.add_argument("--config", type=Path, default=HERE / "zones.toml")
    p.add_argument("--out", type=Path, default=Path("ranked.csv"))
    args = p.parse_args(argv)

    listings = read_csv(args.listings)
    zones = load_zone_settings(args.config)
    medians = trade_medians(read_csv(args.trades)) if args.trades else {}

    rows = rank(listings, zones, medians)
    extra = [c for c in (listings[0].keys() if listings else []) if c not in INPUT_COLUMNS]
    columns = ["순위"] + INPUT_COLUMNS + extra + [c for c in DERIVED_COLUMNS if c != "순위"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print_table(rows)
    print(f"\n{len(rows)}건 → {args.out}  (금액 단위: 만원)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
