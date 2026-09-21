"""命令行入口：一条命令输出全国油价 + 下轮调价预测。

用法（在项目根目录）：
    python -m app.cli
    python -m app.cli --anchor 2026-09-10     # 指定上次真实调价日，窗口更准
    python -m app.cli --json                  # 输出机器可读结果
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from . import config
from .engine import forecast as fc
from .engine import holidays
from .engine import window as win
from .net import make_client
from .sources import qiyoujiage, sina


def collect(anchor: str | None = None) -> dict:
    with make_client() as client:
        realtime = sina.fetch_realtime(client)
        klines = sina.fetch_all_klines(client, limit=80)
        provinces = qiyoujiage.fetch_all_provinces(client)

    site_forecast = next(
        (p["forecast"] for p in provinces.values() if p.get("forecast")), None
    )
    # 没有人工锚点时，用程序推算的最近调价日（种子 +10 工作日递推，计入
    # 真实调休），让调价窗口和变化率窗口都对齐真实周期，不必手动填。
    # 人工锚点仍优先。
    cal = holidays.CN_2025_2026
    effective_anchor = anchor or win.last_anchor(cal=cal).isoformat()

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "realtime": realtime,
        "kline_counts": {k: len(v) for k, v in klines.items()},
        "provinces": provinces,
        "forecast": fc.build_forecast(klines, site_forecast, anchor=effective_anchor),
    }
    if anchor:
        result["window"] = win.next_window(date.fromisoformat(anchor), cal=cal)
    else:
        result["window"] = win.next_window(date.fromisoformat(effective_anchor), cal=cal)
        result["anchor_auto"] = effective_anchor
    return result


def render(data: dict) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 58)
    add(f"  全国油价监控 · {data['generated_at'][:10]}")
    add("=" * 58)

    add("")
    add("【国际原油 · 实时】")
    for key, label in (("brent", "布伦特"), ("wti", "WTI  "), ("sc", "上海原油")):
        item = data["realtime"].get(key)
        if item and item.get("price"):
            add(f"  {label}  {item['price']:>9.2f}   {item.get('date', '')}")
    fx = data["realtime"].get("usdcny")
    if fx and fx.get("price"):
        add(f"  USD/CNY  {fx['price']:>7.4f}")

    fcst = data["forecast"]
    add("")
    add("【下轮调价预测】")
    site = fcst.get("site")
    if site:
        liters = ""
        if site.get("yuan_per_liter_min") is not None:
            liters = f"  ({site['yuan_per_liter_min']}-{site['yuan_per_liter_max']} 元/升)"
        add(f"  网站预测    {site['direction']} {site['yuan_per_ton']:.0f} 元/吨{liters}")
    else:
        add("  网站预测    未取到")

    if fcst.get("weighted_ratio") is None:
        add("  自算变化率  数据不足")
    else:
        add(f"  自算变化率  {fcst['weighted_ratio'] * 100:+.2f}%")
        for key, ratio in fcst.get("ratios", {}).items():
            add(f"                {key:6s} {ratio * 100:+.2f}%   窗口 {fcst['window_sizes'].get(key, 0)} 日")
        me = fcst["self"]
        add(
            f"  自算幅度    {me['yuan_per_ton']:+.1f} 元/吨  "
            f"→ 92# {me['yuan_per_liter']['92']:+.3f} 元/升"
        )
        add(f"  判定        {me['status']}（阈值 {config.MIN_ADJUST_YUAN_PER_TON:.0f} 元/吨）")
        if fcst.get("implied_coefficient") is not None:
            add(
                f"  隐含系数    {fcst['implied_coefficient']:.1f} 元/吨 per 1%"
                f"   (当前用 {fcst['coefficient_used']:.0f})"
            )
        flag = {True: "一致", False: "不一致", None: "无法判定"}[fcst.get("consistent")]
        add(f"  交叉验证    {flag}")
    for note in fcst.get("notes", []):
        add(f"  ⚠ {note}")

    if data.get("window"):
        w = data["window"]
        add("")
        add("【调价窗口】")
        add(f"  上次调价 {w['anchor']}  →  下次 {w['next_date']}")
        add(
            f"  已过 {w['workdays_elapsed']}/{w['cycle_workdays']} 个工作日，"
            f"剩 {w['workdays_remaining']} 个"
        )

    add("")
    add("【全国油价】单位 元/升")
    current_region = None
    for province in config.PROVINCES:
        item = data["provinces"].get(province.slug) or {}
        prices = item.get("prices") or {}
        if province.region != current_region:
            current_region = province.region
            add(f"  -- {current_region} --")
        if prices.get("92") is None:
            add(f"  {province.name:5s} 数据缺失" + (f"  ({item.get('error', '')[:40]})" if item.get("error") else ""))
            continue
        add(
            f"  {province.name:5s} 92# {prices['92']:.2f}   "
            f"95# {prices['95']:.2f}   0# {prices['0']:.2f}"
        )

    add("")
    complete = sum(1 for p in data["provinces"].values() if p.get("complete"))
    add(f"  覆盖 {complete}/{len(config.PROVINCES)} 省")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全国油价监控与调价预测")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--anchor", default=None, help="上次真实调价日 YYYY-MM-DD，用于对齐调价窗口"
    )
    args = parser.parse_args(argv)

    try:
        data = collect(anchor=args.anchor)
    except Exception as exc:  # noqa: BLE001
        print(f"抓取失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
