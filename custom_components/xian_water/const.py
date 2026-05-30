"""Constants for the 西安水务 integration."""

DOMAIN = "xian_water"
NAME = "西安水费"

CONF_CLIENT_CODE = "client_code"
CONF_CLIENT_TYPE = "client_type"
CONF_CID = "cid"

DEFAULT_CLIENT_TYPE = "IC"

API_ENDPOINT = "http://dzfp.xazls.com:54432/invoice/ew/queryPayRecords"

# 水费年阶梯定价
TIER_LEVEL_1 = 144   # m³ - 第2档起始用水量
TIER_LEVEL_2 = 207   # m³ - 第3档起始用水量
TIER_PRICE_1 = 3.8   # 元/m³ - 第1档水价
TIER_PRICE_2 = 6.09  # 元/m³ - 第2档水价
TIER_PRICE_3 = 8.38  # 元/m³ - 第3档水价

# 充值明细
RECHARGE_RECORDS = [
    {"date": "2024-12-23", "cost": 3.8},
    {"date": "2024-12-23", "cost": 114},
    {"date": "2024-12-23", "cost": 38},
    {"date": "2025-04-23", "cost": 380},
    {"date": "2026-02-13", "cost": 380},
]
