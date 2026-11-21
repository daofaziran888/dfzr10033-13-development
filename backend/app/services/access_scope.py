"""授权范围：把账号的“地区 / 城市”访问权收敛成可复用的取数范围。

下钻明细、直接查询、结果下载都必须先经过这里得到同一个 `scope`，
再在事实明细上过滤后聚合。不能先聚合全量再裁剪结果，否则父级小计会
把受限子地区的金额带出来。

`scope_version` 由当前有效授权派生：授权被临时收回 / 恢复后版本随之变化，
页面与下载必须持有同一版本，旧版本结果不会被继续返回。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from app.domain.cube_facts import COUNTRY_NAME, REGION_CITIES, all_cities, infer_level

# 东区全部可查、西区只有成都可查的核对账号。
ACCOUNT_EAST_ALL_WEST_CHENGDU = "analyst_east_west"

# 账号 = 权限模式 + 授权项。cities 为空表示整个地区全部可查。
_BASE_ACCOUNTS: dict[str, dict[str, Any]] = {
    ACCOUNT_EAST_ALL_WEST_CHENGDU: {
        "name": "东区全量 / 西区成都分析员",
        "mode": "interactive",
        "grants": [
            {"region": "东区"},
            {"region": "西区", "cities": ["成都"]},
        ],
    },
    "analyst_national": {
        "name": "全国分析员",
        "mode": "interactive",
        "grants": [{"region": region} for region in REGION_CITIES],
    },
    "reader_south": {
        "name": "南区共享页只读读者",
        "mode": "readonly",
        "grants": [{"region": "南区"}],
    },
    "reader_east_west": {
        "name": "东区 / 西区成都只读读者",
        "mode": "readonly",
        "grants": [
            {"region": "东区"},
            {"region": "西区", "cities": ["成都"]},
        ],
    },
}

# 临时收回的授权：(account, region, city|None)，city 为 None 表示收回整个地区。
_REVOKED: set[tuple[str, str, str | None]] = set()


class ScopeError(Exception):
    """授权范围不可用：未知账号、路径越权或权限版本过期。"""

    def __init__(self, code: str, message: str, scope: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.scope = scope


def account_names() -> list[str]:
    return sorted(_BASE_ACCOUNTS)


def effective_grants(account: str) -> list[dict[str, Any]]:
    """展开临时收回后，账号仍然有效的授权项。"""
    base = _BASE_ACCOUNTS.get(account)
    if base is None:
        raise ScopeError("unknown_account", f"未知账号：{account}")

    result: list[dict[str, Any]] = []
    for grant in base["grants"]:
        region = grant["region"]
        if (account, region, None) in _REVOKED:
            continue
        granted_cities = grant.get("cities")
        allowed = all_cities(region) if granted_cities is None else list(granted_cities)
        allowed = [city for city in allowed if (account, region, city) not in _REVOKED]
        if not allowed:
            continue
        # 全部城市仍然可见时保持“整地区授权”的语义。
        if set(allowed) == set(all_cities(region)):
            result.append({"region": region, "cities": None})
        else:
            result.append({"region": region, "cities": sorted(allowed)})
    return result


def build_scope(account: str) -> dict[str, Any]:
    """生成取数范围，含可见城市、完整性和稳定的权限版本。"""
    base = _BASE_ACCOUNTS.get(account)
    if base is None:
        raise ScopeError("unknown_account", f"未知账号：{account}")

    grants = effective_grants(account)
    visible: dict[str, list[str]] = {}
    for grant in grants:
        region = grant["region"]
        if grant.get("cities") is None:
            visible[region] = all_cities(region)
        else:
            merged = set(visible.get(region, [])) | set(grant["cities"])
            visible[region] = [city for city in all_cities(region) if city in merged]

    visible = {region: cities for region, cities in visible.items() if cities}
    hidden_cities: dict[str, list[str]] = {}
    for region in REGION_CITIES:
        missing = [city for city in all_cities(region) if city not in visible.get(region, [])]
        if missing:
            hidden_cities[region] = missing

    complete = not hidden_cities
    # 权限版本只由“授权后真正可见的输入”决定，保证页面 / 下载拿到的是同一版。
    canonical = {
        "account": account,
        "visible": {region: visible[region] for region in REGION_CITIES if region in visible},
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    parts = []
    for region in REGION_CITIES:
        cities = visible.get(region)
        if not cities:
            continue
        if set(cities) == set(all_cities(region)):
            parts.append(f"{region}(全部)")
        else:
            parts.append(f"{region}({'、'.join(cities)})")
    label = " + ".join(parts) if parts else "无可见地区"

    return {
        "account": account,
        "account_name": base["name"],
        "mode": base["mode"],
        "readonly": base["mode"] == "readonly",
        "grants": grants,
        "granted_regions": [region for region in REGION_CITIES if region in visible],
        "visible_cities": visible,
        "hidden_cities": hidden_cities,
        "complete": complete,
        "scope_version": digest,
        "scope_label": label,
        "country": COUNTRY_NAME,
    }


def scope_or_error(account: str | None) -> tuple[dict[str, Any] | None, ScopeError | None]:
    if not account:
        return None, ScopeError("missing_account", "缺少取数账号，无法确定授权范围")
    try:
        return build_scope(account), None
    except ScopeError as exc:
        return None, exc


def region_visible(scope: dict[str, Any], region: str) -> bool:
    return bool(scope["visible_cities"].get(region))


def city_visible(scope: dict[str, Any], region: str, city: str) -> bool:
    return city in scope["visible_cities"].get(region, [])


def filter_facts(
    scope: dict[str, Any], facts: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """只保留授权范围内的事实行；不接受“先全量聚合再裁剪”的用法。"""
    result = []
    for row in facts:
        region = str(row.get("region", ""))
        city = str(row.get("city", ""))
        if city_visible(scope, region, city):
            result.append(dict(row))
    return result


def authorize_path(
    scope: dict[str, Any], path: list[str]
) -> tuple[bool, str | None, str | None]:
    """校验下钻路径的层级归属与授权范围。

    返回 (是否允许, 当前层级, 拒绝原因)。路径中的每个节点都必须能从
    仍然有效的授权推导出来，否则整个请求不返回任何金额。
    """
    if not path:
        path = [COUNTRY_NAME]
    if path[0] != COUNTRY_NAME:
        return False, None, "下钻路径必须从国家根节点开始"
    if len(path) > 3:
        return False, None, "下钻路径超过维度层级深度"

    level = infer_level(path[0])
    if level != "country":
        return False, None, "根节点不是国家层级"

    if len(path) >= 2:
        region = path[1]
        if region not in REGION_CITIES:
            return False, None, f"未知地区：{region}"
        if not region_visible(scope, region):
            return False, None, f"当前账号没有地区 {region} 的访问权"
        level = "region"

    if len(path) >= 3:
        city = path[2]
        parent = path[1]
        if city not in all_cities(parent):
            return False, None, f"城市 {city} 不属于地区 {parent}"
        if not city_visible(scope, parent, city):
            return False, None, f"当前账号没有城市 {parent}/{city} 的访问权"
        level = "city"

    return True, level, None


def visible_scope_view(scope: dict[str, Any]) -> dict[str, Any]:
    """返回给前端的范围信息（不含具体金额）。"""
    return {
        "account": scope["account"],
        "account_name": scope["account_name"],
        "mode": scope["mode"],
        "readonly": scope["readonly"],
        "scope_version": scope["scope_version"],
        "scope_label": scope["scope_label"],
        "visible_cities": {
            region: list(cities) for region, cities in scope["visible_cities"].items()
        },
        "hidden_city_count": sum(len(cities) for cities in scope["hidden_cities"].values()),
        "complete": scope["complete"],
    }


def check_permission_version(
    scope: dict[str, Any], requested: str | None
) -> ScopeError | None:
    if requested and requested != scope["scope_version"]:
        return ScopeError(
            "scope_version_mismatch",
            "权限版本已变化，请刷新页面后重新下钻或下载",
            scope,
        )
    return None


# ---------- 临时收回 / 恢复 ----------

def temporarily_revoke(
    account: str, region: str, city: str | None = None
) -> dict[str, Any]:
    _require_known(account, region, city)
    _REVOKED.add((account, region, city))
    return build_scope(account)


def restore_access(account: str, region: str | None = None, city: str | None = None) -> dict[str, Any]:
    if account not in _BASE_ACCOUNTS:
        raise ScopeError("unknown_account", f"未知账号：{account}")
    if region is None:
        _REVOKED.difference_update({item for item in _REVOKED if item[0] == account})
    else:
        for item in list(_REVOKED):
            if item[0] != account or item[1] != region:
                continue
            if city is None or item[2] is None or item[2] == city:
                _REVOKED.discard(item)
    return build_scope(account)


def reset_overrides() -> None:
    """测试与演示用：清空所有临时收回。"""
    _REVOKED.clear()


def _require_known(account: str, region: str, city: str | None) -> None:
    if account not in _BASE_ACCOUNTS:
        raise ScopeError("unknown_account", f"未知账号：{account}")
    if region not in REGION_CITIES:
        raise ScopeError("unknown_region", f"未知地区：{region}")
    if city is not None and city not in all_cities(region):
        raise ScopeError("unknown_city", f"城市 {city} 不属于地区 {region}")
