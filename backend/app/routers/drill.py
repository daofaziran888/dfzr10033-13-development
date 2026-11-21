"""下钻取数接口：授权范围、下钻明细、直查、下载、共享页与临时收权。

所有读取都以 `account` 作为取数身份，处理顺序固定为
「解析账号 -> 校验权限版本 -> 校验路径归属 -> 过滤事实 -> 重新聚合」。
金额不来自任何全量缓存，避免受限子地区明细被带出。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.services import access_scope, drill_analysis

router = APIRouter(prefix="/api/drill", tags=["下钻取数"])


def _handle(callable_fn, *args, **kwargs):
    try:
        return callable_fn(*args, **kwargs)
    except drill_analysis.DrillError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except access_scope.ScopeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.get("/scope")
def get_scope(account: str = Query(..., description="取数账号")) -> dict[str, Any]:
    """返回账号的可见地区 / 城市、范围标识与权限版本。"""
    return _handle(drill_analysis.scope_snapshot, account)


@router.get("/accounts")
def list_accounts() -> dict[str, Any]:
    """可切换的演示账号清单（不含金额）。"""
    items = []
    for name in access_scope.account_names():
        scope = access_scope.build_scope(name)
        items.append(access_scope.visible_scope_view(scope))
    return {"ok": True, "items": items}


@router.get("/detail")
def drill_detail(
    account: str = Query(...),
    cube_id: int = Query(default=2),
    path: str = Query(default="全国", description="用 > 分隔的层级路径，如 全国>西区>成都"),
    scope_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """打开或继续展开一条既有下钻路径，只返回授权范围内可证明的数据。"""
    return _handle(drill_analysis.drill, account, cube_id, path, scope_version)


@router.get("/direct")
def direct_query(
    account: str = Query(...),
    cube_id: int = Query(default=2),
    group_by: str = Query(default="city"),
    region: str | None = Query(default=None),
    city: str | None = Query(default=None),
    scope_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """直接查询（不做逐层下钻），与下钻共用同一套范围过滤与聚合。"""
    return _handle(
        drill_analysis.direct_query,
        account,
        cube_id,
        group_by,
        region,
        city,
        scope_version,
    )


@router.get("/export")
def export_detail(
    account: str = Query(...),
    cube_id: int = Query(default=2),
    path: str = Query(default="全国"),
    scope_version: str | None = Query(default=None),
) -> Response:
    """下载下钻明细，维度引用与权限版本和页面完全一致。"""
    content, meta = _handle(
        drill_analysis.export_csv, account, cube_id, path, scope_version
    )
    # HTTP 头只能用 latin-1，中文范围标识按 RFC 5987 编码后再下发。
    encoded_label = quote(meta["scope_label"], safe="")
    return Response(
        content="﻿" + content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{meta["filename"]}"; '
                f"filename*=UTF-8''{quote(meta['filename'], safe='')}"
            ),
            "X-Scope-Version": meta["scope_version"],
            "X-Scope-Label": encoded_label,
        },
    )


@router.get("/manifest")
def list_manifest(
    account: str | None = Query(default=None),
    cube_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """下钻清单：聚合结果已按取数时的范围标识回写。"""
    items = _handle(drill_analysis.list_manifest, account, cube_id)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/shared")
def list_shared(reader: str = Query(..., description="当前读者的账号")) -> dict[str, Any]:
    """共享页清单：只列路径元信息，不返回作者写入的金额。"""
    return _handle(drill_analysis.list_shared, reader)


@router.get("/shared/{manifest_id}")
def shared_detail(
    manifest_id: int,
    reader: str = Query(...),
    scope_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """共享页面打开下钻：按当前读者自己的地区范围重新取数（只读）。"""
    return _handle(drill_analysis.shared_view, manifest_id, reader, scope_version)


@router.post("/scopes/revoke")
def revoke_scope(
    account: str = Query(...),
    region: str = Query(...),
    city: str | None = Query(default=None),
) -> dict[str, Any]:
    """临时收回某个地区或城市的访问权，权限版本随之变化。"""
    scope = _handle(access_scope.temporarily_revoke, account, region, city)
    return {"ok": True, "scope": access_scope.visible_scope_view(scope)}


@router.post("/scopes/restore")
def restore_scope(
    account: str = Query(...),
    region: str | None = Query(default=None),
    city: str | None = Query(default=None),
) -> dict[str, Any]:
    """恢复临时收回的授权。"""
    scope = _handle(access_scope.restore_access, account, region, city)
    return {"ok": True, "scope": access_scope.visible_scope_view(scope)}
