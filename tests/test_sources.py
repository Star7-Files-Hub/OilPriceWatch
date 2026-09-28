"""数据源解析测试（离线，不碰网络）。

重点锁住汽油价格网**预测文案的两种格式**——该站改过版式，且用词会变，
解析器必须同时吃「预计上调油价XXX元/吨」和「预计上涨X元/升-Y元/升」。
"""
import unittest

from app import config
from app.sources import qiyoujiage as qy

ZJ = config.PROVINCE_BY_NAME["浙江"]


class TestForecastParsing(unittest.TestCase):
    def test_new_liter_format(self):
        html = "<span>目前预计上涨0.30元/升-0.36元/升,大家相互转告油价又涨了。</span>"
        f = qy.parse_province(html, ZJ)["forecast"]
        self.assertIsNotNone(f)
        self.assertEqual(f["direction"], "上调")
        self.assertIsNone(f["yuan_per_ton"])
        self.assertAlmostEqual(f["yuan_per_liter_min"], 0.30)
        self.assertAlmostEqual(f["yuan_per_liter_max"], 0.36)

    def test_new_down_direction_normalized(self):
        html = "<span>预计下跌0.10元/升-0.12元/升</span>"
        f = qy.parse_province(html, ZJ)["forecast"]
        self.assertEqual(f["direction"], "下调")

    def test_old_ton_format(self):
        html = "<span>预计下调油价320元/吨(0.24元/升-0.29元/升)</span>"
        f = qy.parse_province(html, ZJ)["forecast"]
        self.assertEqual(f["direction"], "下调")
        self.assertAlmostEqual(f["yuan_per_ton"], 320.0)
        self.assertAlmostEqual(f["yuan_per_liter_min"], 0.24)
        self.assertAlmostEqual(f["yuan_per_liter_max"], 0.29)

    def test_no_forecast_returns_none(self):
        html = "<span>今天天气不错</span>"
        self.assertIsNone(qy.parse_province(html, ZJ)["forecast"])


class TestPriceParsing(unittest.TestCase):
    def test_three_prices_complete(self):
        html = (
            "<div>92#汽油</div><div>8.58</div>"
            "<div>95#汽油</div><div>9.12</div>"
            "<div>0#柴油</div><div>8.28</div>"
        )
        p = qy.parse_province(html, ZJ)
        self.assertEqual(p["prices"]["92"], 8.58)
        self.assertEqual(p["prices"]["95"], 9.12)
        self.assertEqual(p["prices"]["0"], 8.28)
        self.assertTrue(p["complete"])

    def test_missing_price_not_complete(self):
        html = "<div>92#汽油</div><div>8.58</div>"
        p = qy.parse_province(html, ZJ)
        self.assertFalse(p["complete"])

    def test_98_parsed(self):
        html = (
            "<div>92#汽油</div><div>8.58</div>"
            "<div>95#汽油</div><div>9.12</div>"
            "<div>98#汽油</div><div>10.62</div>"
            "<div>0#柴油</div><div>8.28</div>"
        )
        p = qy.parse_province(html, ZJ)
        self.assertEqual(p["prices"]["98"], 10.62)
        self.assertTrue(p["complete"])

    def test_missing_98_still_complete(self):
        # 98# 不是必需油品，缺了不影响 complete（否则覆盖率会被拖垮）
        html = (
            "<div>92#汽油</div><div>8.58</div>"
            "<div>95#汽油</div><div>9.12</div>"
            "<div>0#柴油</div><div>8.28</div>"
        )
        p = qy.parse_province(html, ZJ)
        self.assertIsNone(p["prices"]["98"])
        self.assertTrue(p["complete"])


if __name__ == "__main__":
    unittest.main()
