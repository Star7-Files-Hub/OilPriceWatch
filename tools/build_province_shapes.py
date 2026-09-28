#!/usr/bin/env python
"""把省级边界 GeoJSON 压成浏览器端资产 `static/provinces.js`，并自校验。

为什么要这个脚本（而不是直接提交一份明文经纬度表）：
- 明文经纬度有 32346 个点，写成 `[118.0293,27.1431]` 大约 520KB，塞进 H5 太重。
- 源数据（ECharts）本身用 **ZigZag + delta 压缩**把同样的几何压到 ~54KB，
  代价只是运行时解回来要多 15 行 JS。所以直接沿用压缩串，不做解码落地。

用法：
    python tools/build_province_shapes.py            # 生成 + 自校验
    python tools/build_province_shapes.py --check    # 只自校验，不写文件

输入：tools/data/china_provinces.json
     （Apache ECharts 4.9.0 `map/json/china.json`，Apache-2.0；
      含 34 个省级单位，台湾/香港/澳门齐全）
输出：static/provinces.js → `window.CN_PROVINCE_SHAPES = { 省名: [[ox, oy, "压缩串"], ...] }`

⚠️ 环被**拍平**成一维列表：省级行政区之间是共享边界的独立多边形，不存在"洞"，
   所以判定时逐环做射线法取首个命中即可，不需要保留 polygon→ring 的层级。
⚠️ 本模块同时是**判定算法的 Python 参考实现**（`decode_ring` / `locate`），
   供 tests/test_geo_data.py 断言「生成出来的文件仍能正确判省」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "data" / "china_provinces.json"
OUT = ROOT / "static" / "provinces.js"
SCALE = 1024.0

HEADER = """\
/* 省级边界数据（供浏览器把定位经纬度判成省份）。
 *
 * 数据源：Apache ECharts 4.9.0 map/json/china.json（Apache-2.0 许可），
 *        由 tools/build_province_shapes.py 生成 —— 请勿手改本文件。
 *
 * 坐标为 ECharts 的 ZigZag + delta 压缩串，用 CNGeo.decodeRing() 解回 [lon, lat]。
 * 环已拍平成一维数组（省级行政区之间无"洞"，逐环射线法判定即可）。
 * 坐标系与浏览器定位同为 WGS-84 量级；即便上游是 GCJ-02，偏移也只有数百米，
 * 对"判到省"这件事无影响。
 */
"""

# 省会/直辖市中心点 → 期望省份。覆盖全部 34 个省级单位，用来卡住解码与判定算法。
CITY_CASES: tuple[tuple[str, float, float, str], ...] = (
    ("北京", 39.9042, 116.4074, "北京"),
    ("天津", 39.0842, 117.2009, "天津"),
    ("石家庄", 38.0428, 114.5149, "河北"),
    ("太原", 37.8706, 112.5489, "山西"),
    ("呼和浩特", 40.8426, 111.7492, "内蒙古"),
    ("沈阳", 41.8057, 123.4315, "辽宁"),
    ("长春", 43.8171, 125.3235, "吉林"),
    ("哈尔滨", 45.8038, 126.5349, "黑龙江"),
    ("上海", 31.2304, 121.4737, "上海"),
    ("南京", 32.0603, 118.7969, "江苏"),
    ("杭州", 30.2741, 120.1551, "浙江"),
    ("合肥", 31.8206, 117.2272, "安徽"),
    ("福州", 26.0745, 119.2965, "福建"),
    ("南昌", 28.6820, 115.8579, "江西"),
    ("济南", 36.6512, 117.1201, "山东"),
    ("郑州", 34.7466, 113.6254, "河南"),
    ("武汉", 30.5928, 114.3055, "湖北"),
    ("长沙", 28.2282, 112.9388, "湖南"),
    ("广州", 23.1291, 113.2644, "广东"),
    ("南宁", 22.8170, 108.3665, "广西"),
    ("海口", 20.0444, 110.1999, "海南"),
    ("重庆", 29.5630, 106.5516, "重庆"),
    ("成都", 30.5728, 104.0668, "四川"),
    ("贵阳", 26.6470, 106.6302, "贵州"),
    ("昆明", 24.8801, 102.8329, "云南"),
    ("拉萨", 29.6520, 91.1721, "西藏"),
    ("西安", 34.3416, 108.9398, "陕西"),
    ("兰州", 36.0611, 103.8343, "甘肃"),
    ("西宁", 36.6171, 101.7782, "青海"),
    ("银川", 38.4872, 106.2309, "宁夏"),
    ("乌鲁木齐", 43.8256, 87.6168, "新疆"),
    ("台北", 25.0330, 121.5654, "台湾"),
    ("香港", 22.3193, 114.1694, "香港"),
    ("澳门", 22.1987, 113.5439, "澳门"),
    # 下面几个专卡「被邻省轮廓盖住 / 紧贴边界」的坑：澳门整块落在广东轮廓内，
    # 廊坊被北京和天津夹着。命中多个时取面积最小者才判得对。
    ("珠海", 22.2707, 113.5767, "广东"),
    ("深圳", 22.5431, 114.0579, "广东"),
    ("廊坊", 39.5377, 116.6835, "河北"),
    ("保定", 38.8671, 115.4845, "河北"),
)

# 境外/海上，应判不出省份
OUTSIDE_CASES: tuple[tuple[str, float, float], ...] = (
    ("东京", 35.6762, 139.6503),
    ("首尔", 37.5665, 126.9780),
    ("太平洋", 15.0, 150.0),
)


# --- 判定算法的 Python 参考实现（与 static/geo.js 保持一致）-----------------


def decode_ring(ox: float, oy: float, s: str) -> list[tuple[float, float]]:
    """解开 ECharts 的 ZigZag + delta 压缩串。"""
    pts: list[tuple[float, float]] = []
    px, py = ox, oy
    for i in range(0, len(s) - 1, 2):
        x = ord(s[i]) - 64
        y = ord(s[i + 1]) - 64
        x = (x >> 1) ^ (-(x & 1))
        y = (y >> 1) ^ (-(y & 1))
        px += x
        py += y
        pts.append((px / SCALE, py / SCALE))
    return pts


def _in_ring(x: float, y: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _ring_area(pts: list[tuple[float, float]]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def prepare(shapes: dict) -> list[tuple[str, list, tuple, float]]:
    """拍平成 (省名, 顶点, bbox, 面积) 列表，与 static/geo.js 的 prepare() 一一对应。"""
    out = []
    for name, rings in shapes.items():
        for ox, oy, enc in rings:
            pts = decode_ring(ox, oy, enc)
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append((name, pts, (min(xs), min(ys), max(xs), max(ys)), _ring_area(pts)))
    return out


def locate(shapes: dict, lat: float, lon: float) -> str | None:
    """经纬度 → 省名；判不出返回 None。

    ⚠️ **命中多个省时取面积最小的那个**。这份数据的环里没有"洞"，直辖市/特别行政区
    是被邻省的轮廓**整个盖住**的（实测澳门同时落在广东的环内），只按遍历顺序取首个
    命中会把澳门判成广东。面积最小的那个才是真正的答案。
    （也不用绕向来判洞：这份数据外环是顺时针，不符合 RFC 7946，绕向不可靠。）
    """
    table = shapes if isinstance(shapes, list) else prepare(shapes)
    best: str | None = None
    best_area = float("inf")
    for name, pts, bbox, area in table:
        if lon < bbox[0] or lon > bbox[2] or lat < bbox[1] or lat > bbox[3]:
            continue
        if area >= best_area:
            continue
        if _in_ring(lon, lat, pts):
            best, best_area = name, area
    return best


def selftest(shapes: dict) -> list[str]:
    """返回失败信息列表（空 = 全过）。"""
    bad: list[str] = []
    for label, lat, lon, want in CITY_CASES:
        got = locate(shapes, lat, lon)
        if got != want:
            bad.append("%s(%.4f,%.4f) 期望 %s，实际 %s" % (label, lat, lon, want, got))
    for label, lat, lon in OUTSIDE_CASES:
        got = locate(shapes, lat, lon)
        if got is not None:
            bad.append("%s(%.4f,%.4f) 期望判不出，实际 %s" % (label, lat, lon, got))
    return bad


# --- 生成 -----------------------------------------------------------------


def build_shapes(src: Path = SRC) -> dict:
    data = json.loads(src.read_text(encoding="utf-8"))
    if not data.get("UTF8Encoding") or data.get("UTF8Scale") is not None:
        # 源格式变了 ⇒ 解码参数会不对，宁可直接失败也别生成错数据
        raise ValueError(
            "源数据格式与预期不符：UTF8Encoding=%r UTF8Scale=%r"
            % (data.get("UTF8Encoding"), data.get("UTF8Scale"))
        )

    shapes: dict[str, list] = {}
    for feat in data["features"]:
        name = (feat.get("properties") or {}).get("name")
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        offsets = geom.get("encodeOffsets")
        if not name or not coords or not offsets:
            continue
        if gtype == "Polygon":
            coords, offsets = [coords], [offsets]
        elif gtype != "MultiPolygon":
            continue

        flat: list[list] = []
        for poly, poly_off in zip(coords, offsets):
            for ring, off in zip(poly, poly_off):
                if ring:
                    flat.append([off[0], off[1], ring])
        if flat:
            shapes[name] = flat
    return shapes


def load_generated(path: Path = OUT) -> dict:
    """把生成出来的 provinces.js 读回来（剥掉 JS 外壳取 JSON）。"""
    text = path.read_text(encoding="utf-8")
    marker = "window.CN_PROVINCE_SHAPES = "
    start = text.index(marker) + len(marker)
    end = text.rindex(";")
    return json.loads(text[start:end])


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    shapes = build_shapes()

    bad = selftest(shapes)
    if bad:
        print("!! 自校验未通过，不落盘：", file=sys.stderr)
        for b in bad:
            print("   -", b, file=sys.stderr)
        return 1

    if not check_only:
        body = json.dumps(shapes, ensure_ascii=False, separators=(",", ":"))
        OUT.write_text(
            HEADER + "window.CN_PROVINCE_SHAPES = " + body + ";\n",
            encoding="utf-8",
            newline="\n",
        )
        # 回读一次，确保落盘的内容仍能判对（防止序列化/换行出岔子）
        bad = selftest(load_generated())
        if bad:
            print("!! 落盘后的文件自校验失败：", file=sys.stderr)
            for b in bad:
                print("   -", b, file=sys.stderr)
            return 1

    rings = sum(len(v) for v in shapes.values())
    points = sum(len(r[2]) // 2 for v in shapes.values() for r in v)
    print(
        "省份 %d 个 / 环 %d 条 / 顶点 %d 个；%d 个城市用例全部通过%s"
        % (
            len(shapes),
            rings,
            points,
            len(CITY_CASES) + len(OUTSIDE_CASES),
            "（--check，未写文件）" if check_only else "；已写出 %s（%.1f KB）"
            % (OUT.relative_to(ROOT), OUT.stat().st_size / 1024),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
