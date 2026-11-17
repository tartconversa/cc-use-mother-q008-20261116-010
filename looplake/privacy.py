"""面向公众的披露控制（统计保密）层。

内部事实表按街道/社区/品类保留精确明细，但公开月报只能发布聚合值。本模块在内部
已对账批次之上构建公开视图，并施加两类规则：

1. 主抑制（primary suppression）
   - k 匿名：单元格投递人数 < MIN_CONTRIBUTORS 时抑制（sparse_contributors）；
   - 主导性：最大单一投递人重量占比 > MAX_DOMINANCE 时抑制（dominance）。
2. 互补抑制（secondary suppression）
   每个街道×品类组内被抑制的社区至少有两个，否则再抑制一个最小社区，防止
   “街道总量 − 可见社区 = 被抑制社区”的差分反推（secondary_suppression）。

抑制决策写入跨期单调的 suppression_ledger：一旦某社区×品类在任一期被保护，
后续相邻月份、补录后的重新发布都会继续抑制（historical_protection），因此
多期对比、品类切片、补录事件与重发布都无法通过差分让受保护群体重新暴露。
公开载荷只含聚合值与抑制原因，不含任何 fact_id 或精确重量；原始事实血缘仍
由内部 mart / lineage 在审计角色下追溯。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json

from .model import LakeError, calendar_day, digest, key, now
from .reconcile import Reconciliation
from .store import Store

# 公开抑制原因码（对外可展示，仅描述规则，不泄露任何个体数值）
REASON_SPARSE = "sparse_contributors"
REASON_DOMINANCE = "dominance"
REASON_SECONDARY = "secondary_suppression"
REASON_HISTORICAL = "historical_protection"
REASON_GROUP_TOTAL = "group_fully_suppressed"


@dataclass(frozen=True)
class PrivacyPolicy:
    min_contributors: int = 3
    max_dominance: str = "0.8"

    def fingerprint(self) -> str:
        return digest({"min_contributors": self.min_contributors,
                       "max_dominance": self.max_dominance})

    def validate(self):
        if type(self.min_contributors) is not int or not 2 <= self.min_contributors <= 100:
            raise LakeError("invalid_policy", "min_contributors must be an integer in 2..100")
        dominance = Decimal(str(self.max_dominance))
        if not dominance.is_finite() or not Decimal("0") < dominance <= Decimal("1"):
            raise LakeError("invalid_policy", "max_dominance must be in (0,1]")
        return dominance


class PublicView:
    """基于内部已对账事实构建受保护的公开聚合视图。"""

    def __init__(self, store: Store, reconciliation: Reconciliation,
                 policy: PrivacyPolicy | None = None):
        self.store = store
        self.reconciliation = reconciliation
        self.policy = policy or PrivacyPolicy()
        self.dominance_limit = self.policy.validate()

    # ------------------------------------------------------------------ #
    # 内部单元格构建：仅纳入已解决（ready）批次的 deposit 事实
    # ------------------------------------------------------------------ #
    def _ready_batches(self, month: str) -> set[str]:
        checked = self.reconciliation.month(month)
        return {batch["batch_id"] for batch in checked["batches"] if batch["ready"]}

    def _cells(self, month: str) -> dict:
        ready_batches = self._ready_batches(month)
        facts = self.reconciliation.ledger.period(month)
        cells: dict = {}
        unassigned = 0
        for fact in facts:
            if fact["event_type"] != "deposit" or fact["batch_id"] not in ready_batches:
                continue
            if not fact["community_id"] or not fact["street_id"]:
                unassigned += 1
                continue
            cell = cells.setdefault((fact["street_id"], fact["community_id"], fact["material_id"]),
                                    {"kg": Decimal("0"), "events": 0,
                                     "by_actor": {}, "fact_ids": []})
            weight = Decimal(fact["weight_kg"] or "0")
            cell["kg"] += weight
            cell["events"] += 1
            cell["fact_ids"].append(fact["id"])
            # actor 可能为空（匿名投递）；未知 actor 各自视为独立投递人，
            # 这只会让计数更保守，不会低估被识别风险。
            actor = fact["actor_id"] or f"anonymous:{fact['id']}"
            actor_weight = cell["by_actor"].setdefault(actor, Decimal("0"))
            cell["by_actor"][actor] = actor_weight + weight
        return cells, unassigned

    def _primary_reason(self, cell: dict) -> str | None:
        contributors = len(cell["by_actor"])
        if contributors < self.policy.min_contributors:
            return REASON_SPARSE
        largest = max(cell["by_actor"].values())
        if cell["kg"] > 0 and largest / cell["kg"] > self.dominance_limit:
            return REASON_DOMINANCE
        return None

    def _community_street(self, community_id: str, month: str) -> str | None:
        """从社区维度当前属性解析所属街道；维度失效时回退到历史事实。"""
        day = month + "-01"
        with self.store.connect() as conn:
            rows = self.store.all(
                conn,
                "SELECT attributes FROM dimension_versions WHERE kind='community' "
                "AND external_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) "
                "ORDER BY version DESC",
                (community_id, day, day))
        if rows:
            return json.loads(rows[0]["attributes"]).get("street_id")
        with self.store.connect() as conn:
            fact = self.store.one(
                conn,
                "SELECT street_id FROM facts WHERE community_id=? AND street_id IS NOT NULL "
                "ORDER BY id LIMIT 1", (community_id,))
        return fact["street_id"] if fact else None

    # ------------------------------------------------------------------ #
    # 受保护视图构建
    # ------------------------------------------------------------------ #
    def build(self, month: str) -> dict:
        calendar_day(month + "-01", "month")
        cells, unassigned = self._cells(month)
        historical = {(row["community_id"], row["material_id"]): row
                      for row in self.store.suppression_entries()}
        decisions: dict = {}
        for (street_id, community_id, material_id), cell in cells.items():
            reason = self._primary_reason(cell)
            carried = (community_id, material_id) in historical
            if reason is None and carried:
                reason = REASON_HISTORICAL
            if reason is not None:
                decisions[(street_id, community_id, material_id)] = reason

        # 历史受保护对在零投递月份也保留合成抑制行（无重量、无 fact_id），
        # 使“本月无活动”与“本月有活动但被抑制”在公开接口上不可区分，
        # 防止以抑制列表的出现/消失作为存在性侧信道。
        community_street = {key_tuple[1:]: key_tuple[0] for key_tuple in cells}
        dormant = []
        present_pairs = {key_tuple[1:] for key_tuple in cells}
        for (community_id, material_id), entry in sorted(historical.items()):
            if (community_id, material_id) not in present_pairs:
                street_id = community_street.get((community_id, material_id)) \
                    or self._community_street(community_id, month)
                if street_id is None:
                    continue
                dormant.append({"month": month, "street_id": street_id,
                                "community_id": community_id, "material_id": material_id,
                                "suppressed": True, "reason": REASON_HISTORICAL,
                                "policy": {"min_contributors": self.policy.min_contributors,
                                           "max_dominance": self.policy.max_dominance}})

        # 互补抑制：每个街道×品类组至少两个被抑制社区，杜绝单格差分
        groups: dict = {}
        for (street_id, community_id, material_id) in cells:
            groups.setdefault((street_id, material_id), set()).add(community_id)
        for row in dormant:
            groups.setdefault((row["street_id"], row["material_id"]), set()) \
                .add(row["community_id"])
        for (street_id, material_id), community_ids in sorted(groups.items()):
            suppressed = {community for community in community_ids
                          if (street_id, community, material_id) in decisions}
            # 休眠（本月零投递）的历史保护社区也计入组内抑制数量
            suppressed |= {row["community_id"] for row in dormant
                           if row["street_id"] == street_id
                           and row["material_id"] == material_id}
            if suppressed and len(suppressed) < 2:
                # 选未受保护社区中重量最小者承担互补抑制；重量并列时按投递事件数、
                # 再按社区标识打破并列，保证决策确定且对发布者最省信息损失。
                candidates = sorted(
                    (community for community in community_ids if community not in suppressed),
                    key=lambda community: (
                        cells[(street_id, community, material_id)]["kg"],
                        cells[(street_id, community, material_id)]["events"],
                        community))
                if candidates:
                    decisions[(street_id, candidates[0], material_id)] = REASON_SECONDARY

        community_rows, suppressed_rows = [], []
        for (street_id, community_id, material_id), cell in sorted(cells.items()):
            reason = decisions.get((street_id, community_id, material_id))
            if reason is None:
                community_rows.append({
                    "month": month, "street_id": street_id, "community_id": community_id,
                    "material_id": material_id, "deposit_kg": format(cell["kg"], "f"),
                    "contributor_count": len(cell["by_actor"]),
                    "deposit_events": cell["events"], "suppressed": False})
            else:
                suppressed_rows.append({
                    "month": month, "street_id": street_id, "community_id": community_id,
                    "material_id": material_id, "suppressed": True,
                    "reason": reason,
                    "policy": {"min_contributors": self.policy.min_contributors,
                               "max_dominance": self.policy.max_dominance}})
        suppressed_rows.extend(dormant)
        suppressed_rows.sort(key=lambda row: (row["street_id"], row["community_id"],
                                              row["material_id"]))

        street_rows = self._street_rows(month, cells, decisions, groups, dormant)
        region_rows = self._region_rows(month, street_rows)
        ledger_actions = self._ledger_actions(month, cells, decisions, historical)
        return {
            "month": month,
            "policy": {"min_contributors": self.policy.min_contributors,
                       "max_dominance": self.policy.max_dominance,
                       "fingerprint": self.policy.fingerprint()},
            "community_rows": community_rows,
            "suppressed_rows": suppressed_rows,
            "street_rows": street_rows,
            "region_rows": region_rows,
            "unassigned_deposit_events": unassigned,
            "ledger_actions": ledger_actions,
            "fingerprint": digest({
                "community_rows": community_rows, "suppressed_rows": suppressed_rows,
                "street_rows": street_rows, "region_rows": region_rows,
                "unassigned": unassigned, "month": month,
                "policy": self.policy.fingerprint()}),
        }

    def _street_rows(self, month, cells, decisions, groups, dormant):
        dormant_pairs = {(row["street_id"], row["community_id"], row["material_id"])
                         for row in dormant}
        rows = []
        for (street_id, material_id), community_ids in sorted(groups.items()):
            visible_kg, visible_events, visible_contributors = Decimal("0"), 0, 0
            suppressed_count = 0
            for community_id in sorted(community_ids):
                triple = (street_id, community_id, material_id)
                if triple in decisions or triple in dormant_pairs:
                    suppressed_count += 1
                    continue
                cell = cells[triple]
                visible_kg += cell["kg"]
                visible_events += cell["events"]
                visible_contributors += len(cell["by_actor"])
            if suppressed_count and suppressed_count >= len(community_ids):
                rows.append({"month": month, "street_id": street_id, "material_id": material_id,
                             "suppressed": True, "reason": REASON_GROUP_TOTAL,
                             "policy": {"min_contributors": self.policy.min_contributors,
                                        "max_dominance": self.policy.max_dominance}})
                continue
            rows.append({"month": month, "street_id": street_id, "material_id": material_id,
                         "suppressed": False, "deposit_kg": format(visible_kg, "f"),
                         "contributor_count": visible_contributors,
                         "deposit_events": visible_events,
                         "suppressed_community_count": suppressed_count})
        return rows

    def _region_rows(self, month, street_rows):
        totals: dict = {}
        for row in street_rows:
            bucket = totals.setdefault(row["material_id"],
                                       {"kg": Decimal("0"), "events": 0, "contributors": 0,
                                        "streets": 0, "suppressed_streets": 0})
            bucket["streets"] += 1
            if row["suppressed"]:
                bucket["suppressed_streets"] += 1
                continue
            bucket["kg"] += Decimal(row["deposit_kg"])
            bucket["events"] += row["deposit_events"]
            bucket["contributors"] += row["contributor_count"]
        rows = []
        for material_id, bucket in sorted(totals.items()):
            if bucket["suppressed_streets"] and bucket["suppressed_streets"] == bucket["streets"]:
                rows.append({"month": month, "material_id": material_id, "suppressed": True,
                             "reason": REASON_GROUP_TOTAL,
                             "policy": {"min_contributors": self.policy.min_contributors,
                                        "max_dominance": self.policy.max_dominance}})
            else:
                rows.append({"month": month, "material_id": material_id, "suppressed": False,
                             "deposit_kg": format(bucket["kg"], "f"),
                             "contributor_count": bucket["contributors"],
                             "deposit_events": bucket["events"],
                             "suppressed_street_count": bucket["suppressed_streets"]})
        return rows

    def _ledger_actions(self, month, cells, decisions, historical):
        """生成跨期单调台账动作：只新增/保持，永不删除。"""
        actions = []
        for (street_id, community_id, material_id), reason in sorted(decisions.items()):
            entry = historical.get((community_id, material_id))
            if entry is None:
                actions.append({"action": "add", "month": month,
                                "community_id": community_id, "material_id": material_id,
                                "reason": reason})
            else:
                actions.append({"action": "carry", "month": month,
                                "community_id": community_id, "material_id": material_id,
                                "reason": reason, "first_month": entry["month"],
                                "first_reason": entry["reason"]})
        return actions


class PublicRelease:
    """冻结公开版本并提供受角色约束的查询。"""

    def __init__(self, store: Store, view: PublicView):
        self.store, self.view = store, view

    def preview(self, month: str) -> dict:
        return self.view.build(month)

    def publish(self, name: str, month: str, expected_fingerprint: str) -> dict:
        name = key(name, "name")
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
            raise LakeError("invalid_fingerprint", "expected_fingerprint must be SHA-256")
        with self.store.write() as conn:
            previous = self.store.one(conn, "SELECT * FROM public_releases WHERE name=?", (name,))
            if previous:
                # 同名幂等：相同指纹直接返回；不同内容一律冲突，绝不覆盖冻结版
                if previous["fingerprint"] == expected_fingerprint:
                    return {"public_release_id": previous["id"], "name": name,
                            "fingerprint": previous["fingerprint"], "created": False}
                raise LakeError("public_release_conflict",
                                "public release name already has different content", 409)
            preview = self.preview(month)
            if expected_fingerprint != preview["fingerprint"]:
                raise LakeError("stale_preview", "public month changed since preview", 409)
            if self.preview(month)["fingerprint"] != expected_fingerprint:
                raise LakeError("stale_preview", "public month changed during release", 409)
            release_id = self._persist(conn, name, month, preview)
        return {"public_release_id": release_id, "name": name,
                "fingerprint": preview["fingerprint"], "created": True}

    def _persist(self, conn, name, month, preview):
        # 先冻结抑制台账（跨期单调），再冻结公开载荷，二者同事务提交
        known = {(row["community_id"], row["material_id"]): row
                 for row in self.store.all(conn, "SELECT * FROM suppression_ledger")}
        for action in preview["ledger_actions"]:
            pair = (action["community_id"], action["material_id"])
            if pair not in known:
                cursor = conn.execute(
                    "INSERT INTO suppression_ledger(month,community_id,material_id,reason,"
                    "first_release_id,first_seen_at,rule_fingerprint) VALUES(?,?,?,?,?,?,?)",
                    (action["month"], pair[0], pair[1], action["reason"], None, now(),
                     preview["policy"]["fingerprint"]))
                known[pair] = {"id": cursor.lastrowid}
                self.store.audit(conn, "suppression_registered",
                                 f"{pair[0]}:{pair[1]}",
                                 {"month": action["month"], "reason": action["reason"]})
        content = self._public_payload(preview)
        cursor = conn.execute(
            "INSERT INTO public_releases(name,project_month,fingerprint,content,created_at) "
            "VALUES(?,?,?,?,?)",
            (name, month, preview["fingerprint"],
             json.dumps(content, ensure_ascii=False, sort_keys=True), now()))
        release_id = cursor.lastrowid
        # 回填台账的首发版本指针
        for pair, entry in known.items():
            if entry.get("first_release_id") is None:
                conn.execute("UPDATE suppression_ledger SET first_release_id=? WHERE id=?",
                             (release_id, entry["id"]))
        self.store.audit(conn, "public_release_published", name,
                         {"id": release_id, "fingerprint": preview["fingerprint"],
                          "suppressed_cells": len(preview["suppressed_rows"])})
        return release_id

    @staticmethod
    def _public_payload(preview: dict) -> dict:
        # 剥离 ledger_actions 等内部编排字段；载荷中绝不出现 fact_id / 精确抑制重量
        return {"month": preview["month"], "policy": preview["policy"],
                "community_rows": preview["community_rows"],
                "suppressed_rows": preview["suppressed_rows"],
                "street_rows": preview["street_rows"],
                "region_rows": preview["region_rows"],
                "unassigned_deposit_events": preview["unassigned_deposit_events"],
                "fingerprint": preview["fingerprint"]}

    def get(self, identifier: int | str) -> dict:
        return self.store.public_release(identifier)

    def listing(self, month: str) -> dict:
        calendar_day(month + "-01", "month")
        with self.store.connect() as conn:
            rows = self.store.all(conn,
                                  "SELECT id,name,fingerprint,created_at FROM public_releases "
                                  "WHERE project_month=? ORDER BY id", (month,))
        return {"month": month, "releases": rows}

    # ------------------------------------------------------------------ #
    # 公开查询：只从冻结的公开版本读取，永远不直接碰内部事实
    # ------------------------------------------------------------------ #
    def public_community(self, release_ref: int | str, community_id: str) -> dict:
        community_id = key(community_id, "community_id")
        release = self.get(release_ref)
        content = release["content"]
        visible = [row for row in content["community_rows"]
                   if row["community_id"] == community_id]
        suppressed = [row for row in content["suppressed_rows"]
                      if row["community_id"] == community_id]
        return {"public_release_id": release["id"], "month": content["month"],
                "community_id": community_id, "rows": visible, "suppressed": suppressed}

    def public_street(self, release_ref: int | str, street_id: str) -> dict:
        street_id = key(street_id, "street_id")
        release = self.get(release_ref)
        content = release["content"]
        return {"public_release_id": release["id"], "month": content["month"],
                "street_id": street_id,
                "rows": [row for row in content["street_rows"]
                         if row["street_id"] == street_id]}

    def public_region(self, release_ref: int | str) -> dict:
        release = self.get(release_ref)
        content = release["content"]
        return {"public_release_id": release["id"], "month": content["month"],
                "rows": content["region_rows"]}

    def public_material_slice(self, release_ref: int | str, material_id: str) -> dict:
        material_id = key(material_id, "material_id")
        release = self.get(release_ref)
        content = release["content"]
        return {
            "public_release_id": release["id"], "month": content["month"],
            "material_id": material_id,
            "community_rows": [row for row in content["community_rows"]
                               if row["material_id"] == material_id],
            "suppressed_rows": [row for row in content["suppressed_rows"]
                                if row["material_id"] == material_id],
            "street_rows": [row for row in content["street_rows"]
                            if row["material_id"] == material_id],
            "region": next((row for row in content["region_rows"]
                            if row["material_id"] == material_id), None)}


class DisclosureAudit:
    """内部审核入口：可追溯被保护单元格对应的原始事实，但需审计角色。"""

    # 演示用角色令牌；真实部署应由身份层签发并绑定权限
    AUDITOR_TOKEN = "auditor-token"

    def __init__(self, store: Store, view: PublicView):
        self.store, self.view = store, view

    def require_auditor(self, role: str | None):
        if role != self.AUDITOR_TOKEN:
            raise LakeError("forbidden", "auditor role required for protected lineage", 403)

    def protected_cells(self, month: str, role: str | None = None) -> dict:
        self.require_auditor(role)
        calendar_day(month + "-01", "month")
        cells, _ = self.view._cells(month)
        preview = self.view.build(month)
        protected = []
        for row in preview["suppressed_rows"]:
            triple = (row["street_id"], row["community_id"], row["material_id"])
            if triple not in cells:
                # 零投递月份的休眠占位：无事实可追溯，只回传公开抑制原因
                protected.append({"month": month, "street_id": row["street_id"],
                                  "community_id": row["community_id"],
                                  "material_id": row["material_id"],
                                  "public_reason": row["reason"], "internal": None})
                continue
            cell = cells[triple]
            protected.append({
                "month": month, "street_id": row["street_id"],
                "community_id": row["community_id"], "material_id": row["material_id"],
                "public_reason": row["reason"],
                "internal": {"exact_deposit_kg": format(cell["kg"], "f"),
                             "contributor_count": len(cell["by_actor"]),
                             "deposit_events": cell["events"],
                             "fact_ids": sorted(cell["fact_ids"])}})
        return {"month": month, "cells": protected}

    def ledger(self, role: str | None = None, month: str | None = None) -> dict:
        self.require_auditor(role)
        entries = self.store.suppression_entries(month)
        return {"entries": entries}

    def cell_lineage(self, month: str, community_id: str, material_id: str,
                     role: str | None = None) -> dict:
        self.require_auditor(role)
        community_id = key(community_id, "community_id")
        material_id = key(material_id, "material_id")
        calendar_day(month + "-01", "month")
        cells, _ = self.view._cells(month)
        match = [(key_tuple, cell) for key_tuple, cell in cells.items()
                 if key_tuple[1] == community_id and key_tuple[2] == material_id]
        if not match:
            raise LakeError("unknown_protected_cell", "cell has no ready facts in month", 404)
        (street_id, _, _), cell = match[0]
        traces = [self.view.reconciliation.ledger.trace(fact_id)
                  for fact_id in sorted(cell["fact_ids"])]
        return {"month": month, "street_id": street_id, "community_id": community_id,
                "material_id": material_id,
                "exact_deposit_kg": format(cell["kg"], "f"),
                "contributor_count": len(cell["by_actor"]),
                "traces": traces}
