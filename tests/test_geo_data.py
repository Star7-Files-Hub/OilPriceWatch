"""省级边界数据 + 「经纬度→省份」判定算法的回归测试。

`static/provinces.js` 是生成物，但它直接决定「自动定位判到哪个省」对不对，
所以当资产来测：解出来的几何必须仍能把城市用例判对。

⚠️ 判定逻辑有**两份实现**（`tools/build_province_shapes.py` 的 Python 参考实现，
   和 `static/geo.js` 的浏览器实现）。两份漂移了就会出现「生成时自校验通过、
   线上却判错」这种最难查的 bug，所以下面有一条测试专门跑 node 去对拍。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import config
from tools.build_province_shapes import (
    CITY_CASES,
    OUTSIDE_CASES,
    load_generated,
    locate,
    prepare,
)

ROOT = Path(__file__).resolve().parent.parent

NODE_CANDIDATES = (
    shutil.which("node"),
    r"C:\Users\7Star\.workbuddy-ai\binaries\node\versions\22.22.2-3\node.exe",
    r"C:\Program Files\nodejs\node.exe",
)

NODE_HARNESS = """
const path = require("path");
const PROJ = process.argv[2];
global.window = global;
require(path.join(PROJ, "static/provinces.js"));
const CNGeo = require(path.join(PROJ, "static/geo.js"));
const cases = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const out = cases.map((c) => CNGeo.locate(c.lat, c.lon));
process.stdout.write(JSON.stringify(out));
"""


def _node_bin() -> str | None:
    for cand in NODE_CANDIDATES:
        if cand and Path(cand).exists():
            return str(cand)
    return None


class TestProvinceShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shapes = load_generated()
        cls.table = prepare(cls.shapes)

    def test_cities_map_to_expected_province(self):
        bad = []
        for label, lat, lon, want in CITY_CASES:
            got = locate(self.table, lat, lon)
            if got != want:
                bad.append("%s(%.4f,%.4f) 期望 %s，实际 %s" % (label, lat, lon, want, got))
        self.assertEqual(bad, [], "判定结果与预期不符")

    def test_outside_china_returns_none(self):
        bad = []
        for label, lat, lon in OUTSIDE_CASES:
            got = locate(self.table, lat, lon)
            if got is not None:
                bad.append("%s(%.4f,%.4f) 判成了 %s" % (label, lat, lon, got))
        self.assertEqual(bad, [])

    def test_enclave_does_not_get_swallowed_by_neighbour(self):
        """澳门整块落在广东轮廓内，必须仍判成澳门。

        这份数据的环里没有"洞"，所以「命中多个取首个」会把澳门判成广东。
        修法是取面积最小的命中环 —— 这条测试守住那个修法。
        """
        self.assertEqual(locate(self.table, 22.1987, 113.5439), "澳门")
        self.assertEqual(locate(self.table, 22.2707, 113.5767), "广东")  # 珠海，别矫枉过正
        self.assertEqual(locate(self.table, 22.3193, 114.1694), "香港")

    def test_covers_every_province_we_sell_prices_for(self):
        """31 个有油价的省必须在边界数据里都有，否则那些省永远自动定位不到。"""
        names = {row[0] for row in self.table}
        missing = [p.name for p in config.PROVINCES if p.name not in names]
        self.assertEqual(missing, [], "边界数据缺少省份")

    def test_includes_taiwan_hongkong_macau(self):
        """领土完整性：港澳台必须在数据里（虽然它们没有油价数据）。"""
        names = {row[0] for row in self.table}
        for n in ("台湾", "香港", "澳门"):
            self.assertIn(n, names)

    def test_malformed_input_returns_none(self):
        self.assertIsNone(locate(self.table, float("nan"), 120.0))
        self.assertIsNone(locate(self.table, 0.0, 0.0))


class TestJsImplementationMatchesPython(unittest.TestCase):
    """跑 node 对拍：JS 实现必须与 Python 参考实现逐点一致。"""

    @classmethod
    def setUpClass(cls):
        cls.node = _node_bin()
        if not cls.node:
            raise unittest.SkipTest("本机没有 node，跳过 JS 对拍")

    def test_same_answers_as_python(self):
        cases = [
            {"lat": lat, "lon": lon, "want": want}
            for _, lat, lon, want in CITY_CASES
        ] + [
            {"lat": lat, "lon": lon, "want": None} for _, lat, lon in OUTSIDE_CASES
        ]
        expected = [c["want"] for c in cases]

        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            cases_file = Path(tmp) / "cases.json"
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            cases_file.write_text(
                json.dumps(cases, ensure_ascii=False), encoding="utf-8"
            )
            proc = subprocess.run(
                [self.node, str(harness), str(ROOT), str(cases_file)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(proc.returncode, 0, "node 执行失败: %s" % proc.stderr[:500])
        got = json.loads(proc.stdout)
        mismatched = [
            "%s: python=%s js=%s" % (cases[i]["want"], expected[i], got[i])
            for i in range(len(cases))
            if got[i] != expected[i]
        ]
        self.assertEqual(mismatched, [], "JS 与 Python 判定结果不一致")


if __name__ == "__main__":
    unittest.main()
