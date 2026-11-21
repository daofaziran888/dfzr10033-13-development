"""多维立方的区域事实明细。

这里放的是下钻取数的原始事实粒度：国家 / 地区 / 城市，以及 revenue、orders
两个度量。下钻聚合只在“当前账号可见的事实行”上做，不能先按全量事实聚合再
在结果层裁剪，否则父级小计会把受限子地区的金额带出来。
"""
from __future__ import annotations

from typing import Any

# 层级顺序固定，下钻路径只能沿这个顺序向下展开。
LEVEL_ORDER: tuple[str, ...] = ("country", "region", "city")

# 全部地区及其城市。父子关系同时用于计算“可见子节点 / 全部子节点”。
REGION_CITIES: dict[str, list[str]] = {
    "东区": ["上海", "杭州", "南京"],
    "西区": ["成都", "重庆", "西安"],
    "南区": ["广州", "深圳"],
    "北区": ["北京", "天津"],
}

COUNTRY_NAME = "全国"

# 立方 2“区域销售立方”的事实明细。金额和订单数为演示用的确定值，
# 便于核对“东区全量 + 西区仅成都”这一账号的可见结果。
FACT_ROWS: list[dict[str, Any]] = [
    {"id": 1, "cube_id": 2, "country": COUNTRY_NAME, "region": "东区", "city": "上海", "revenue": 1200, "orders": 30},
    {"id": 2, "cube_id": 2, "country": COUNTRY_NAME, "region": "东区", "city": "杭州", "revenue": 800, "orders": 20},
    {"id": 3, "cube_id": 2, "country": COUNTRY_NAME, "region": "东区", "city": "南京", "revenue": 500, "orders": 10},
    {"id": 4, "cube_id": 2, "country": COUNTRY_NAME, "region": "西区", "city": "成都", "revenue": 900, "orders": 25},
    {"id": 5, "cube_id": 2, "country": COUNTRY_NAME, "region": "西区", "city": "重庆", "revenue": 700, "orders": 18},
    {"id": 6, "cube_id": 2, "country": COUNTRY_NAME, "region": "西区", "city": "西安", "revenue": 600, "orders": 15},
    {"id": 7, "cube_id": 2, "country": COUNTRY_NAME, "region": "南区", "city": "广州", "revenue": 1100, "orders": 28},
    {"id": 8, "cube_id": 2, "country": COUNTRY_NAME, "region": "南区", "city": "深圳", "revenue": 1000, "orders": 26},
    {"id": 9, "cube_id": 2, "country": COUNTRY_NAME, "region": "北区", "city": "北京", "revenue": 1300, "orders": 32},
    {"id": 10, "cube_id": 2, "country": COUNTRY_NAME, "region": "北区", "city": "天津", "revenue": 400, "orders": 9},
]

DEFAULT_MEASURES: tuple[str, ...] = ("revenue", "orders")


def facts_for_cube(cube_id: int) -> list[dict[str, Any]]:
    """返回指定立方的事实明细；没有登记事实的立方返回空列表。"""
    return [dict(row) for row in FACT_ROWS if int(row.get("cube_id", 0)) == int(cube_id)]


def all_cities(region: str) -> list[str]:
    return list(REGION_CITIES.get(region, []))


def infer_level(value: str) -> str:
    """根据节点名称推断所在层级，用于“继续展开已有路径”时定位父节点。"""
    if value == COUNTRY_NAME:
        return "country"
    if value in REGION_CITIES:
        return "region"
    return "city"


def level_index(level: str) -> int:
    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return -1


def next_level(level: str) -> str | None:
    index = level_index(level)
    if index < 0 or index >= len(LEVEL_ORDER) - 1:
        return None
    return LEVEL_ORDER[index + 1]
