"""调价幅度预测：自算原油变化率，并与数据源现成的预测值交叉验证。

⚠️ 核心认知：国内成品油**不是实时跟随国际原油**。发改委每 10 个工作日
   比较一次"本轮三地原油均价 vs 上一轮均价"，变化率对应幅度 ≥50 元/吨才调，
   否则搁浅。所以这里做的是**确定性计算**，不是预测原油走势。

⚠️ 已知偏差来源：新浪没有迪拜、米纳斯原油，只能用布伦特 + WTI 近似三地均价。
   这正是必须做交叉验证的原因——自算值和网站预测值偏差过大时应当告警而非静默。
"""
from __future__ import annotations

from .. import config


def window_average(klines: list[dict], days: int) -> float | None:
    if len(klines) < days or days <= 0:
        return None
    return sum(k["close"] for k in klines[-days:]) / days


def split_windows(
    klines: list[dict], cycle: int, anchor: str | None = None
) -> tuple[list[dict], list[dict]]:
    """切出 (本轮窗口, 上一轮窗口)。

    给了 anchor（上次真实调价日）就按日期切，这样窗口与发改委口径对齐；
    没给就退化成"最近 cycle 条 vs 再往前 cycle 条"，会有窗口错位偏差。
    """
    if anchor:
        current = [k for k in klines if k["date"] > anchor]
        if current:
            start = len(klines) - len(current)
            previous = klines[max(0, start - cycle) : start]
            return current, previous
    return klines[-cycle:], klines[-2 * cycle : -cycle]


def change_ratio(
    klines: list[dict], cycle: int = config.CYCLE_WORKDAYS, anchor: str | None = None
) -> float | None:
    """本轮均价相对上一轮均价的变化率（0.035 表示 +3.5%）。"""
    current, previous = split_windows(klines, cycle, anchor)
    # ⚠️ 上一轮窗口必须是完整的 cycle 天，否则基准不成立。
    #    少了这个检查，数据不足会被算成"变化率 0%"，把缺失伪装成持平。
    if not current or len(previous) < cycle:
        return None
    cur_avg = sum(k["close"] for k in current) / len(current)
    prev_avg = sum(k["close"] for k in previous) / len(previous)
    if not prev_avg:
        return None
    return (cur_avg - prev_avg) / prev_avg


def crude_change_ratio(
    klines_by_symbol: dict[str, list[dict]],
    cycle: int = config.CYCLE_WORKDAYS,
    anchor: str | None = None,
) -> dict:
    """加权计算整体原油变化率。返回 {per_symbol, weighted, window_sizes}。"""
    per_symbol: dict[str, float] = {}
    window_sizes: dict[str, int] = {}
    for key, klines in klines_by_symbol.items():
        if not klines:
            continue
        current, _ = split_windows(klines, cycle, anchor)
        window_sizes[key] = len(current)
        ratio = change_ratio(klines, cycle, anchor)
        if ratio is not None:
            per_symbol[key] = ratio

    weighted = None
    if per_symbol:
        total_weight = sum(config.CRUDE_WEIGHTS.get(k, 0.0) for k in per_symbol)
        if total_weight > 0:
            weighted = (
                sum(per_symbol[k] * config.CRUDE_WEIGHTS.get(k, 0.0) for k in per_symbol)
                / total_weight
            )
    return {
        "per_symbol": per_symbol,
        "weighted": weighted,
        "window_sizes": window_sizes,
    }


def to_yuan_per_ton(
    ratio: float, coefficient: float = config.YIELD_COEFFICIENT
) -> float:
    return ratio * 100 * coefficient


def to_yuan_per_liter(yuan_per_ton: float, fuel: str) -> float:
    liters = config.LITERS_PER_TON.get(fuel)
    if not liters:
        return 0.0
    return yuan_per_ton / liters


def build_forecast(
    klines_by_symbol: dict[str, list[dict]],
    site_forecast: dict | None = None,
    cycle: int = config.CYCLE_WORKDAYS,
    anchor: str | None = None,
) -> dict:
    """产出完整预测结果，含自算值、网站值、隐含系数与一致性判定。"""
    crude = crude_change_ratio(klines_by_symbol, cycle, anchor)
    weighted = crude["weighted"]

    result: dict = {
        "cycle_workdays": cycle,
        "anchor": anchor,
        "window_sizes": crude["window_sizes"],
        "ratios": {k: round(v, 5) for k, v in crude["per_symbol"].items()},
        "weighted_ratio": round(weighted, 5) if weighted is not None else None,
        "coefficient_used": config.YIELD_COEFFICIENT,
        "self": None,
        "site": site_forecast,
        "implied_coefficient": None,
        "consistent": None,
        "notes": [],
    }

    if weighted is None:
        result["notes"].append("日K数据不足，无法自算变化率")
        return result

    # 周期刚开始时本轮样本很少，算出来的变化率会剧烈摆动——必须明说，别让用户当真
    partial = {k: v for k, v in crude["window_sizes"].items() if v < cycle}
    if partial:
        result["notes"].append(f"本轮窗口未走满 {partial}，预测会随每日行情大幅波动")

    yuan_per_ton = to_yuan_per_ton(weighted)
    result["self"] = {
        "yuan_per_ton": round(yuan_per_ton, 1),
        "yuan_per_liter": {
            fuel: round(to_yuan_per_liter(yuan_per_ton, fuel), 3)
            for fuel in config.LITERS_PER_TON
        },
        "direction": "上调" if yuan_per_ton > 0 else "下调",
        "will_adjust": abs(yuan_per_ton) >= config.MIN_ADJUST_YUAN_PER_TON,
        "status": (
            "上调"
            if yuan_per_ton >= config.MIN_ADJUST_YUAN_PER_TON
            else "下调"
            if yuan_per_ton <= -config.MIN_ADJUST_YUAN_PER_TON
            else "搁浅"
        ),
    }

    if site_forecast and site_forecast.get("yuan_per_ton") and weighted:
        result["implied_coefficient"] = round(
            site_forecast["yuan_per_ton"] / (weighted * 100), 1
        )

    if site_forecast and site_forecast.get("direction"):
        same_direction = site_forecast["direction"] == result["self"]["direction"]
        site_ton = site_forecast.get("yuan_per_ton") or 0.0
        deviation = (
            abs(result["self"]["yuan_per_ton"] - site_ton) / site_ton
            if site_ton
            else None
        )
        result["deviation"] = round(deviation, 3) if deviation is not None else None
        result["consistent"] = bool(same_direction and (deviation or 0) < 0.5)
        if not same_direction:
            result["notes"].append("方向与网站预测相反，需人工核查")
        elif deviation is not None and deviation >= 0.5:
            result["notes"].append(
                f"幅度偏差 {deviation:.0%}，系数 {config.YIELD_COEFFICIENT} 可能需校准"
            )

    return result
