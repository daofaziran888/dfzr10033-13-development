"""授权范围内的下钻取数：按立方事实明细重新聚合可见层级数据。

三条读取路径共用同一套授权与聚合逻辑，保证口径一致：
- `drill()`：下钻明细（打开 / 继续展开已有路径）
- `direct_query()`：直接查询
- `export_csv()`：下载

关键约束：
1. 先按授权范围过滤事实行，再聚合；绝不先聚合全量再裁剪结果。
2. 父节点下只要存在受限子节点，就不输出全量小计（`subtotal=None`）。
3. 若该账号此前看到过这个父节点的完整小计，随后授权被临时收回，
   则连“部分小计”也一并不输出，避免用新旧总量相减反推出受限金额。
4. 每个组结果都带 `scope_version` 范围标识；页面与下载必须使用同一版本。
"""
from __future__ import annotations

import csv
import io
import time
from typing import Any

from app.domain.cube_facts import (
    COUNTRY_NAME,
    LEVEL_ORDER,
    REGION_CITIES,
    all_cities,
    facts_for_cube,
    infer_level,
    next_level,
)
from app.services import access_scope
from app.services.access_scope import ScopeError
from app.services.drill_config import DrillConfigService
from app.store import store

MANIFEST_MODULE = "drill_manifest"
METRICS: tuple[str, ...] = ("revenue", "orders")

_drill_config = DrillConfigService()

# 任何一次读取（包括只读共享页）只要返回过完整小计，就记住该节点。
# 这样后续临时收权时，即使清单尚未落盘，也能识别“新旧总量相减”的风险。
_SEEN_COMPLETE: dict[tuple[str, int], set[tuple[str, ...]]] = {}


class DrillError(Exception):
    """取数失败：带 HTTP 状态码，便于路由层直接映射。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------- 路径与授权 ----------

def parse_path(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        parts: list[str] = []
    elif isinstance(raw, list):
        parts = [str(part).strip() for part in raw]
    else:
        text = str(raw).strip()
        for sep in ("|", "/", ">"):
            text = text.replace(sep, ">")
        parts = [part.strip() for part in text.split(">")]
    parts = [part for part in parts if part]
    if not parts:
        return [COUNTRY_NAME]
    if parts[0] != COUNTRY_NAME:
        parts.insert(0, COUNTRY_NAME)
    return parts


def _require_scope(account: str | None) -> dict[str, Any]:
    if not account:
        raise DrillError("missing_account", "缺少取数账号，无法确定授权范围", 400)
    try:
        return access_scope.build_scope(account)
    except ScopeError as exc:
        raise DrillError(exc.code, exc.message, 400) from exc


def _authorize_path(scope: dict[str, Any], path: list[str]) -> str:
    allowed, level, reason = access_scope.authorize_path(scope, path)
    if not allowed:
        raise DrillError("path_forbidden", reason or "下钻路径超出授权范围", 403)
    return level or "country"


def _check_version(scope: dict[str, Any], requested: str | None) -> None:
    problem = access_scope.check_permission_version(scope, requested)
    if problem is not None:
        raise DrillError(problem.code, problem.message, 409)


# ---------- 事实过滤与聚合 ----------

def _cube_facts(cube_id: int) -> list[dict[str, Any]]:
    if store.find("olap_cube", cube_id) is None:
        raise DrillError("cube_not_found", f"立方 {cube_id} 不存在", 404)
    return facts_for_cube(cube_id)


def _scope_facts(scope: dict[str, Any], cube_id: int, path: list[str]) -> list[dict[str, Any]]:
    facts = access_scope.filter_facts(scope, _cube_facts(cube_id))
    if len(path) >= 2:
        facts = [row for row in facts if row.get("region") == path[1]]
    if len(path) >= 3:
        facts = [row for row in facts if row.get("city") == path[2]]
    return facts


def _sum_metrics(facts: list[dict[str, Any]]) -> dict[str, int]:
    return {metric: sum(int(row.get(metric, 0)) for row in facts) for metric in METRICS}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _all_children(child_level: str, path: list[str]) -> list[str]:
    """当前节点在 `child_level` 上的全部子节点（不受授权裁剪）。"""
    if child_level == "region":
        return list(REGION_CITIES)
    if child_level == "city" and len(path) >= 2:
        return all_cities(path[1])
    return []


def _node_complete(scope: dict[str, Any], level: str, path: list[str]) -> bool:
    """节点覆盖的全部事实是否都在授权范围内。"""
    if level == "city":
        return True
    if level == "region":
        if len(path) < 2:
            return False
        region = path[1]
        return set(all_cities(region)) <= set(scope["visible_cities"].get(region, []))
    if level == "country":
        return all(
            set(all_cities(region)) <= set(scope["visible_cities"].get(region, []))
            for region in REGION_CITIES
        )
    return False


def _prior_complete_paths(account: str, cube_id: int) -> set[tuple[str, ...]]:
    """该账号曾经看到过完整小计的节点路径（清单 + 本次进程内读取记录）。"""
    complete: set[tuple[str, ...]] = set(_SEEN_COMPLETE.get((account, int(cube_id)), set()))
    for row in store.rows(MANIFEST_MODULE):
        if row.get("账号") != account or _as_int(row.get("立方")) != int(cube_id):
            continue
        for node in row.get("完整节点", []) or []:
            complete.add(tuple(node))
    return complete


def _record_complete(account: str, cube_id: int, node_paths: list[list[str]]) -> None:
    bucket = _SEEN_COMPLETE.setdefault((account, int(cube_id)), set())
    for node in node_paths:
        bucket.add(tuple(node))


def reset_seen_complete() -> None:
    """测试与演示用：清空进程内的完整小计读取记录。"""
    _SEEN_COMPLETE.clear()


def _scope_tag(scope: dict[str, Any]) -> str:
    return f"scope:{scope['scope_version']}"


def _summary(
    scope: dict[str, Any],
    path: list[str],
    level: str,
    facts_all: list[dict[str, Any]],
    prior_complete: set[tuple[str, ...]],
) -> dict[str, Any]:
    """父节点摘要：受限时不给全量小计，必要时连部分小计也一并压下。"""
    complete = _node_complete(scope, level, path)
    metrics = _sum_metrics(facts_all)
    summary: dict[str, Any] = {
        "path": list(path),
        "name": path[-1],
        "level": level,
        "complete": complete,
        "subtotal_allowed": complete,
        "subtotal": metrics if complete else None,
        "partial_metrics": None,
        "restricted_children": not complete,
        "withheld": False,
        "scope_version": scope["scope_version"],
        "scope_tag": _scope_tag(scope),
        "scope_label": scope["scope_label"],
    }
    if not complete:
        previously_complete = tuple(path) in prior_complete
        if previously_complete:
            # 先看到过完整小计、后被收回：部分小计也不能给，否则可相减反推。
            summary["withheld"] = True
            summary["reason"] = "授权范围发生变化，父级小计不可展示，避免反推受限明细"
        else:
            summary["partial_metrics"] = metrics
            summary["reason"] = "存在受限子节点，不展示全量小计"
    return summary


def _group_rows(
    scope: dict[str, Any],
    path: list[str],
    child_level: str,
    facts: list[dict[str, Any]],
    prior_complete: set[tuple[str, ...]],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in facts:
        value = str(row.get(child_level, ""))
        if not value:
            continue
        buckets.setdefault(value, []).append(row)

    ordered = [value for value in _all_children(child_level, path) if value in buckets]
    # 事实里出现、但登记表未列出的值也一并保留，避免静默丢数。
    ordered.extend(sorted(value for value in buckets if value not in ordered))

    groups: list[dict[str, Any]] = []
    for value in ordered:
        child_path = [*path, value]
        child_complete = _node_complete(scope, child_level, child_path)
        withheld = _withhold_group(child_path, prior_complete, child_complete)
        metrics = _sum_metrics(buckets[value])
        group: dict[str, Any] = {
            "name": value,
            "level": child_level,
            "path": child_path,
            "parent_path": list(path),
            "metrics": None if withheld else metrics,
            "complete": child_complete,
            "subtotal_allowed": child_complete,
            "partial": not child_complete,
            "restricted_children": not child_complete,
            "withheld": withheld,
            "row_count": len(buckets[value]),
            "scope_version": scope["scope_version"],
            "scope_tag": _scope_tag(scope),
            "scope_label": scope["scope_label"],
        }
        if withheld:
            group["reason"] = "授权范围发生变化，该组数值不可展示，避免反推受限明细"
        elif not child_complete:
            group["reason"] = "按授权范围聚合，仅含可见子节点"
        groups.append(group)
    return groups


def _withhold_group(
    child_path: list[str],
    prior_complete: set[tuple[str, ...]],
    complete: bool,
) -> bool:
    return not complete and tuple(child_path) in prior_complete


def _remember_complete(
    scope: dict[str, Any],
    cube_id: int,
    path: list[str],
    parent: dict[str, Any],
    groups: list[dict[str, Any]],
) -> None:
    """只读共享页也会命中：任何一次能看到完整小计的读取都要留痕。"""
    nodes = [group["path"] for group in groups if group.get("complete")]
    if parent.get("complete"):
        nodes.append(list(path))
    if nodes:
        _record_complete(scope["account"], cube_id, nodes)


# ---------- 下钻明细 ----------

def drill(
    account: str | None,
    cube_id: int,
    path: str | list[str] | None = None,
    scope_version: str | None = None,
    *,
    write_manifest: bool = True,
) -> dict[str, Any]:
    scope = _require_scope(account)
    _check_version(scope, scope_version)
    normalized = parse_path(path)
    current_level = _authorize_path(scope, normalized)

    hierarchy = _drill_config.resolve_hierarchy(cube_id)
    child_level = next_level(current_level)
    if child_level is not None:
        allowed, reason = _drill_config.validate_transition(current_level, child_level)
        if not allowed:
            raise DrillError("transition_not_allowed", reason or "下钻配置不允许该步骤", 409)

    facts_all = _scope_facts(scope, cube_id, normalized)
    prior_complete = _prior_complete_paths(scope["account"], cube_id)
    parent = _summary(scope, normalized, current_level, facts_all, prior_complete)

    groups: list[dict[str, Any]] = []
    if child_level:
        groups = _group_rows(scope, normalized, child_level, facts_all, prior_complete)

    result = {
        "ok": True,
        "cube_id": cube_id,
        "account": scope["account"],
        "readonly": scope["readonly"],
        "scope": access_scope.visible_scope_view(scope),
        "path": normalized,
        "level": current_level,
        "child_level": child_level,
        "hierarchy": hierarchy,
        "parent": parent,
        "groups": groups,
        "written_to_manifest": False,
    }
    _remember_complete(scope, cube_id, normalized, parent, groups)
    if write_manifest:
        result["manifest"] = _write_manifest(scope, cube_id, normalized, current_level, groups, parent)
        result["written_to_manifest"] = True
        _touch_drill_config(current_level, child_level)
    return result


# ---------- 直接查询 ----------

def direct_query(
    account: str | None,
    cube_id: int,
    group_by: str,
    region: str | None = None,
    city: str | None = None,
    scope_version: str | None = None,
) -> dict[str, Any]:
    scope = _require_scope(account)
    _check_version(scope, scope_version)
    if group_by not in ("region", "city"):
        raise DrillError("bad_group_by", "直接查询只支持按 region 或 city 分组", 400)

    path: list[str] = [COUNTRY_NAME]
    if region:
        if not access_scope.region_visible(scope, region):
            raise DrillError("region_forbidden", f"当前账号没有地区 {region} 的访问权", 403)
        path.append(region)
    if city:
        if not region:
            raise DrillError("missing_region", "按城市直查时必须同时指定地区", 400)
        if not access_scope.city_visible(scope, region, city):
            raise DrillError("city_forbidden", f"当前账号没有城市 {region}/{city} 的访问权", 403)
        path.append(city)

    facts = _scope_facts(scope, cube_id, path)
    prior_complete = _prior_complete_paths(scope["account"], cube_id)
    rows = _group_rows(scope, path, group_by, facts, prior_complete)
    parent = _summary(scope, path, infer_level(path[-1]), facts, prior_complete)

    _remember_complete(scope, cube_id, path, parent, rows)

    # 直查结果同样回写下钻清单，保证“曾经完整看过某节点”这一事实
    # 在后续临时收回时仍可用于判定是否需要压下部分小计。
    manifest = _write_manifest(scope, cube_id, path, infer_level(path[-1]), rows, parent)

    return {
        "ok": True,
        "cube_id": cube_id,
        "account": scope["account"],
        "readonly": scope["readonly"],
        "mode": "direct_query",
        "scope": access_scope.visible_scope_view(scope),
        "group_by": group_by,
        "path": path,
        "rows": rows,
        "parent": parent,
        "hierarchy": _drill_config.resolve_hierarchy(cube_id),
        "manifest": manifest,
    }


# ---------- 下载 ----------

def export_csv(
    account: str | None,
    cube_id: int,
    path: str | list[str] | None = None,
    scope_version: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """使用与页面完全相同的授权版本和聚合结果生成 CSV。

    返回 (csv 文本, 元信息)。受权限版本变化影响时抛 409，不生成旧版本文件。
    """
    result = drill(
        account,
        cube_id,
        path,
        scope_version,
        write_manifest=False,
    )
    scope = result["scope"]
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "账号", "权限版本", "范围标识", "下钻路径", "层级", "父节点",
        "节点", "revenue", "orders", "分组范围标识", "是否完整小计",
    ])
    for group in result["groups"]:
        metrics = group.get("metrics") or {}
        writer.writerow([
            result["account"], scope["scope_version"], scope["scope_label"],
            ">".join(result["path"]), group["level"], ">".join(group["parent_path"]),
            group["name"],
            "" if group.get("withheld") else metrics.get("revenue", ""),
            "" if group.get("withheld") else metrics.get("orders", ""),
            group["scope_tag"],
            "是" if group["subtotal_allowed"] else "否",
        ])
    parent = result["parent"]
    if parent.get("subtotal") is not None:
        writer.writerow([
            result["account"], scope["scope_version"], scope["scope_label"],
            ">".join(result["path"]), parent["level"], "",
            f"{parent['name']}(小计)", parent["subtotal"].get("revenue", ""),
            parent["subtotal"].get("orders", ""), parent["scope_tag"], "是",
        ])
    else:
        # 受限父级不写金额列，只留一条明确标记，避免空列被当成 0。
        writer.writerow([
            result["account"], scope["scope_version"], scope["scope_label"],
            ">".join(result["path"]), parent["level"], "",
            f"{parent['name']}(小计不可展示)", "", "", parent["scope_tag"], "否",
        ])
    meta = {
        "filename": f"drill_cube{cube_id}_{scope['scope_version']}.csv",
        "scope_version": scope["scope_version"],
        "scope_label": scope["scope_label"],
        "row_count": len(result["groups"]),
        "readonly": result["readonly"],
    }
    return output.getvalue(), meta


# ---------- 下钻清单回写 ----------

def _write_manifest(
    scope: dict[str, Any],
    cube_id: int,
    path: list[str],
    level: str,
    groups: list[dict[str, Any]],
    parent: dict[str, Any],
) -> dict[str, Any]:
    rows = store.rows(MANIFEST_MODULE)
    for row in rows:
        if (
            row.get("账号") == scope["account"]
            and _as_int(row.get("立方")) == int(cube_id)
            and ">".join(row.get("下钻路径", [])) == ">".join(path)
            and row.get("status") == "当前"
        ):
            row["status"] = "已过期"
            row["abnormal"] = True

    entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    complete_nodes = [
        group["path"] for group in groups if group.get("complete")
    ]
    if parent.get("complete"):
        complete_nodes.append(path)
    entry = {
        "id": entry_id,
        "清单编号": f"DRIL-M{entry_id:04d}",
        "账号": scope["account"],
        "立方": cube_id,
        "下钻路径": path,
        "层级": level,
        "权限版本": scope["scope_version"],
        "范围标识": scope["scope_label"],
        "组结果": groups,
        "父级": parent,
        "完整节点": complete_nodes,
        "写入时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "当前",
        "pending": False,
        "abnormal": not parent.get("complete", False),
    }
    rows.append(entry)
    return {
        "清单编号": entry["清单编号"],
        "id": entry_id,
        "权限版本": entry["权限版本"],
        "组数": len(groups),
        "父级完整": parent.get("complete"),
    }


def _touch_drill_config(current_level: str, child_level: str | None) -> None:
    if child_level is None:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for row in store.rows("drill_config"):
        if row.get("status") != "已生效":
            continue
        if row.get("起始维度") == current_level and row.get("目标维度") == child_level:
            row["最后使用"] = now


# ---------- 清单查询 ----------

def list_manifest(account: str | None = None, cube_id: int | None = None) -> list[dict[str, Any]]:
    """列出下钻清单。

    - 指定账号时，只返回该账号在当前权限版本下写回的组结果；旧版本的
      金额一律剥离并标记过期，避免继续返回无法从当前授权证明的数据。
    - 不指定账号时只返回元信息，绝不带出任何金额。
    """
    current_version = access_scope.build_scope(account)["scope_version"] if account else None
    rows = store.rows(MANIFEST_MODULE)
    result = []
    for row in reversed(rows):
        if account and row.get("账号") != account:
            continue
        if cube_id is not None and _as_int(row.get("立方")) != int(cube_id):
            continue
        item = dict(row)
        stale = current_version is not None and item.get("权限版本") != current_version
        if current_version is None or stale:
            item["组结果"] = []
            item["父级"] = None
            if stale:
                item["status"] = "已过期"
                item["version_stale"] = True
        result.append(item)
    return result


def list_shared(reader_account: str | None) -> dict[str, Any]:
    """共享页清单：只给出路径元信息，金额一律按当前读者的范围另行聚合。"""
    reader = _require_scope(reader_account)
    items = []
    for row in list_manifest():
        if row.get("status") != "当前":
            continue
        path = list(row.get("下钻路径", []))
        allowed, level, _reason = access_scope.authorize_path(reader, path)
        items.append({
            "id": row.get("id"),
            "清单编号": row.get("清单编号"),
            "立方": row.get("立方"),
            "下钻路径": path,
            "层级": row.get("层级"),
            "作者范围": row.get("范围标识"),
            "作者权限版本": row.get("权限版本"),
            "读者可访问": allowed,
            "读者权限版本": reader["scope_version"],
            "读者范围": reader["scope_label"],
            "status": row.get("status"),
            "abnormal": row.get("abnormal"),
        })
    return {
        "ok": True,
        "reader": access_scope.visible_scope_view(reader),
        "items": items,
    }


def shared_view(
    manifest_id: int,
    reader_account: str | None,
    scope_version: str | None = None,
) -> dict[str, Any]:
    """在共享页面打开一条下钻路径：按当前读者的地区范围重新取数。

    共享页面是只读的，作者写入清单时的组结果只用于定位路径，
    绝不直接返回；金额必须由读者自己的授权范围重新聚合得到。
    """
    reader = _require_scope(reader_account)
    _check_version(reader, scope_version)
    manifest = store.find(MANIFEST_MODULE, manifest_id)
    if manifest is None:
        raise DrillError("manifest_not_found", f"下钻清单 {manifest_id} 不存在", 404)
    path = list(manifest.get("下钻路径", [COUNTRY_NAME]))
    result = drill(
        reader["account"],
        _as_int(manifest.get("立方")),
        path,
        reader["scope_version"],
        write_manifest=False,
    )
    result["mode"] = "shared_readonly"
    result["shared_from"] = {
        "manifest_id": manifest_id,
        "清单编号": manifest.get("清单编号"),
        "作者范围": manifest.get("范围标识"),
        "作者权限版本": manifest.get("权限版本"),
    }
    return result


def scope_snapshot(account: str | None) -> dict[str, Any]:
    scope = _require_scope(account)
    return {
        "ok": True,
        "scope": access_scope.visible_scope_view(scope),
        "accounts": access_scope.account_names(),
        "hierarchy": _drill_config.resolve_hierarchy(None),
        "level_order": list(LEVEL_ORDER),
    }
