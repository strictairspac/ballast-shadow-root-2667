"""编号登记簿（CodeLedger）单元测试。

运行：PYTHONPATH=backend python -m unittest -v tests.test_ledger
"""

import unittest

from src.domain.ledger import CodeLedger, LedgerError


class LedgerTest(unittest.TestCase):
    def setUp(self):
        # 初始：BR-051 -> 页1，BR-052 -> 页2，BR-053 -> 页3
        self.ledger = CodeLedger.from_pointers(
            {"BR-051": 1, "BR-052": 2, "BR-053": 3}
        )

    def test_initial_bindings_and_history(self):
        self.assertEqual(self.ledger.resolve("BR-051"), 1)
        self.assertEqual(len(self.ledger.history), 3)
        self.assertTrue(all(entry.kind == "bind" for entry in self.ledger.history))

    def test_renumber_keeps_single_page_and_aliases(self):
        # 换号前先让新旧两号并存：直接把新号绑到同一页（停用前的共存窗口）
        self.ledger.bind("BR-101", 1)
        self.assertEqual(self.ledger.effective_codes(1), ["BR-051", "BR-101"])
        # 旧号停用前，两号指向同一配方；生效根只引用这一页
        self.assertEqual(self.ledger.resolve("BR-051"), 1)
        self.assertEqual(self.ledger.resolve("BR-101"), 1)
        # 停用旧号
        self.ledger.retire("BR-051")
        self.assertFalse(self.ledger.is_active("BR-051"))
        self.assertEqual(self.ledger.resolve("BR-101"), 1)

    def test_retired_code_can_be_reassigned_to_other_recipe(self):
        self.ledger.retire("BR-051")  # 页1 的旧号停用
        # 停用旧号分给另一张配方（页2）
        self.ledger.bind("BR-051", 2)
        self.assertEqual(self.ledger.resolve("BR-051"), 2)
        self.assertEqual(
            [(entry.kind, entry.bound_page_id) for entry in self.ledger.trail("BR-051")],
            [("bind", 1), ("retire", 1), ("bind", 2)],
        )

    def test_resolve_retired_code_raises(self):
        self.ledger.retire("BR-052")
        with self.assertRaises(LedgerError):
            self.ledger.resolve("BR-052")

    def test_renumber_rejects_unknown_old_code(self):
        with self.assertRaises(LedgerError):
            self.ledger.renumber([("NOPE", "BR-900")])
        # 整批退回，登记簿原样
        self.assertEqual(set(self.ledger.bindings), {"BR-051", "BR-052", "BR-053"})

    def test_renumber_rejects_active_target_owned_by_other_page(self):
        with self.assertRaises(LedgerError):
            # BR-052 是页2 的有效编号，不能抢来给页1
            self.ledger.renumber([("BR-051", "BR-052")])
        self.assertEqual(self.ledger.resolve("BR-051"), 1)
        self.assertEqual(self.ledger.resolve("BR-052"), 2)

    def test_renumber_same_page_target_is_allowed(self):
        # 目标编号与旧编号已经同页（别名共存窗口关闭换号），允许
        self.ledger.bind("BR-101", 1)
        result = self.ledger.renumber([("BR-051", "BR-101")])
        self.assertEqual(result, {"BR-101": 1})
        self.assertFalse(self.ledger.is_active("BR-051"))
        self.assertEqual(self.ledger.resolve("BR-101"), 1)

    def test_renumber_batch_is_atomic(self):
        self.ledger.bind("BR-102", 2)
        with self.assertRaises(LedgerError):
            # 第二对非法：BR-053 想换成 BR-102，但 BR-102 属于页2
            self.ledger.renumber([("BR-051", "BR-101"), ("BR-053", "BR-102")])
        # 第一对也不能留下半成品
        self.assertTrue(self.ledger.is_active("BR-051"))
        self.assertFalse(self.ledger.is_active("BR-101"))

    def test_renumber_duplicate_targets_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.renumber([("BR-051", "BR-101"), ("BR-052", "BR-101")])

    def test_snapshot_rollback_discards_unpublished_change(self):
        mark = self.ledger.snapshot()
        self.ledger.renumber([("BR-051", "BR-101")])
        self.assertFalse(self.ledger.is_active("BR-051"))
        self.ledger.rollback(mark)
        self.assertTrue(self.ledger.is_active("BR-051"))
        self.assertFalse(self.ledger.is_active("BR-101"))
        self.assertEqual(len(self.ledger.history), mark["history_len"])

    def test_binding_at_order_time_is_stable(self):
        # 接单瞬间快照归属；之后编号改给别的配方，不影响已接订单
        snapshot_at_accept = dict(self.ledger.bindings)
        accepted_page = self.ledger.resolve("BR-051")
        self.ledger.retire("BR-051")
        self.ledger.bind("BR-051", 2)
        # 已接单保留的解析结果仍是页1，即「以接单时的归属为准」
        self.assertEqual(snapshot_at_accept["BR-051"], accepted_page)
        self.assertEqual(accepted_page, 1)
        self.assertEqual(self.ledger.resolve("BR-051"), 2)


if __name__ == "__main__":
    unittest.main()
