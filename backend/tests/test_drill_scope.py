"""授权范围下钻取数的验收测试：下钻、直查、下载三处读取口径一致。"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.domain import cube_facts
from app.main import app
from app.services import access_scope, drill_analysis
from app.store import store

client = TestClient(app)

EAST_WEST = access_scope.ACCOUNT_EAST_ALL_WEST_CHENGDU
CUBE = 2


@pytest.fixture(autouse=True)
def clean_state():
    access_scope.reset_overrides()
    drill_analysis.reset_seen_complete()
    store.rows("drill_manifest").clear()
    yield
    access_scope.reset_overrides()
    drill_analysis.reset_seen_complete()
    store.rows("drill_manifest").clear()


def _flat_metrics(payload: dict) -> list[tuple[str, int | None, int | None]]:
    rows = payload.get("groups") or payload.get("rows") or []
    return [(g["name"], (g["metrics"] or {}).get("revenue"), (g["metrics"] or {}).get("orders")) for g in rows]


class TestScope:
    def test_east_full_west_one_city(self):
        scope = access_scope.build_scope(EAST_WEST)
        assert scope["visible_cities"]["东区"] == ["上海", "杭州", "南京"]
        assert scope["visible_cities"]["西区"] == ["成都"]
        assert "重庆" in scope["hidden_cities"]["西区"]
        assert scope["complete"] is False
        assert scope["readonly"] is False

    def test_scope_version_is_stable_and_changes_on_revoke(self):
        first = access_scope.build_scope(EAST_WEST)["scope_version"]
        assert access_scope.build_scope(EAST_WEST)["scope_version"] == first
        revoked = access_scope.temporarily_revoke(EAST_WEST, "西区", "成都")
        assert revoked["scope_version"] != first
        restored = access_scope.restore_access(EAST_WEST)
        assert restored["scope_version"] == first


class TestDrill:
    def test_east_region_is_complete(self):
        payload = drill_analysis.drill(EAST_WEST, CUBE, "全国>东区")
        assert payload["parent"]["complete"] is True
        assert payload["parent"]["subtotal"] == {"revenue": 2500, "orders": 60}
        assert _flat_metrics(payload) == [
            ("上海", 1200, 30), ("杭州", 800, 20), ("南京", 500, 10),
        ]

    def test_west_region_has_no_full_subtotal(self):
        payload = drill_analysis.drill(EAST_WEST, CUBE, "全国>西区")
        assert payload["parent"]["complete"] is False
        assert payload["parent"]["subtotal"] is None
        assert payload["parent"]["subtotal_allowed"] is False
        # 仅成都可见，其余子地区不出现
        assert _flat_metrics(payload) == [("成都", 900, 25)]
        assert payload["groups"][0]["complete"] is True

    def test_restricted_child_never_leaks(self):
        payload = drill_analysis.drill(EAST_WEST, CUBE, "全国")
        names = [g["name"] for g in payload["groups"]]
        assert names == ["东区", "西区"]
        assert "南区" not in names and "北区" not in names
        # 全国父级含受限子节点：不给全量小计，也不含重庆 / 西安的金额
        assert payload["parent"]["subtotal"] is None
        assert payload["parent"]["partial_metrics"] == {"revenue": 3400, "orders": 85}

    def test_path_to_hidden_city_is_forbidden(self):
        with pytest.raises(drill_analysis.DrillError) as exc:
            drill_analysis.drill(EAST_WEST, CUBE, "全国>西区>重庆")
        assert exc.value.status == 403 and exc.value.code == "path_forbidden"

    def test_unknown_region_is_forbidden(self):
        with pytest.raises(drill_analysis.DrillError) as exc:
            drill_analysis.drill(EAST_WEST, CUBE, "全国>南区")
        assert exc.value.status == 403

    def test_every_group_carries_scope_version(self):
        payload = drill_analysis.drill(EAST_WEST, CUBE, "全国")
        version = payload["scope"]["scope_version"]
        assert payload["parent"]["scope_version"] == version
        for group in payload["groups"]:
            assert group["scope_version"] == version
            assert group["scope_tag"] == f"scope:{version}"

    def test_manifest_is_written_with_scope_tag(self):
        payload = drill_analysis.drill(EAST_WEST, CUBE, "全国>东区")
        manifest = payload["manifest"]
        assert manifest["组数"] == 3
        rows = store.rows("drill_manifest")
        current = [r for r in rows if r["status"] == "当前"]
        assert len(current) == 1
        assert current[0]["权限版本"] == payload["scope"]["scope_version"]
        assert all(g["scope_tag"].startswith("scope:") for g in current[0]["组结果"])


class TestRevocationContinuity:
    def test_old_version_request_is_rejected(self):
        version = access_scope.build_scope(EAST_WEST)["scope_version"]
        access_scope.temporarily_revoke(EAST_WEST, "西区", "成都")
        with pytest.raises(drill_analysis.DrillError) as exc:
            drill_analysis.drill(EAST_WEST, CUBE, "全国", version)
        assert exc.value.status == 409
        assert exc.value.code == "scope_version_mismatch"

    def test_revoke_one_city_withholds_previous_totals(self):
        # 全国分析员先看到完整总量
        first = drill_analysis.drill("analyst_national", CUBE, "全国")
        assert first["parent"]["subtotal"]["revenue"] == 8500
        assert any(g["name"] == "西区" for g in first["groups"])

        # 临时收回西区西安
        revoked = access_scope.temporarily_revoke("analyst_national", "西区", "西安")
        second = drill_analysis.drill("analyst_national", CUBE, "全国", revoked["scope_version"])

        west = next(g for g in second["groups"] if g["name"] == "西区")
        # 西区此前完整可见，收回后连部分小计也不给，避免用旧总量相减反推
        assert west["withheld"] is True
        assert west["metrics"] is None
        assert second["parent"]["subtotal"] is None
        assert second["parent"]["partial_metrics"] is None
        assert second["parent"]["withheld"] is True

    def test_continue_existing_path_only_proves_authorized_inputs(self):
        # 先完整看过西区（西区小计可见并留痕），再收回成渝
        drill_analysis.drill("analyst_national", CUBE, "全国>西区")
        access_scope.temporarily_revoke("analyst_national", "西区", "成都")
        access_scope.temporarily_revoke("analyst_national", "西区", "重庆")
        scope = access_scope.build_scope("analyst_national")
        payload = drill_analysis.drill("analyst_national", CUBE, "全国>西区", scope["scope_version"])
        assert [g["name"] for g in payload["groups"]] == ["西安"]
        assert payload["groups"][0]["metrics"] == {"revenue": 600, "orders": 15}
        # 此前看过完整西区，现在只剩部分输入 -> 所有小计压下
        assert payload["parent"]["withheld"] is True
        assert payload["parent"]["partial_metrics"] is None

    def test_shared_readonly_view_also_marks_prior_complete(self):
        # 只读读者先通过共享页看到完整南区，再被收回深圳
        author = drill_analysis.drill("analyst_national", CUBE, "全国")
        drill_analysis.shared_view(author["manifest"]["id"], "reader_south")
        revoked = access_scope.temporarily_revoke("reader_south", "南区", "深圳")
        payload = drill_analysis.drill("reader_south", CUBE, "全国>南区", revoked["scope_version"])
        assert [g["name"] for g in payload["groups"]] == ["广州"]
        assert payload["groups"][0]["metrics"] == {"revenue": 1100, "orders": 28}
        # 南区此前完整可见，收权后父级连部分小计也不给
        assert payload["parent"]["subtotal"] is None
        assert payload["parent"]["partial_metrics"] is None
        assert payload["parent"]["withheld"] is True


class TestDirectQuery:
    def test_direct_matches_drill_for_east(self):
        direct = drill_analysis.direct_query(EAST_WEST, CUBE, "city", region="东区")
        drill = drill_analysis.drill(EAST_WEST, CUBE, "全国>东区")
        assert _flat_metrics(direct) == _flat_metrics(drill)
        assert direct["parent"]["subtotal"] == drill["parent"]["subtotal"]

    def test_direct_west_only_visible_city(self):
        direct = drill_analysis.direct_query(EAST_WEST, CUBE, "city", region="西区")
        assert _flat_metrics(direct) == [("成都", 900, 25)]
        assert direct["parent"]["subtotal"] is None

    def test_direct_hidden_city_forbidden(self):
        with pytest.raises(drill_analysis.DrillError) as exc:
            drill_analysis.direct_query(EAST_WEST, CUBE, "city", region="西区", city="重庆")
        assert exc.value.status == 403

    def test_direct_national_hides_restricted_regions(self):
        direct = drill_analysis.direct_query(EAST_WEST, CUBE, "region")
        assert [r["name"] for r in direct["rows"]] == ["东区", "西区"]
        assert direct["rows"][0]["metrics"] == {"revenue": 2500, "orders": 60}


class TestExport:
    def test_export_uses_same_version_as_page(self):
        page = drill_analysis.drill(EAST_WEST, CUBE, "全国")
        text, meta = drill_analysis.export_csv(
            EAST_WEST, CUBE, "全国", page["scope"]["scope_version"]
        )
        assert meta["scope_version"] == page["scope"]["scope_version"]
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0][0] == "账号"
        data = rows[1:]
        assert all(row[1] == page["scope"]["scope_version"] for row in data)
        # 不含任何受限子地区的金额
        blob = text
        assert "重庆" not in blob and "西安" not in blob and "广州" not in blob

    def test_export_rejects_stale_version(self):
        version = access_scope.build_scope(EAST_WEST)["scope_version"]
        access_scope.temporarily_revoke(EAST_WEST, "东区", "南京")
        with pytest.raises(drill_analysis.DrillError) as exc:
            drill_analysis.export_csv(EAST_WEST, CUBE, "全国", version)
        assert exc.value.status == 409

    def test_export_marks_restricted_parent_without_amounts(self):
        text, _ = drill_analysis.export_csv(EAST_WEST, CUBE, "全国", None)
        rows = list(csv.reader(io.StringIO(text)))
        subtotal_row = next(row for row in rows if row[6].endswith("(小计不可展示)"))
        assert subtotal_row[7] == "" and subtotal_row[8] == ""
        west = next(row for row in rows if row[6] == "西区")
        assert west[7] == "900" and west[10] == "否"


class TestSharedReadonly:
    def test_shared_page_uses_reader_scope_not_author_scope(self):
        author = drill_analysis.drill("analyst_national", CUBE, "全国")
        manifest_id = author["manifest"]["id"]

        view = drill_analysis.shared_view(manifest_id, "reader_south")
        assert view["readonly"] is True
        assert view["scope"]["account"] == "reader_south"
        assert [g["name"] for g in view["groups"]] == ["南区"]
        # 南区读者只看到南区，不会拿到作者的全国组结果
        assert view["parent"]["subtotal"] is None
        assert view["parent"]["partial_metrics"] == {"revenue": 2100, "orders": 54}

    def test_shared_list_never_contains_metrics(self):
        drill_analysis.drill("analyst_national", CUBE, "全国")
        listing = drill_analysis.list_shared("reader_south")
        for item in listing["items"]:
            assert "metrics" not in str(item)
            assert "组结果" not in item

    def test_shared_readonly_export_matches_shared_view(self):
        author = drill_analysis.drill("analyst_national", CUBE, "全国")
        view = drill_analysis.shared_view(author["manifest"]["id"], "reader_south")
        text, meta = drill_analysis.export_csv(
            "reader_south", CUBE, "全国", view["scope"]["scope_version"]
        )
        assert meta["readonly"] is True
        assert "南区" in text
        assert "东区" not in text


class TestManifestVersioning:
    def test_manifest_requires_scope_version_to_expose_metrics(self):
        drill_analysis.drill(EAST_WEST, CUBE, "全国>东区")
        anonymous = drill_analysis.list_manifest()
        assert anonymous and all(item["组结果"] == [] for item in anonymous)

        scoped = drill_analysis.list_manifest(EAST_WEST, CUBE)
        assert scoped and scoped[0]["组结果"]

    def test_manifest_metrics_dropped_after_scope_change(self):
        drill_analysis.drill(EAST_WEST, CUBE, "全国>东区")
        access_scope.temporarily_revoke(EAST_WEST, "东区", "南京")
        items = drill_analysis.list_manifest(EAST_WEST, CUBE)
        assert all(item["组结果"] == [] and item["父级"] is None for item in items)
        assert items[0].get("version_stale") is True


class TestApi:
    def test_scope_endpoint(self):
        resp = client.get("/api/drill/scope", params={"account": EAST_WEST})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"]["visible_cities"]["西区"] == ["成都"]

    def test_drill_endpoint_and_403(self):
        ok = client.get("/api/drill/detail", params={"account": EAST_WEST, "cube_id": CUBE, "path": "全国>西区"})
        assert ok.status_code == 200
        assert ok.json()["parent"]["subtotal"] is None

        denied = client.get("/api/drill/detail", params={"account": EAST_WEST, "cube_id": CUBE, "path": "全国>西区>重庆"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "path_forbidden"

    def test_export_endpoint_headers(self):
        page = client.get("/api/drill/detail", params={"account": EAST_WEST, "cube_id": CUBE, "path": "全国"})
        version = page.json()["scope"]["scope_version"]
        resp = client.get("/api/drill/export", params={
            "account": EAST_WEST, "cube_id": CUBE, "path": "全国", "scope_version": version,
        })
        assert resp.status_code == 200
        assert resp.headers["x-scope-version"] == version
        assert "重庆" not in resp.text

    def test_revoke_then_stale_request_is_409(self):
        version = client.get("/api/drill/scope", params={"account": EAST_WEST}).json()["scope"]["scope_version"]
        client.post("/api/drill/scopes/revoke", params={"account": EAST_WEST, "region": "东区", "city": "南京"})
        resp = client.get("/api/drill/detail", params={
            "account": EAST_WEST, "cube_id": CUBE, "path": "全国", "scope_version": version,
        })
        assert resp.status_code == 409

    def test_shared_endpoint_readonly(self):
        page = client.get("/api/drill/detail", params={"account": "analyst_national", "cube_id": CUBE, "path": "全国"})
        manifest_id = page.json()["manifest"]["id"]
        resp = client.get(f"/api/drill/shared/{manifest_id}", params={"reader": "reader_south"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["readonly"] is True
        assert [g["name"] for g in body["groups"]] == ["南区"]
