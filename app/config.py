"""OilPriceWatch 全局配置。

只放常量与静态映射，不放逻辑。改数据源地址、省份、调价参数都在这里。
"""
from __future__ import annotations

from typing import NamedTuple


class Province(NamedTuple):
    slug: str
    name: str
    region: str


# 31 个省级行政区。
# ⚠️ 陕西的 slug 是 "shanxi-3"（"shanxi" 被山西占用），这是汽油价格网的命名，
#    直接猜 "shaanxi" 会 404。改动此表前先去首页扒一次导航链接。
PROVINCES: tuple[Province, ...] = (
    Province("beijing", "北京", "华北"),
    Province("tianjin", "天津", "华北"),
    Province("hebei", "河北", "华北"),
    Province("shanxi", "山西", "华北"),
    Province("neimenggu", "内蒙古", "华北"),
    Province("liaoning", "辽宁", "东北"),
    Province("jilin", "吉林", "东北"),
    Province("heilongjiang", "黑龙江", "东北"),
    Province("shanghai", "上海", "华东"),
    Province("jiangsu", "江苏", "华东"),
    Province("zhejiang", "浙江", "华东"),
    Province("anhui", "安徽", "华东"),
    Province("fujian", "福建", "华东"),
    Province("jiangxi", "江西", "华东"),
    Province("shandong", "山东", "华东"),
    Province("henan", "河南", "华中"),
    Province("hubei", "湖北", "华中"),
    Province("hunan", "湖南", "华中"),
    Province("guangdong", "广东", "华南"),
    Province("guangxi", "广西", "华南"),
    Province("hainan", "海南", "华南"),
    Province("chongqing", "重庆", "西南"),
    Province("sichuan", "四川", "西南"),
    Province("guizhou", "贵州", "西南"),
    Province("yunnan", "云南", "西南"),
    Province("xizang", "西藏", "西南"),
    Province("shanxi-3", "陕西", "西北"),
    Province("gansu", "甘肃", "西北"),
    Province("qinghai", "青海", "西北"),
    Province("ningxia", "宁夏", "西北"),
    Province("xinjiang", "新疆", "西北"),
)

PROVINCE_BY_SLUG: dict[str, Province] = {p.slug: p for p in PROVINCES}
PROVINCE_BY_NAME: dict[str, Province] = {p.name: p for p in PROVINCES}

# --- 数据源地址 ---------------------------------------------------------

# ⚠️ 上游证书配置错误：https://www.qiyoujiage.com 与 https://qiyoujiage.com 的
#    证书主机名都不匹配（CERTIFICATE_VERIFY_FAILED: Hostname mismatch），
#    开着校验必挂。实测 http 正常返回且内容一致，故走 http（公开价格数据，无密钥）。
QIYOUJIAGE_BASE = "http://www.qiyoujiage.com"
QIYOUJIAGE_PROVINCE_URL = QIYOUJIAGE_BASE + "/{slug}.shtml"
QIYOUJIAGE_SCHEDULE_URL = QIYOUJIAGE_BASE + "/tz/2025"

SINA_QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
SINA_REFERER = "https://finance.sina.com.cn"

# 外盘期货日K。返回 JSONP，形如 `var _t=([{...},...])`，按时间升序，最新在末尾。
# 实测：OIL 覆盖 2016-09 至今（2587 条），CL 覆盖 1996 至今。
SINA_KLINE_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}"
)
SINA_KLINE_SYMBOLS: dict[str, str] = {"brent": "OIL", "wti": "CL"}

# 新浪行情代码。⚠️ 新浪没有迪拜、米纳斯原油（hf_DINIW / hf_DUBAI / hf_OMAN 全为空），
#    因此"三地原油"只能用 布伦特 + WTI + 上海原油 近似，这也是必须做交叉验证的原因。
SINA_CODES: dict[str, str] = {
    "brent": "hf_OIL",
    "wti": "hf_CL",
    "sc": "nf_SC0",
    "usdcny": "fx_susdcny",
}

# 自算变化率时各油种权重（布伦特为主，贴近发改委以布伦特为锚的实际做法）
CRUDE_WEIGHTS: dict[str, float] = {"brent": 0.6, "wti": 0.25, "sc": 0.15}

# --- 调价机制参数（2016《石油价格管理办法》）-----------------------------

CYCLE_WORKDAYS = 10
MIN_ADJUST_YUAN_PER_TON = 50.0

# 吨 → 升。用于把"元/吨"折算成用户看得懂的"元/升"。
# 98# 是近似值（密度与 95# 接近），仅影响折算展示，不影响调价幅度计算。
LITERS_PER_TON: dict[str, float] = {
    "92": 1351.0,
    "95": 1357.0,
    "98": 1360.0,
    "0": 1183.0,
}

# 原油变化率(%) → 国内调价幅度(元/吨) 的换算系数。
# ⚠️ 待标定：发改委未公开完整公式，只能靠真实数据校准，别当成定论。
#    2026-09-19 实测：自算 +13.27% vs 网站预测 635 元/吨 → 隐含系数 **47.9**，
#    与当前值偏差 4.4%；自算的 92# +0.491 元/升落在网站给的 0.48-0.57 区间内。
#    单点样本不足以下结论，等多轮调价数据后再决定是否改成 47.9。
YIELD_COEFFICIENT = 50.0

# 网站只给「元/升」时，按哪个油品的吨升比折回「元/吨」。
# 该站文案（「目前预计上涨0.30元/升-0.36元/升」）说的是 92# 汽油。
# 🔴 换这个值会直接改变交叉验证的幅度基准，改前先核对网站原文。
SITE_FORECAST_FUEL = "92"

# 网站页面上的油品标签 -> 内部键
FUEL_LABELS: dict[str, str] = {
    "92#汽油": "92",
    "95#汽油": "95",
    "98#汽油": "98",
    "0#柴油": "0",
}

# 「数据完整」判定只看这几个——98# 并非所有省份都供应（部分偏远省份页面无此价），
# 不能算进 complete，否则覆盖率会被 98# 缺失拖垮。
REQUIRED_FUELS: tuple[str, ...] = ("92", "95", "0")

REQUEST_TIMEOUT = 20.0
REQUEST_RETRIES = 3
