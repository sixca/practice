import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import molit_trades as mt  # noqa: E402
import rank_listings as rl  # noqa: E402

RH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><buildYear>1990</buildYear><dealAmount>  95,000</dealAmount><dealDay>7</dealDay>
<dealMonth>3</dealMonth><dealYear>2026</dealYear><excluUseAr>40.5</excluUseAr><floor>2</floor>
<jibun>360-12</jibun><landAr>25.1</landAr><mhouseNm>OO빌라</mhouseNm><umdNm>보광동</umdNm>
<houseType>다세대</houseType><cdealType></cdealType><dealingGbn>중개거래</dealingGbn></item>
<item><dealAmount>200,000</dealAmount><dealDay>1</dealDay><dealMonth>4</dealMonth>
<dealYear>2026</dealYear><plottageAr>100</plottageAr><totalFloorAr>150</totalFloorAr>
<jibun>3**</jibun><umdNm>보광동</umdNm><houseType>단독</houseType></item>
</items><numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body></response>"""

ERR_XML = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>
<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""


class MolitTest(unittest.TestCase):
    def test_parse_and_normalize(self):
        items, total = mt.parse_items(RH_XML)
        self.assertEqual(total, 2)
        a, b = map(mt.normalize, items)
        self.assertEqual(a["date"], "2026-03-07")
        self.assertEqual(a["amount"], 95000)
        self.assertEqual(a["land_area"], 25.1)
        self.assertEqual(a["name"], "OO빌라")
        self.assertEqual(b["land_area"], 100)
        self.assertEqual(b["floor_area"], 150)

    def test_error_response(self):
        with self.assertRaises(RuntimeError):
            mt.parse_items(ERR_XML)

    def test_match_bonbun(self):
        self.assertEqual(mt.match_bonbun("360-12", ["360"]), "일치")
        self.assertEqual(mt.match_bonbun("361", ["360"]), "")
        self.assertEqual(mt.match_bonbun("3**", ["360"]), "후보")
        self.assertEqual(mt.match_bonbun("2**", ["360"]), "")
        self.assertEqual(mt.match_bonbun("3**", ["60"]), "")
        self.assertEqual(mt.match_bonbun("아무거나", []), "동전체")

    def test_row_ppp(self):
        z = mt.Zone("한남4구역", "11170", ["보광동"])
        t = mt.normalize(mt.parse_items(RH_XML)[0][0])
        row = mt.to_row(z, "rh", t, "일치")
        self.assertEqual(row["대지평당가(만원)"], round(95000 / (25.1 / mt.PYEONG)))

    def test_months(self):
        self.assertEqual(mt.month_range("202511", "202602"),
                         ["202511", "202512", "202601", "202602"])
        self.assertEqual(mt.shift_month("202601", -1), "202512")
        self.assertEqual(mt.shift_month("202609", -23), "202410")

    def test_zones_config_loads(self):
        zones = mt.load_zones(Path(mt.__file__).with_name("zones.toml"))
        self.assertIn("한남4구역", [z.name for z in zones])


class RankTest(unittest.TestCase):
    def test_full_redevelopment_math(self):
        row = {"구역": "Z", "매매가": "250,000", "감정가": "80000", "비례율": "",
               "조합원분양가": "180000", "신축공급㎡": "112", "전세보증금": "20000",
               "대지지분㎡": "33", "지위양도": "Y"}
        zone = {"default_ratio": 110, "nearby_new_ppp": 15000}
        r = rl.evaluate(row, zone, {})
        self.assertEqual(r["권리가액"], 88000)
        self.assertEqual(r["프리미엄"], 162000)
        self.assertEqual(r["추가분담금"], 92000)
        self.assertEqual(r["총매입가"], 342000)
        ppp = 342000 / (112 / rl.PYEONG)
        self.assertEqual(r["신축환산평당가"], round(ppp))
        self.assertEqual(r["안전마진%"], round((1 - ppp / 15000) * 100, 1))
        self.assertEqual(r["초기투자금"], 230000)

    def test_ranking_order(self):
        listings = rl.read_csv(Path(rl.__file__).with_name("listings_template.csv"))
        zones = {"한남4구역": {"nearby_new_ppp": 15000}}
        rows = rl.rank(listings, zones, {})
        self.assertEqual(rows[0]["매물명"].split("-")[0], "예시A")  # 안전마진 있는 매물 우선
        self.assertIn("지위양도 불가", rows[-1]["경고"])

    def test_trade_median(self):
        trades = [{"구역": "Z", "유형": "연립다세대", "대지평당가(만원)": v, "해제여부": c}
                  for v, c in [("10000", ""), ("12000", ""), ("99999", "O")]]
        m = rl.trade_medians(trades)
        self.assertEqual(m[("Z", "연립다세대")], 11000)
        r = rl.evaluate({"구역": "Z", "유형": "연립다세대", "매매가": "33000",
                         "대지지분㎡": str(3 * rl.PYEONG)}, {}, m)
        self.assertEqual(r["실거래중위대비%"], 100.0)


if __name__ == "__main__":
    unittest.main()
