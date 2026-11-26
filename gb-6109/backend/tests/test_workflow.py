"""换号与接单重演的端到端场景测试。

运行：PYTHONPATH=backend python3 -m unittest -v tests.test_workflow
"""

import unittest

from src.domain import scenario
from src.domain.recipe import Recipe
from src.domain.workflow import OrderRejected, OrderWorkflow


BASE = [
    {"recipe": "BR-051", "fine_bp": 2500, "coarse_bp": 7500},
    {"recipe": "BR-052", "fine_bp": 3000, "coarse_bp": 7000},
    {"recipe": "BR-053", "fine_bp": 2800, "coarse_bp": 7200},
]


def build(keep=True, budget=6):
    from src.domain.recipe import recipe_set
    return OrderWorkflow.build(recipe_set(BASE, "initial"), budget, keep)


class RenumberWorkflowTest(unittest.TestCase):
    def test_renumber_alias_window_keeps_single_page(self):
        """停用前新旧两号指向同一配方，生效根只留一页。"""
        flow = build()
        pages_before = set(flow.pager.pages)
        released = flow.renumber([("BR-051", "BR-101")])
        # 没有分配新物理页，也没有可回收的孤儿页（两号同页）
        self.assertEqual(set(flow.pager.pages), pages_before)
        self.assertEqual(released, [])
        self.assertEqual(flow.ledger.resolve("BR-101"), 1)
        self.assertFalse(flow.ledger.is_active("BR-051"))
        # 生效根里页 1 只出现一次
        root_pages = list(flow.pager.root.pointers.values())
        self.assertEqual(len(root_pages), len(set(root_pages)))
        self.assertEqual(flow.pager.root.number, 1)

    def test_retired_code_reassigned_to_other_recipe(self):
        """停用的旧号可以分给另一张配方。"""
        flow = build()
        flow.renumber([("BR-051", "BR-101")])
        page_id = flow.reassign("BR-051", "BR-052")  # 旧号改派给 BR-052 所在页
        self.assertEqual(page_id, 2)
        self.assertEqual(flow.ledger.resolve("BR-051"), 2)
        self.assertEqual(flow.ledger.effective_codes(2), ["BR-051", "BR-052"])

    def test_intake_conflict_whole_batch_rejected_page_pool_intact(self):
        """同一步用两号填了不同配比，整批退回，页池原样。"""
        flow = build()
        pages_before = set(flow.pager.pages)
        bindings_before = dict(flow.ledger.bindings)
        lines = [
            {"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400},
            {"recipe": "BR-051", "fine_bp": 2700, "coarse_bp": 7300},
        ]
        with self.assertRaises(OrderRejected):
            flow.intake("SO-1", lines)
        self.assertEqual(set(flow.pager.pages), pages_before)
        self.assertEqual(flow.ledger.bindings, bindings_before)
        self.assertEqual(flow.rejected, 1)
        self.assertNotIn("SO-1", flow.orders)

    def test_intake_retired_code_rejected_and_pool_intact(self):
        """用停用编号接单同样整批退回，页池原样。"""
        flow = build()
        flow.renumber([("BR-051", "BR-101")])
        pages_before = set(flow.pager.pages)
        with self.assertRaises(OrderRejected):
            flow.intake("SO-2", [{"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400}])
        self.assertEqual(set(flow.pager.pages), pages_before)

    def test_ownership_at_order_time_wins_over_later_change(self):
        """编号归属变更与接单并发时，以接单时的归属为准。"""
        flow = build()
        flow.intake("SO-3", [{"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400}])
        order = flow.orders["SO-3"]
        self.assertEqual(order.binding_snapshot["BR-051"], 1)
        # 接单之后、执行之前，旧号停用并改派给页 2（另一张配方）
        flow.renumber([("BR-051", "BR-101")])
        flow.reassign("BR-051", "BR-052")
        self.assertEqual(flow.ledger.resolve("BR-051"), 2)
        # 执行订单仍改的是接单时归属的页 1（经现存别名 BR-101 定位，写时复制到新页）
        flow.apply_order("SO-3")
        self.assertEqual(order.applied_root, flow.pager.root.number)
        current_page = flow.ledger.resolve("BR-101")
        self.assertEqual(flow.pager.pages[current_page].fine, 2600)
        # 接单时的原物理页 1 保留基准内容（不可变页）
        self.assertEqual(flow.pager.pages[1].fine, 2500)
        # 页 2 配方没有被该订单改动（改派到页 2 的旧号不影响订单锚定的归属）
        self.assertEqual(flow.pager.pages[2].fine, 3000)
        # 订单留痕仍指向接单时归属页 1
        self.assertEqual(order.lines[0].page_id, 1)

    def test_rerun_accepted_order_keeps_ownership_and_is_idempotent(self):
        """重跑已接单批次不改换归属，且不会再次产生页面。"""
        flow = build()
        flow.intake("SO-4", [{"recipe": "BR-052", "fine_bp": 3300, "coarse_bp": 6700}])
        flow.apply_order("SO-4")
        pages_after_first = set(flow.pager.pages)
        assigned, recycled, released, label = flow.apply_order("SO-4")
        self.assertEqual(assigned, [])
        self.assertEqual(set(flow.pager.pages), pages_after_first)
        self.assertIn("重跑不改换归属", label)

    def test_old_code_in_republished_order_still_tracks_original_page(self):
        """重演还会收到填旧号的签发单，且留痕可见原填编号与实际配方页。"""
        flow = build()
        flow.intake("SO-5", [{"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400}])
        flow.renumber([("BR-051", "BR-101")])
        flow.apply_order("SO-5")
        order_rows = flow.order_rows()
        row = next(item for item in order_rows if item["订单"] == "SO-5")
        self.assertEqual(row["原填编号"], "BR-051")
        self.assertEqual(row["接单时归属页"], 1)
        self.assertEqual(row["实际改的配方"], "物理页 1")
        self.assertEqual(row["编号当前归属页"], "已停用")

    def test_visible_root_lists_origin_code_and_actual_recipe(self):
        """可见配方根页面能查到原填编号和实际改的是哪张配方。"""
        flow = build()
        flow.intake("SO-6", [{"recipe": "BR-053", "fine_bp": 3100, "coarse_bp": 6900}])
        flow.apply_order("SO-6")
        effective = {row["物理页"]: row for row in flow.effective_rows()}
        target_page = flow.ledger.resolve("BR-053")
        self.assertIn("BR-053", effective[target_page]["有效编号"])
        self.assertEqual(effective[target_page]["细料基点"], 3100)
        registry_codes = {row["编号"] for row in flow.registry_rows()}
        self.assertIn("BR-053", registry_codes)

    def test_repeated_orders_using_renumbered_code_still_resolve(self):
        """换号后发布重演再收到同编号（新号）签发单，可正常接单执行。"""
        flow = build()
        flow.renumber([("BR-051", "BR-101")])
        flow.intake("SO-7", [{"recipe": "BR-101", "fine_bp": 2900, "coarse_bp": 7100}])
        flow.apply_order("SO-7")
        self.assertEqual(flow.pager.pages[flow.ledger.resolve("BR-101")].fine, 2900)

    def test_no_dangling_root_pointer_after_release(self):
        """解封基准回收旧页后，根指针不得指向已回收的物理页。"""
        flow = build()
        flow.renumber([("BR-051", "BR-101")])
        flow.reassign("BR-051", "BR-052")
        flow.intake("SO-8", [{"recipe": "BR-101", "fine_bp": 2600, "coarse_bp": 7400}])
        flow.apply_order("SO-8")
        flow.release_baseline()
        root_pages = set(flow.pager.root.pointers.values())
        self.assertEqual(root_pages - set(flow.pager.pages), set())
        for page_id in root_pages:
            self.assertIn(page_id, flow.pager.pages)

    def test_alias_coexistence_window_points_to_single_page(self):
        """新旧号共存窗口：两号同页，且该页在根里只出现一次。"""
        flow = build()
        flow.ledger.bind("BR-101", 1)  # 新号先与旧号同页（停用前共存窗口）
        self.assertEqual(flow.ledger.effective_codes(1), ["BR-051", "BR-101"])
        flow._sync_root_to_ledger()
        root = flow.pager.root.pointers
        # 两号在登记簿里都指向页 1
        self.assertEqual(flow.ledger.resolve("BR-051"), 1)
        self.assertEqual(flow.ledger.resolve("BR-101"), 1)
        # 生效根里页 1 只占一个条目（生效根只留一页）
        self.assertEqual(list(root.values()).count(1), 1)

    def test_intake_same_code_same_ratio_is_one_consistent_order(self):
        """同号同配比的重复行不算冲突，可以正常接单。"""
        flow = build()
        flow.intake("SO-9", [
            {"recipe": "BR-052", "fine_bp": 3300, "coarse_bp": 6700},
            {"recipe": "BR-052", "fine_bp": 3300, "coarse_bp": 6700},
        ])
        self.assertEqual(flow.rejected, 0)
        self.assertEqual(len(flow.orders["SO-9"].lines), 2)

    def test_power_failure_phases_keep_ledger_ownership(self):
        """刷盘前/刷盘后断电都不改变编号归属，正式发布才迁移别名。"""
        flow = build()
        change = [Recipe("BR-051", 2600, 7400)]
        flow.revise(change, "volatile")
        self.assertEqual(flow.ledger.resolve("BR-051"), 1)  # 未刷盘，归属不动
        flow.revise(change, "flushed")
        self.assertEqual(flow.ledger.resolve("BR-051"), 1)  # 刷盘未换根，归属不动
        flow.revise(change, "published")
        self.assertNotEqual(flow.ledger.resolve("BR-051"), 1)  # 正式发布，别名迁新页
        self.assertEqual(flow.pager.pages[flow.ledger.resolve("BR-051")].fine, 2600)


class ScenarioIntegrationTest(unittest.TestCase):
    def test_legacy_fixtures_still_run(self):
        """旧资料结构（仅 revise/release_baseline）行为保持兼容。"""
        from src.domain.fixtures import FIXTURES
        for fixture in FIXTURES:
            result = scenario.run(fixture["payload"], scenario.CONFIG["default_parameter"])
            self.assertTrue(result["rows"])
            self.assertIn("registry", result)
            self.assertIn("orders", result)

    def test_full_scenario_via_payload(self):
        payload = {
            "initial": BASE, "keep_baseline": True,
            "events": [
                {"op": "renumber", "pairs": [{"old_code": "BR-051", "new_code": "BR-101"}]},
                {"op": "reassign", "code": "BR-051", "to_code": "BR-052"},
                {"op": "intake", "order_id": "SO-A", "lines": [
                    {"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400},
                    {"recipe": "BR-051", "fine_bp": 2550, "coarse_bp": 7450},
                ]},
                {"op": "intake", "order_id": "SO-B", "lines": [
                    {"recipe": "BR-101", "fine_bp": 2600, "coarse_bp": 7400},
                ]},
                {"op": "apply_order", "order_id": "SO-B"},
                {"op": "apply_order", "order_id": "SO-B"},
                {"op": "release_baseline"},
            ],
        }
        result = scenario.run(payload, 6)
        self.assertEqual(result["metrics"]["接单整批退回"], 1)
        # BR-101、BR-052、BR-053、BR-051（改派后）共 4 个有效编号
        self.assertEqual(result["metrics"]["有效编号数"], 4)
        orders = {row["订单"]: row for row in result["orders"]}
        self.assertEqual(orders["SO-B"]["原填编号"], "BR-101")
        self.assertEqual(orders["SO-B"]["实际改的配方"], "物理页 1")
        # 换号步骤未使用任何影子页
        renumber_step = result["rows"][0]
        self.assertEqual(renumber_step["影子页"], "—")
        self.assertIn("未新增物理页", renumber_step["结论"])

    def test_budget_exhaustion_still_counted(self):
        # 预算 4：初始 3 基准页（封存保护）+ 仅 1 个空闲页；
        # 第二次修订需要新页时无页可用，触发预算拒绝。
        payload = {
            "initial": BASE, "keep_baseline": True,
            "events": [
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-051", "fine_bp": 2600, "coarse_bp": 7400}]},
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-052", "fine_bp": 3100, "coarse_bp": 6900}]},
            ],
        }
        result = scenario.run(payload, 4)
        self.assertGreaterEqual(result["metrics"]["预算拒绝"], 1)

    def test_invalid_event_rejected(self):
        with self.assertRaises(ValueError):
            scenario.normalize({"initial": BASE, "keep_baseline": False,
                                "events": [{"op": "mystery"}]})


if __name__ == "__main__":
    unittest.main()
