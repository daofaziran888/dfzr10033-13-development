"""下钻配置领域服务：层级归属解析、下钻路径校验，供实际取数使用。

原先这里只有字段和状态流转，层级配置并没有作用到取数。现在把
“起始维度 / 目标维度”解析成可执行的分层路径，下钻明细、直查和下载
都先经过 `resolve_hierarchy` / `validate_transition`，层级归属不再只是配置展示。
"""
from __future__ import annotations

from typing import Any

from app.domain.cube_facts import LEVEL_ORDER
from app.services.base import BaseModuleService
from app.store import store


class DrillConfigService(BaseModuleService):
    MODULE = "drill_config"
    ENTITY = "下钻路径"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["路径编号", "起始维度", "目标维度", "下钻类型", "关联键", "跳转规则", "展示模板", "最后使用"]
    REQUIRED_FIELDS = ["路径编号", "起始维度", "目标维度"]
    STATUS_ORDER = ["草稿", "已生效", "已禁用", "已废弃"]
    ACTION_RULES = {"启效路径": "已生效", "禁用路径": "已禁用", "废弃路径": "已废弃"}
    NEGATIVE_ACTIONS = []

    def effective_transitions(self) -> list[dict[str, Any]]:
        """当前已生效的下钻步骤（start -> target）。"""
        result = []
        for row in store.rows(self.MODULE):
            if row.get("status") != "已生效":
                continue
            start = str(row.get("起始维度", "")).strip()
            target = str(row.get("目标维度", "")).strip()
            try:
                start_idx = LEVEL_ORDER.index(start)
                target_idx = LEVEL_ORDER.index(target)
            except ValueError:
                continue
            if target_idx != start_idx + 1:
                continue
            result.append({
                "path_id": row.get("id"),
                "路径编号": row.get("路径编号"),
                "start": start,
                "target": target,
                "关联键": row.get("关联键"),
                "跳转规则": row.get("跳转规则"),
            })
        return result

    def resolve_hierarchy(self, cube_id: int | None = None) -> dict[str, Any]:
        """解析立方可用的层级顺序，并把已生效下钻配置转成可执行的步骤集合。"""
        levels = list(LEVEL_ORDER)
        hierarchy_source = "default"
        hierarchy_ref: int | None = None

        # 维度树中登记了完整层级路径时，以它为准。
        for row in store.rows("dimension_tree"):
            if row.get("status") not in ("可用", "构建中"):
                continue
            path = [part.strip() for part in str(row.get("层级路径", "")).split(">") if part.strip()]
            if path == levels:
                hierarchy_source = f"dimension_tree:{row.get('id')}"
                hierarchy_ref = int(row.get("id", 0))
                break

        transitions = self.effective_transitions()
        allowed = {(item["start"], item["target"]) for item in transitions}
        return {
            "cube_id": cube_id,
            "levels": levels,
            "hierarchy_source": hierarchy_source,
            "hierarchy_ref": hierarchy_ref,
            "transitions": transitions,
            "enforced_transitions": bool(allowed),
            "allowed_transitions": sorted(allowed),
        }

    def validate_transition(self, start_level: str, target_level: str) -> tuple[bool, str | None]:
        """按已生效配置校验一步下钻；没有生效配置时按默认层级放行。"""
        transitions = self.effective_transitions()
        if not transitions:
            return True, None
        allowed = {(item["start"], item["target"]) for item in transitions}
        if (start_level, target_level) in allowed:
            return True, None
        return (
            False,
            f"下钻配置未把 {start_level} -> {target_level} 登记为已生效路径",
        )

    def drill_transitions_for(self, cube_id: int) -> list[dict[str, Any]]:
        return self.resolve_hierarchy(cube_id)["transitions"]
