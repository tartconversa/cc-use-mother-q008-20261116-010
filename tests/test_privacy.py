"""公开披露控制层测试：合成稀疏社区、相邻月份、增量补录与重新发布。

测试数据均为合成数据。核心安全断言是：任何公开数字的线性组合都无法还原被
保护社区×品类的精确重量，且抑制决策跨期单调（补录与重发布不会解除保护）。
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from looplake.api import create_app
from looplake.model import LakeError
from looplake.privacy import (
    REASON_DOMINANCE,
    REASON_HISTORICAL,
    REASON_SECONDARY,
    REASON_SPARSE,
    PrivacyPolicy,
)
from looplake.service import LakeService

MONTH_A, MONTH_B = "2026-07", "2026-08"


def deposit(service, ext, batch, device, material, weight, day, actor):
    return service.ingest({
        "source": "box-feed", "external_id": ext, "event_type": "deposit",
        "happened_at": f"{day}T10:00:00+08:00", "batch_id": batch,
        "channel": "smart_box", "material_id": material, "device_id": device,
        "weight_kg": str(weight), "actor_id": actor})


def weigh_in(service, ext, batch, material, weight, day):
    return service.ingest({
        "source": "scale-feed", "external_id": ext, "event_type": "weigh_in",
        "happened_at": f"{day}T11:00:00+08:00", "batch_id": batch,
        "channel": "scale", "material_id": material, "station_id": "station-1",
        "weight_kg": str(weight)})


def balanced(service, batch, device, material, weight, day, actor):
    d = deposit(service, batch + "-dep", batch, device, material, weight, day, actor)
    w = weigh_in(service, batch + "-in", batch, material, weight, day)
    return d, w


def setup_service(path: Path, policy: PrivacyPolicy | None = None) -> LakeService:
    service = LakeService(path, policy)
    for source, channel in (("box-feed", "smart_box"), ("scale-feed", "scale"),
                            ("station-feed", "station"), ("pay-feed", "payment")):
        service.register_source(source, channel)
    dimensions = [
        {"kind": "street", "external_id": "street-1", "name": "一街道", "valid_from": "2026-01-01"},
        {"kind": "street", "external_id": "street-2", "name": "二街道", "valid_from": "2026-01-01"},
        {"kind": "community", "external_id": "community-dense", "name": "密集社区",
         "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
        {"kind": "community", "external_id": "community-sparse", "name": "稀疏社区",
         "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
        {"kind": "community", "external_id": "community-third", "name": "第三社区",
         "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
        {"kind": "community", "external_id": "community-other", "name": "二街道社区",
         "valid_from": "2026-01-01", "attributes": {"street_id": "street-2"}},
        {"kind": "device", "external_id": "box-dense", "name": "密集箱",
         "valid_from": "2026-01-01", "attributes": {"community_id": "community-dense"}},
        {"kind": "device", "external_id": "box-sparse", "name": "稀疏箱",
         "valid_from": "2026-01-01", "attributes": {"community_id": "community-sparse"}},
        {"kind": "device", "external_id": "box-third", "name": "第三箱",
         "valid_from": "2026-01-01", "attributes": {"community_id": "community-third"}},
        {"kind": "device", "external_id": "box-other", "name": "二街道箱",
         "valid_from": "2026-01-01", "attributes": {"community_id": "community-other"}},
        {"kind": "station", "external_id": "station-1", "name": "中转站", "valid_from": "2026-01-01"},
        {"kind": "material", "external_id": "foam", "name": "泡沫", "valid_from": "2026-01-01"},
        {"kind": "material", "external_id": "glass", "name": "玻璃", "valid_from": "2026-01-01"},
    ]
    for actor in ("r1", "r2", "r3", "r4", "r5", "r6", "solo", "o1", "o2", "o3"):
        dimensions.append({"kind": "actor", "external_id": actor, "name": actor,
                           "valid_from": "2026-01-01"})
    for row in dimensions:
        service.register_dimension(row)
    return service


def dense_three(service, month: str, day: int, prefix: str, device="box-dense",
                community_actors=("r1", "r2", "r3"), weights=(10, 12, 8)):
    """在一个社区内造三个不同投递人、三个已对账批次。"""
    for index, (actor, weight) in enumerate(zip(community_actors, weights)):
        batch = f"{prefix}-b{index}"
        balanced(service, batch, device, "foam", weight, f"{month}-{day:02d}", actor)


def sparse_one(service, month: str, day: int, prefix: str, weight=5,
               device="box-sparse", actor="solo", material="foam"):
    balanced(service, f"{prefix}-sparse", device, material, weight,
             f"{month}-{day:02d}", actor)


class PrivacyBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = setup_service(Path(self.temp.name) / "lake.sqlite3")

    def test_sparse_cell_primary_and_complementary_suppression(self):
        dense_three(self.service, MONTH_A, 2, "dense")
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        preview = self.service.public_preview(MONTH_A)
        suppressed = {(r["community_id"], r["material_id"]): r["reason"]
                      for r in preview["suppressed_rows"]}
        # 稀疏社区被主抑制；互补抑制再保护一个社区，保证组内 >=2 个被抑制
        self.assertEqual(suppressed[("community-sparse", "foam")], REASON_SPARSE)
        self.assertGreaterEqual(len(suppressed), 2)
        self.assertIn(REASON_SECONDARY, suppressed.values())
        # 三个社区全部被抑制时，街道与区域总量级联抑制，不发布可被差分的总量
        street = next(r for r in preview["street_rows"] if r["street_id"] == "street-1")
        self.assertTrue(street["suppressed"])
        region = preview["region_rows"][0]
        self.assertTrue(region["suppressed"])

    def test_partial_group_keeps_explainable_street_total(self):
        # 组内有三个社区：稀疏社区被抑制，互补抑制命中最小的第三社区，密集社区可见
        dense_three(self.service, MONTH_A, 2, "dense")
        dense_three(self.service, MONTH_A, 3, "third", device="box-third",
                    community_actors=("r4", "r5", "r6"), weights=(1, 2, 3))
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        preview = self.service.public_preview(MONTH_A)
        visible = {r["community_id"]: r for r in preview["community_rows"]}
        suppressed = {(r["community_id"]): r["reason"] for r in preview["suppressed_rows"]}
        self.assertEqual(set(visible), {"community-dense"})
        self.assertEqual(suppressed["community-sparse"], REASON_SPARSE)
        self.assertEqual(suppressed["community-third"], REASON_SECONDARY)
        street = next(r for r in preview["street_rows"] if r["street_id"] == "street-1")
        # 街道总量 = 可见社区之和（被抑制重量不计入），关系可解释且无残留可减
        expected = sum((Decimal(r["deposit_kg"]) for r in preview["community_rows"]
                        if r["street_id"] == "street-1"), Decimal("0"))
        self.assertEqual(expected, Decimal("30"))
        self.assertFalse(street["suppressed"])
        self.assertEqual(Decimal(street["deposit_kg"]), expected)
        self.assertEqual(street["suppressed_community_count"], 2)
        region = preview["region_rows"][0]
        self.assertEqual(Decimal(region["deposit_kg"]), expected)

    def test_dominance_rule_fires_on_single_large_contributor(self):
        # 三个投递人满足 k 匿名，但最大投递人重量 90 / 100 = 0.9 > 0.8
        dense_three(self.service, MONTH_A, 2, "dom", weights=(90, 5, 5))
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        preview = self.service.public_preview(MONTH_A)
        reasons = {(r["community_id"]): r["reason"] for r in preview["suppressed_rows"]}
        self.assertEqual(reasons["community-dense"], REASON_DOMINANCE)

    def test_policy_validation(self):
        with self.assertRaises(LakeError):
            PrivacyPolicy(min_contributors=1).validate()
        with self.assertRaises(LakeError):
            PrivacyPolicy(max_dominance="1.2").validate()
        with self.assertRaises(LakeError):
            PrivacyPolicy(max_dominance="0").validate()

    def test_only_ready_batches_enter_public_view(self):
        # 只有 deposit 没有 weigh_in 的批次未对账，不得进入公开视图
        deposit(self.service, "orphan-dep", "orphan", "box-sparse", "foam", 5,
                "2026-07-09", "solo")
        preview = self.service.public_preview(MONTH_A)
        self.assertEqual(preview["community_rows"], [])
        self.assertEqual(preview["suppressed_rows"], [])


class SuppressionMonotonicityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = setup_service(Path(self.temp.name) / "lake.sqlite3")

    def publish(self, name, month):
        preview = self.service.public_preview(month)
        result = self.service.publish_public(name, month, preview["fingerprint"])
        self.assertTrue(result["created"])
        return result

    def test_backfill_cannot_unprotect_sparse_community(self):
        dense_three(self.service, MONTH_A, 2, "jul-dense")
        sparse_one(self.service, MONTH_A, 9, "jul-sparse", weight=5)
        first = self.publish("public-jul-1", MONTH_A)
        first_view = self.service.public_releases.get(first["public_release_id"])["content"]
        protected_july = {(r["community_id"], r["material_id"])
                          for r in first_view["suppressed_rows"]}
        self.assertIn(("community-sparse", "foam"), protected_july)

        # 增量补录：稀疏社区新增两个投递人，按当月规则本已达到 k=3
        balanced(self.service, "backfill-1", "box-sparse", "foam", 6,
                 "2026-07-20", "r2")
        balanced(self.service, "backfill-2", "box-sparse", "foam", 7,
                 "2026-07-21", "r3")
        republish = self.publish("public-jul-2", MONTH_A)
        second_view = self.service.public_releases.get(
            republish["public_release_id"])["content"]
        reasons = {(r["community_id"], r["material_id"]): r["reason"]
                   for r in second_view["suppressed_rows"]}
        # 历史保护使该社区在重新发布中继续抑制，差分无法还原其重量变化
        self.assertEqual(reasons[("community-sparse", "foam")], REASON_HISTORICAL)
        # 补录重量全部落在被抑制社区：可见社区行、街道与区域总量两版完全一致，
        # 公开读者无法通过两版相减推断补录的 13kg
        def visible_totals(view):
            community = sorted((r["street_id"], r["community_id"], r["material_id"],
                                r["deposit_kg"]) for r in view["community_rows"])
            street = sorted((r["street_id"], r["material_id"],
                             None if r["suppressed"] else r["deposit_kg"])
                            for r in view["street_rows"])
            region = sorted((r["material_id"],
                             None if r["suppressed"] else r["deposit_kg"])
                            for r in view["region_rows"])
            return community, street, region
        self.assertEqual(visible_totals(first_view), visible_totals(second_view))
        # 旧冻结版完全不变
        self.assertEqual(
            self.service.public_releases.get(first["public_release_id"])["fingerprint"],
            first["fingerprint"])

    def test_adjacent_months_carry_historical_protection(self):
        dense_three(self.service, MONTH_A, 2, "jul-dense")
        sparse_one(self.service, MONTH_A, 9, "jul-sparse", weight=5)
        self.publish("public-jul", MONTH_A)

        # 八月：稀疏社区有三个投递人，单期看并不稀疏
        dense_three(self.service, MONTH_B, 2, "aug-dense")
        balanced(self.service, "aug-s1", "box-sparse", "foam", 3, "2026-08-05", "r1")
        balanced(self.service, "aug-s2", "box-sparse", "foam", 4, "2026-08-06", "r2")
        balanced(self.service, "aug-s3", "box-sparse", "foam", 5, "2026-08-07", "r3")
        preview = self.service.public_preview(MONTH_B)
        reasons = {(r["community_id"], r["material_id"]): r["reason"]
                   for r in preview["suppressed_rows"]}
        self.assertEqual(reasons[("community-sparse", "foam")], REASON_HISTORICAL)
        # 台账跨期保留且只增不减
        ledger = self.service.disclosure_audit.ledger(
            self.service.disclosure_audit.AUDITOR_TOKEN)
        pairs = {(e["community_id"], e["material_id"]) for e in ledger["entries"]}
        self.assertIn(("community-sparse", "foam"), pairs)

    def test_dormant_month_keeps_placeholder_suppression(self):
        # 七月稀疏社区被保护；八月它完全没有投递，抑制占位仍须出现，
        # 否则“抑制行消失”本身就泄露了本月零活动这一事实
        dense_three(self.service, MONTH_A, 2, "jul-dense")
        sparse_one(self.service, MONTH_A, 9, "jul-sparse", weight=5)
        self.publish("public-jul", MONTH_A)
        dense_three(self.service, MONTH_B, 2, "aug-dense")
        aug = self.publish("public-aug", MONTH_B)
        content = self.service.public_releases.get(aug["public_release_id"])["content"]
        dormant = [r for r in content["suppressed_rows"]
                   if r["community_id"] == "community-sparse"]
        self.assertEqual(len(dormant), 1)
        self.assertEqual(dormant[0]["reason"], REASON_HISTORICAL)
        # 占位行不含任何重量或事实标识
        self.assertNotIn("deposit_kg", dormant[0])
        self.assertNotIn("fact_id", str(dormant[0]))
        # 公开查询中该社区与普通可见空结果形状一致，差异仅在抑制原因
        queried = self.service.public_releases.public_community(
            aug["public_release_id"], "community-sparse")
        self.assertEqual(queried["rows"], [])
        self.assertEqual(queried["suppressed"][0]["reason"], REASON_HISTORICAL)
        # 审计视图：休眠占位 internal 为 None，不会伪造事实，而有事实的单元格可追溯
        audited = self.service.disclosure_audit.protected_cells(
            MONTH_B, self.service.disclosure_audit.AUDITOR_TOKEN)["cells"]
        dormant_cell = next(c for c in audited
                            if c["community_id"] == "community-sparse")
        self.assertIsNone(dormant_cell["internal"])
        dense_cell = next(c for c in audited if c["community_id"] == "community-dense")
        self.assertIsNotNone(dense_cell["internal"]["fact_ids"])

    def test_difference_attack_cannot_reconstruct_protected_weight(self):
        dense_three(self.service, MONTH_A, 2, "dense")
        dense_three(self.service, MONTH_A, 3, "third", device="box-third",
                    community_actors=("r4", "r5", "r6"), weights=(1, 2, 3))
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        release = self.publish("public-jul", MONTH_A)
        content = self.service.public_releases.get(release["public_release_id"])["content"]

        # 公开载荷中不得出现任何内部事实标识
        raw = str(content)
        self.assertNotIn("fact_id", raw)
        self.assertNotIn("fact_ids", raw)

        visible_sum = sum((Decimal(r["deposit_kg"]) for r in content["community_rows"]),
                          Decimal("0"))
        # 街道总量恰好等于可见社区之和：不存在“总量减可见量”的残差
        for street in content["street_rows"]:
            if not street["suppressed"]:
                street_visible = sum(
                    (Decimal(r["deposit_kg"]) for r in content["community_rows"]
                     if r["street_id"] == street["street_id"]), Decimal("0"))
                self.assertEqual(Decimal(street["deposit_kg"]), street_visible)
        # 区域总量 = 所有未抑制街道总量之和
        region_sum = sum((Decimal(r["deposit_kg"]) for r in content["region_rows"]
                          if not r["suppressed"]), Decimal("0"))
        street_sum = sum((Decimal(r["deposit_kg"]) for r in content["street_rows"]
                          if not r["suppressed"]), Decimal("0"))
        self.assertEqual(region_sum, street_sum)
        self.assertEqual(visible_sum, street_sum)
        # 被保护的精确重量 5kg 在任何公开数值中都不出现
        self.assertNotIn("5", [r["deposit_kg"] for r in content["community_rows"]])

    def test_material_slice_uses_same_suppression(self):
        dense_three(self.service, MONTH_A, 2, "foam-dense")
        sparse_one(self.service, MONTH_A, 9, "foam-sparse", weight=5)
        # 玻璃品类：稀疏社区只投 1 次
        balanced(self.service, "glass-d1", "box-dense", "glass", 4, "2026-07-04", "r1")
        balanced(self.service, "glass-d2", "box-dense", "glass", 6, "2026-07-05", "r2")
        balanced(self.service, "glass-d3", "box-dense", "glass", 8, "2026-07-06", "r3")
        balanced(self.service, "glass-s1", "box-sparse", "glass", 3, "2026-07-07", "solo")
        release = self.publish("public-jul", MONTH_A)
        foam_slice = self.service.public_releases.public_material_slice(
            release["public_release_id"], "foam")
        glass_slice = self.service.public_releases.public_material_slice(
            release["public_release_id"], "glass")
        foam_protected = {(r["community_id"], r["material_id"])
                          for r in foam_slice["suppressed_rows"]}
        glass_protected = {(r["community_id"], r["material_id"])
                           for r in glass_slice["suppressed_rows"]}
        self.assertIn(("community-sparse", "foam"), foam_protected)
        self.assertIn(("community-sparse", "glass"), glass_protected)
        # 两个品类切片里每个街道组都至少有两个被抑制社区，单切片也无法差分
        for slice_rows in (foam_slice["suppressed_rows"], glass_slice["suppressed_rows"]):
            groups = {}
            for row in slice_rows:
                groups.setdefault((row["street_id"], row["material_id"]), 0)
                groups[(row["street_id"], row["material_id"])] += 1
            self.assertTrue(all(count >= 2 for count in groups.values()))


class PublicReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = setup_service(Path(self.temp.name) / "lake.sqlite3")

    def test_fingerprint_guards_and_idempotent_name(self):
        dense_three(self.service, MONTH_A, 2, "dense")
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        preview = self.service.public_preview(MONTH_A)
        with self.assertRaises(LakeError) as failure:
            self.service.publish_public("pub", MONTH_A, "a" * 64)
        self.assertEqual(failure.exception.code, "stale_preview")
        first = self.service.publish_public("pub-jul", MONTH_A, preview["fingerprint"])
        again = self.service.publish_public("pub-jul", MONTH_A, preview["fingerprint"])
        self.assertFalse(again["created"])
        self.assertEqual(first["public_release_id"], again["public_release_id"])
        # 同名不同内容冲突
        balanced(self.service, "extra", "box-dense", "foam", 3, "2026-07-22", "r1")
        new_fp = self.service.public_preview(MONTH_A)["fingerprint"]
        with self.assertRaises(LakeError) as failure:
            self.service.publish_public("pub-jul", MONTH_A, new_fp)
        self.assertEqual(failure.exception.code, "public_release_conflict")

    def test_listing_and_queries_read_only_frozen_content(self):
        dense_three(self.service, MONTH_A, 2, "dense")
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        release = self.service.publish_public(
            "pub-jul", MONTH_A, self.service.public_preview(MONTH_A)["fingerprint"])
        rid = release["public_release_id"]
        listing = self.service.public_releases.listing(MONTH_A)
        self.assertEqual([r["name"] for r in listing["releases"]], ["pub-jul"])
        community = self.service.public_releases.public_community(rid, "community-sparse")
        self.assertEqual(community["rows"], [])
        self.assertTrue(community["suppressed"])
        street = self.service.public_releases.public_street(rid, "street-1")
        self.assertTrue(all(r["street_id"] == "street-1" for r in street["rows"]))
        region = self.service.public_releases.public_region(rid)
        self.assertTrue(region["rows"])
        with self.assertRaises(LakeError) as failure:
            self.service.public_releases.get(999)
        self.assertEqual(failure.exception.code, "unknown_public_release")

    def test_internal_lineage_remains_traceable_for_auditors(self):
        dense_three(self.service, MONTH_A, 2, "dense")
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)
        self.service.publish_public(
            "pub-jul", MONTH_A, self.service.public_preview(MONTH_A)["fingerprint"])
        audit = self.service.disclosure_audit
        # 无令牌 / 错误令牌一律拒绝
        with self.assertRaises(LakeError) as failure:
            audit.protected_cells(MONTH_A)
        self.assertEqual(failure.exception.status, 403)
        with self.assertRaises(LakeError) as failure:
            audit.protected_cells(MONTH_A, "wrong")
        self.assertEqual(failure.exception.code, "forbidden")
        cells = audit.protected_cells(MONTH_A, audit.AUDITOR_TOKEN)["cells"]
        target = next(c for c in cells
                      if c["community_id"] == "community-sparse" and c["material_id"] == "foam")
        self.assertEqual(target["internal"]["exact_deposit_kg"], "5")
        self.assertTrue(target["internal"]["fact_ids"])
        lineage = audit.cell_lineage(MONTH_A, "community-sparse", "foam",
                                     audit.AUDITOR_TOKEN)
        self.assertEqual(lineage["exact_deposit_kg"], "5")
        self.assertEqual(lineage["traces"][0]["fact"]["event_type"], "deposit")
        with self.assertRaises(LakeError):
            audit.cell_lineage(MONTH_A, "community-sparse", "foam")


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = Path(self.temp.name) / "api.sqlite3"
        self.client = TestClient(create_app(db_path))
        # 客户端与播种服务共享同一个 SQLite 文件
        self.service = LakeService(db_path)
        self._seed_shared()

    def _seed_shared(self):
        for source, channel in (("box-feed", "smart_box"), ("scale-feed", "scale"),
                                ("station-feed", "station"), ("pay-feed", "payment")):
            self.service.register_source(source, channel)
        dimensions = [
            {"kind": "street", "external_id": "street-1", "name": "一街道",
             "valid_from": "2026-01-01"},
            {"kind": "community", "external_id": "community-dense", "name": "密集",
             "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
            {"kind": "community", "external_id": "community-sparse", "name": "稀疏",
             "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
            {"kind": "community", "external_id": "community-third", "name": "第三",
             "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}},
            {"kind": "device", "external_id": "box-dense", "name": "密集箱",
             "valid_from": "2026-01-01", "attributes": {"community_id": "community-dense"}},
            {"kind": "device", "external_id": "box-sparse", "name": "稀疏箱",
             "valid_from": "2026-01-01", "attributes": {"community_id": "community-sparse"}},
            {"kind": "device", "external_id": "box-third", "name": "第三箱",
             "valid_from": "2026-01-01", "attributes": {"community_id": "community-third"}},
            {"kind": "station", "external_id": "station-1", "name": "站",
             "valid_from": "2026-01-01"},
            {"kind": "material", "external_id": "foam", "name": "泡沫",
             "valid_from": "2026-01-01"},
        ]
        for actor in ("r1", "r2", "r3", "solo"):
            dimensions.append({"kind": "actor", "external_id": actor, "name": actor,
                               "valid_from": "2026-01-01"})
        for row in dimensions:
            self.service.register_dimension(row)
        dense_three(self.service, MONTH_A, 2, "dense")
        dense_three(self.service, MONTH_A, 3, "third", device="box-third",
                    community_actors=("r1", "r2", "r3"), weights=(7, 9, 11))
        sparse_one(self.service, MONTH_A, 9, "sparse", weight=5)

    def test_public_endpoints_and_role_boundary(self):
        preview = self.client.get(f"/public/months/{MONTH_A}/preview").json()
        self.assertNotIn("ledger_actions", preview)
        self.assertNotIn("fact_id", str(preview))
        publish = self.client.post("/public/releases", json={
            "name": "pub-jul", "month": MONTH_A,
            "expected_fingerprint": preview["fingerprint"]})
        self.assertEqual(publish.status_code, 200)
        rid = publish.json()["public_release_id"]

        community = self.client.get(f"/public/releases/{rid}/communities/community-sparse")
        self.assertEqual(community.json()["rows"], [])
        self.assertTrue(community.json()["suppressed"])
        street = self.client.get(f"/public/releases/{rid}/streets/street-1").json()
        self.assertTrue(street["rows"])
        region = self.client.get(f"/public/releases/{rid}/region").json()
        self.assertTrue(region["rows"])
        slice_resp = self.client.get(f"/public/releases/{rid}/materials/foam").json()
        self.assertIn("suppressed_rows", slice_resp)
        listing = self.client.get(f"/public/releases?month={MONTH_A}").json()
        self.assertEqual(listing["releases"][0]["name"], "pub-jul")

        # 审计端点在无令牌时 403，带令牌才返回含 fact_id 的内部追溯
        denied = self.client.get(f"/audit/public/months/{MONTH_A}/protected-cells")
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get(
            f"/audit/public/months/{MONTH_A}/protected-cells",
            headers={"x-auditor-token": "auditor-token"})
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["cells"][0]["internal"]["fact_ids"])
        ledger = self.client.get("/audit/public/suppression-ledger",
                                 headers={"x-auditor-token": "auditor-token"})
        self.assertEqual(ledger.status_code, 200)
        self.assertTrue(ledger.json()["entries"])

    def test_public_preview_extra_field_rejected(self):
        response = self.client.post("/public/releases", json={
            "name": "x", "month": MONTH_A, "expected_fingerprint": "a" * 64,
            "unexpected": 1})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
