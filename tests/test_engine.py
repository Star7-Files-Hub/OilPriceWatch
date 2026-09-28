"""计算引擎单元测试。纯函数，不碰网络，可离线跑。

    python -m unittest discover -s tests -t .
"""
import unittest
from datetime import date

from app import config
from app.engine import forecast as fc
from app.engine import holidays
from app.engine import window as win


def make_klines(closes, start_day=1):
    return [
        {"date": f"2026-08-{start_day + i:02d}", "close": float(c)}
        for i, c in enumerate(closes)
    ]


class TestWindowAverage(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(fc.window_average(make_klines(range(1, 21)), 10), 15.5)

    def test_insufficient(self):
        self.assertIsNone(fc.window_average(make_klines([1, 2]), 10))


class TestChangeRatio(unittest.TestCase):
    def test_rising(self):
        # 本轮 110..119 均值 114.5；上轮 100..109 均值 104.5
        ratio = fc.change_ratio(make_klines(list(range(100, 120))), 10)
        self.assertAlmostEqual(ratio, 10 / 104.5, places=6)

    def test_flat(self):
        self.assertAlmostEqual(fc.change_ratio(make_klines([100] * 20), 10), 0.0)

    def test_insufficient(self):
        self.assertIsNone(fc.change_ratio(make_klines([1] * 15), 10))


class TestSplitWithAnchor(unittest.TestCase):
    def test_anchor_slices_by_date(self):
        klines = make_klines(list(range(100, 120)))
        current, previous = fc.split_windows(klines, 10, anchor="2026-08-15")
        self.assertTrue(all(k["date"] > "2026-08-15" for k in current))
        self.assertEqual(len(previous), 10)

    def test_without_anchor_uses_last_cycle(self):
        klines = make_klines(list(range(100, 120)))
        current, previous = fc.split_windows(klines, 10)
        self.assertEqual(len(current), 10)
        self.assertEqual(current[0]["close"], 110.0)
        self.assertEqual(previous[0]["close"], 100.0)


class TestWorkdays(unittest.TestCase):
    def test_2026_09_18_is_friday(self):
        self.assertEqual(date(2026, 9, 18).weekday(), 4)

    def test_add_workdays_skips_weekend(self):
        self.assertEqual(win.add_workdays(date(2026, 9, 18), 1), date(2026, 9, 21))

    def test_count_workdays(self):
        self.assertEqual(
            win.count_workdays(date(2026, 9, 18), date(2026, 9, 25)), 5
        )

    def test_next_window_spans_ten_workdays(self):
        w = win.next_window(date(2026, 9, 18), today=date(2026, 9, 18))
        self.assertEqual(w["next_date"], "2026-10-02")
        self.assertEqual(w["workdays_remaining"], 10)
        self.assertEqual(w["progress"], 0.0)

    def test_holidays_shift_window(self):
        cal = holidays.HolidayCalendar(off=frozenset({date(2026, 9, 21)}))
        shifted = win.next_window(
            date(2026, 9, 18), today=date(2026, 9, 18), cal=cal
        )
        self.assertEqual(shifted["next_date"], "2026-10-05")


class TestBuildForecast(unittest.TestCase):
    def test_rising_matches_site_within_tolerance(self):
        klines = {
            "brent": make_klines(list(range(100, 120))),
            "wti": make_klines(list(range(100, 120))),
        }
        # 自算 0.0957 * 100 * 50 = 478.5，构造一个同值网站预测
        site = {"direction": "上调", "yuan_per_ton": 478.5}
        result = fc.build_forecast(klines, site)
        self.assertEqual(result["self"]["direction"], "上调")
        self.assertTrue(result["self"]["will_adjust"])
        self.assertTrue(result["consistent"])
        self.assertAlmostEqual(result["implied_coefficient"], 50.0, places=1)

    def test_direction_conflict_is_flagged(self):
        falling = make_klines(list(range(120, 100, -1)))
        klines = {"brent": falling, "wti": falling}
        site = {"direction": "上调", "yuan_per_ton": 500.0}
        result = fc.build_forecast(klines, site)
        self.assertEqual(result["self"]["direction"], "下调")
        self.assertFalse(result["consistent"])
        self.assertTrue(any("方向" in note for note in result["notes"]))

    def test_below_threshold_means_standstill(self):
        # 变化率极小 -> 幅度低于 50 元/吨 -> 搁浅
        klines = {"brent": make_klines([100.0] * 20), "wti": make_klines([100.0] * 20)}
        result = fc.build_forecast(klines, None)
        self.assertEqual(result["self"]["status"], "搁浅")
        self.assertFalse(result["self"]["will_adjust"])

    def test_no_data_is_handled(self):
        result = fc.build_forecast({"brent": [], "wti": []}, None)
        self.assertIsNone(result["weighted_ratio"])
        self.assertTrue(result["notes"])


class TestSiteForecastUnitConversion(unittest.TestCase):
    """回归：网站只给「元/升」时，幅度交叉验证不能被静默跳过。

    2026-09 网站文案改成「目前预计上涨0.30元/升-0.36元/升」，`yuan_per_ton` 变 null。
    老代码在 build_forecast 里取 `site_forecast.get("yuan_per_ton") or 0.0`
    ⇒ site_ton = 0 ⇒ deviation = None ⇒ consistent 退化成"只看方向"，
    而 UI 照样显示「与网站一致」——**幅度比对和 implied_coefficient 一起静默失效**。
    """

    RISING = {
        "brent": make_klines(list(range(100, 120))),
        "wti": make_klines(list(range(100, 120))),
    }

    def test_helper_derives_from_range_midpoint(self):
        ton, source = fc.site_yuan_per_ton(
            {"direction": "上调", "yuan_per_liter_min": 0.30, "yuan_per_liter_max": 0.36}
        )
        self.assertEqual(source, "derived")
        self.assertAlmostEqual(ton, 0.33 * config.LITERS_PER_TON["92"], places=6)

    def test_helper_prefers_direct_per_ton(self):
        ton, source = fc.site_yuan_per_ton(
            {"yuan_per_ton": 635.0, "yuan_per_liter_min": 0.1, "yuan_per_liter_max": 0.2}
        )
        self.assertEqual((ton, source), (635.0, "site"))

    def test_helper_returns_none_without_usable_magnitude(self):
        self.assertEqual(fc.site_yuan_per_ton({"direction": "上调"}), (None, None))
        self.assertEqual(fc.site_yuan_per_ton(None), (None, None))
        # 幅度 0 也当作"拿不到"：避免除零，也避免把"0 元/吨"当成比对基准
        self.assertEqual(
            fc.site_yuan_per_ton(
                {"yuan_per_liter_min": 0.0, "yuan_per_liter_max": 0.0}
            ),
            (None, None),
        )

    def test_per_liter_only_still_yields_deviation_and_coefficient(self):
        site = {
            "direction": "上调",
            "yuan_per_ton": None,
            "yuan_per_liter_min": 0.30,
            "yuan_per_liter_max": 0.36,
        }
        result = fc.build_forecast(self.RISING, site)
        self.assertEqual(result["site_yuan_per_ton_source"], "derived")
        self.assertAlmostEqual(result["site_yuan_per_ton"], 445.8, places=1)
        # 🔴 老代码这里 deviation 是 None —— 就是被静默跳过的证据
        self.assertIsNotNone(result["deviation"])
        self.assertLess(result["deviation"], 0.5)
        self.assertTrue(result["consistent"])
        # 隐含系数也得跟着活过来（校准 YIELD_COEFFICIENT 全靠它）
        self.assertAlmostEqual(result["implied_coefficient"], 46.6, places=1)

    def test_per_liter_only_magnitude_mismatch_is_flagged(self):
        site = {
            "direction": "上调",
            "yuan_per_liter_min": 0.10,
            "yuan_per_liter_max": 0.12,
        }
        result = fc.build_forecast(self.RISING, site)
        self.assertFalse(result["consistent"])
        self.assertGreaterEqual(result["deviation"], 0.5)
        self.assertTrue(any("幅度偏差" in n for n in result["notes"]))

    def test_per_liter_only_direction_conflict_is_flagged(self):
        site = {
            "direction": "下调",
            "yuan_per_liter_min": 0.30,
            "yuan_per_liter_max": 0.36,
        }
        result = fc.build_forecast(self.RISING, site)
        self.assertFalse(result["consistent"])
        self.assertTrue(any("方向" in n for n in result["notes"]))
        # 方向相反时算出来的"隐含系数"是负数，没有校准意义 ⇒ 宁可不给
        self.assertIsNone(result["implied_coefficient"])

    def test_missing_magnitude_is_unknown_not_consistent(self):
        result = fc.build_forecast(self.RISING, {"direction": "上调"})
        self.assertIsNone(result["site_yuan_per_ton"])
        self.assertIsNone(result["site_yuan_per_ton_source"])
        # 只能比方向时必须如实说"无法判定"，不能冒充"已校验一致"
        self.assertIsNone(result["consistent"])
        self.assertTrue(any("幅度" in n for n in result["notes"]))


class TestHolidayCalendar(unittest.TestCase):
    def test_make_up_workday_is_workday(self):
        # 2026-01-04 是周日，但属元旦调休补班 -> 必须算工作日
        self.assertTrue(holidays.CN_2025_2026.is_workday(date(2026, 1, 4)))

    def test_statutory_holiday_is_not_workday(self):
        # 2026-01-01 元旦，法定假日
        self.assertFalse(holidays.CN_2025_2026.is_workday(date(2026, 1, 1)))

    def test_normal_saturday_is_not_workday(self):
        # 2026-01-10 普通周六，不在任何假日/补班里
        self.assertFalse(holidays.CN_2025_2026.is_workday(date(2026, 1, 10)))

    def test_off_and_make_up_disjoint(self):
        cal = holidays.CN_2025_2026
        self.assertFalse(cal.off & cal.make_up_work)

    def test_2025_spring_festival_makeup(self):
        # 2025-02-08 周六是春节补班
        self.assertTrue(holidays.CN_2025_2026.is_workday(date(2025, 2, 8)))
        # 2025-01-29 春节假期
        self.assertFalse(holidays.CN_2025_2026.is_workday(date(2025, 1, 29)))


class TestAutoAnchorWithCalendar(unittest.TestCase):
    def test_anchor_before_today_next_after(self):
        today = date(2026, 9, 21)
        a = win.last_anchor(today=today, cal=holidays.CN_2025_2026)
        self.assertLessEqual(a, today)
        self.assertGreater(win.add_workdays(a, config.CYCLE_WORKDAYS, holidays.CN_2025_2026), today)

    def test_anchor_is_workday(self):
        # 推算出的锚点必须是真实调价日 -> 工作日（排除补班日落点不算，但锚点
        # 本身来自 +10 工作日递推，天然落在工作日上）
        today = date(2026, 9, 21)
        a = win.last_anchor(today=today, cal=holidays.CN_2025_2026)
        self.assertTrue(holidays.CN_2025_2026.is_workday(a))

    def test_next_date_is_workday(self):
        today = date(2026, 9, 21)
        w = win.next_window(
            win.last_anchor(today=today, cal=holidays.CN_2025_2026),
            today=today,
            cal=holidays.CN_2025_2026,
        )
        self.assertTrue(holidays.CN_2025_2026.is_workday(date.fromisoformat(w["next_date"])))


class TestConfigIntegrity(unittest.TestCase):
    def test_all_provinces_have_unique_slug_and_name(self):
        slugs = [p.slug for p in config.PROVINCES]
        names = [p.name for p in config.PROVINCES]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(names), len(set(names)))

    def test_thirty_one_provinces(self):
        self.assertEqual(len(config.PROVINCES), 31)

    def test_shaanxi_uses_special_slug(self):
        # 陕西是 shanxi-3，shanxi 是山西——踩过的坑，钉住
        self.assertEqual(config.PROVINCE_BY_NAME["陕西"].slug, "shanxi-3")
        self.assertEqual(config.PROVINCE_BY_NAME["山西"].slug, "shanxi")

    def test_crude_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(config.CRUDE_WEIGHTS.values()), 1.0, places=6)


class TestAutoAnchor(unittest.TestCase):
    def test_last_anchor_before_today_and_next_after(self):
        today = date(2026, 9, 21)
        a = win.last_anchor(today=today)
        self.assertLessEqual(a, today)
        self.assertGreater(win.add_workdays(a, config.CYCLE_WORKDAYS), today)

    def test_parse_adjustment_dates(self):
        html = "2024年12月18日 搁浅调整 2024年12月4日 2024年11月20日"
        dates = win.parse_adjustment_dates(html)
        self.assertIn(date(2024, 12, 18), dates)
        self.assertEqual(len(dates), 3)


if __name__ == "__main__":
    unittest.main()
