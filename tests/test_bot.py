"""Bot 离线单测：省份识别与推送文案，不碰网络/数据库。"""
import unittest

from app import bot


class TestFindProvince(unittest.TestCase):
    def test_full_name(self):
        self.assertEqual(bot._find_province("浙江").slug, "zhejiang")

    def test_with_suffix(self):
        self.assertEqual(bot._find_province("广东省").slug, "guangdong")
        self.assertEqual(bot._find_province("北京市").slug, "beijing")

    def test_special_slug(self):
        # 陕西是 shanxi-3
        self.assertEqual(bot._find_province("陕西").slug, "shanxi-3")


class TestBuildDigest(unittest.TestCase):
    def test_digest_includes_forecast_and_window(self):
        snap = {
            "forecast": {
                "self": {
                    "direction": "上调",
                    "yuan_per_ton": 607.9,
                    "yuan_per_liter": {"92": 0.45, "95": 0.448, "0": 0.514},
                },
                "site": {"direction": "上调", "yuan_per_ton": 635.0},
            },
            "window": {
                "next_date": "2026-09-23",
                "workdays_remaining": 2,
            },
        }
        text = bot.build_digest(snap)
        self.assertIn("上调", text)
        self.assertIn("608", text)  # 607.9 四舍五入到整数
        self.assertIn("635", text)  # 网站预测值
        self.assertIn("2026-09-23", text)
        self.assertIn("2 个工作日", text)


if __name__ == "__main__":
    unittest.main()
