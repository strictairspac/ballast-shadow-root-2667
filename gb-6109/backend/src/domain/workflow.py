"""配方换号与接单重演编排。

在 :class:`~src.domain.pager.ShadowPager` 的不可变页池之上叠加一层
:class:`~src.domain.ledger.CodeLedger` 编号归属，实现四类业务动作：

- ``renumber``   换号：先让新号与旧号同页并存（生效根只多一个编号、不增页），
                  再停用旧号，归属整体切换；
- ``intake``     接单：按接单瞬间的登记簿解析编号并快照归属，同一批次里
                  同号不同配比直接整批退回，页池与登记簿原样不动；
- ``apply_order`` 执行已接订单：按接单快照改配方，重跑不会改换归属；
- ``revise`` / ``release_baseline`` 沿用影子页池原有断电与解封语义。

页面（物理页）内容只能由 ``revise`` / ``apply_order`` 的写时复制产生，
换号类操作永远不分配新页。
"""

from dataclasses import dataclass, field

from .ledger import CodeLedger, LedgerError
from .pager import PageBudgetExceeded, ShadowPager
from .recipe import Recipe


class OrderRejected(ValueError):
    """接单批次被整批退回（编号停用或同号配比冲突）。"""


@dataclass
class OrderLine:
    code: str
    fine: int
    coarse: int
    page_id: int  # 接单瞬间解析到的物理页


@dataclass
class AcceptedOrder:
    order_id: str
    lines: list[OrderLine]
    binding_snapshot: dict  # 接单时全量编号归属快照
    applied: bool = False
    root_at_accept: int = 0
    applied_root: int | None = None


@dataclass
class OrderWorkflow:
    pager: ShadowPager
    ledger: CodeLedger
    orders: dict[str, AcceptedOrder] = field(default_factory=dict)
    rejected: int = 0

    @classmethod
    def build(cls, initial, budget, keep_baseline):
        pager = ShadowPager(initial, budget, keep_baseline)
        ledger = CodeLedger.from_pointers(dict(pager.root.pointers))
        return cls(pager, ledger)

    # -- 内部工具 -------------------------------------------------------

    def _sync_aliases(self, previous_page, new_page):
        """页面写时复制后，把原页上的全部有效编号迁到新页，并留下归属历史。"""
        aliases = self.ledger.effective_codes(previous_page)
        for code in aliases:
            self.ledger.bind(code, new_page)
        return aliases

    def _sync_root_to_ledger(self):
        """按登记簿重建生效根指针（编号 -> 物理页），同一物理页只占一个条目。

        一页若有多个有效编号（新旧号共存窗口），取字典序最小的编号作为该页
        的根键，其余别名仍完整保留在登记簿中。当前根只包含仍有有效编号的
        物理页；封存根（backup）由页池单独保留，被它引用的旧页不会被回收。
        """
        page_to_code = {}
        for code, page_id in sorted(self.ledger.bindings.items()):
            page_to_code.setdefault(page_id, code)
        pointers = {code: page_id for page_id, code in page_to_code.items()}
        self.pager.root = type(self.pager.root)(
            self.ledger.root_number, self._freeze(pointers)
        )

    @staticmethod
    def _freeze(pointers):
        from types import MappingProxyType
        return MappingProxyType(pointers)

    def _publish_ledger_root(self, history_mark=None):
        self.ledger.root_number = self.pager.root.number
        if history_mark is not None:
            self.ledger.stamp_since(history_mark, self.pager.root.number)
        self._sync_root_to_ledger()

    # -- 业务动作 -------------------------------------------------------

    def renumber(self, pairs):
        """换号：新旧号先同页并存，再停用旧号；不产生新物理页。"""
        pages_before = set(self.pager.pages)
        mark = self.ledger.snapshot()
        try:
            # 1) 新号与旧号指向同一物理页（停用前的共存窗口）。
            for old, new in pairs:
                page_id = self.ledger.resolve(old)
                if not self.ledger.is_active(new):
                    self.ledger.bind(new, page_id)
            # 2) 整体停用旧号；任一步非法都回滚，归属保持原样。
            for old, _new in pairs:
                if self.ledger.is_active(old):
                    self.ledger.retire(old)
        except LedgerError:
            self.ledger.rollback(mark)
            raise
        # 3) 归属切换本身也是一次原子根发布，但页池一页未增。
        assert set(self.pager.pages) == pages_before, "换号不得分配物理页"
        self._republish_root(mark)
        released = self.pager.reclaim()
        return released

    def _republish_root(self, history_mark=None):
        """归属变更（不涉及页内容）触发一次原子根发布。"""
        self.pager.root = type(self.pager.root)(
            self.pager.root.number + 1, self.pager.root.pointers
        )
        self.ledger.root_number = self.pager.root.number
        if history_mark is not None:
            self.ledger.stamp_since(history_mark, self.pager.root.number)
        self._sync_root_to_ledger()

    def reassign(self, code, target_code):
        """把已停用编号改派给另一张有效配方所在的物理页。"""
        if self.ledger.is_active(code):
            raise LedgerError(f"编号 {code} 仍有效，须先停用才能改派")
        target_page = self.ledger.resolve(target_code)
        mark = self.ledger.snapshot()
        self.ledger.bind(code, target_page)
        self._republish_root(mark)
        return target_page

    def release_baseline(self):
        """解封基准：回收孤儿页，并把根指针重建到当前仍存活的物理页。

        编号在写时复制时已随别名迁移到新页，登记簿指向的是存活页；被回收的
        只是封存根保护的基准旧页，因此直接按登记簿重建根，不会产生悬空指针。
        """
        released = self.pager.release()
        live_pages = set(self.pager.pages)
        # 防御性校验：登记簿不允许指向已回收的物理页。
        for code, page_id in self.ledger.bindings.items():
            if page_id not in live_pages:
                raise RuntimeError(f"编号 {code} 指向已回收物理页 {page_id}")
        self.ledger.root_number = self.pager.root.number
        self._sync_root_to_ledger()
        return released

    def intake(self, order_id, rows):
        """接单：解析编号并快照归属；冲突则整批退回，页池原样。

        与资料校验不同，接单允许同一编号出现多行——这正是「同一步用两号
        填了不同配比」的检测点，配比不一致即整批退回。
        """
        if order_id in self.orders:
            raise OrderRejected(f"订单 {order_id} 已存在，不能重复接单")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise OrderRejected(f"订单 {order_id} 需包含 1 至 32 行签发内容")
        lines = []
        seen: dict[str, Recipe] = {}
        mark = self.ledger.snapshot()
        pages_before = set(self.pager.pages)
        try:
            for row in rows:
                recipe = row if isinstance(row, Recipe) else Recipe.from_row(row)
                if recipe.code in seen and seen[recipe.code] != recipe:
                    raise OrderRejected(
                        f"订单 {order_id} 中编号 {recipe.code} 填写了不同配比，整批退回"
                    )
                page_id = self.ledger.resolve(recipe.code)  # 停用/未登记编号在此拒绝
                seen[recipe.code] = recipe
                lines.append(OrderLine(recipe.code, recipe.fine, recipe.coarse, page_id))
        except (LedgerError, OrderRejected) as exc:
            self.ledger.rollback(mark)
            assert set(self.pager.pages) == pages_before, "退单不得改动页池"
            self.rejected += 1
            if not isinstance(exc, OrderRejected):
                raise OrderRejected(str(exc)) from exc
            raise
        order = AcceptedOrder(
            order_id=order_id, lines=lines,
            binding_snapshot=dict(self.ledger.bindings),
            root_at_accept=self.ledger.root_number,
        )
        self.orders[order_id] = order
        return order

    def apply_order(self, order_id):
        """按接单快照执行订单：重跑不改归属，已执行则幂等无操作。"""
        order = self.orders.get(order_id)
        if order is None:
            raise OrderRejected(f"订单 {order_id} 尚未接单，不能执行")
        if order.applied:
            return [], [], [], f"订单 {order_id} 已执行，重跑不改换归属"
        # 以接单快照为准定位物理页：即使原填编号此后停用或改派，
        # 仍改的是接单那一刻归属的那张配方页。
        changes = []
        page_map = {}
        for line in order.lines:
            target_code = line.code
            if not self.ledger.is_active(line.code):
                aliases = self.ledger.effective_codes(line.page_id)
                if not aliases:
                    raise OrderRejected(
                        f"订单 {order_id} 的编号 {line.code} 已停用且原配方页无有效编号"
                    )
                target_code = aliases[0]
            changes.append(Recipe(target_code, line.fine, line.coarse))
            page_map[target_code] = line.page_id
        assigned, recycled, released, label = self._replace_with_snapshot(
            changes, order, page_map=page_map
        )
        order.applied = True
        order.applied_root = self.pager.root.number
        return assigned, recycled, released, f"执行订单 {order_id}：{label}"

    def revise(self, changes, phase):
        """普通修订，保持原有断电三阶段语义。"""
        return self._replace_with_snapshot(changes, None, phase)

    def _replace_with_snapshot(self, changes, order, phase="published", page_map=None):
        # page_map 显式给出「当前生效编号 -> 接单时物理页」，覆盖编号已被
        # 改派/停用、当前登记簿无法反查原页的情况。
        previous_pages = page_map or {
            code: self.ledger.resolve(code)
            for code in {recipe.code for recipe in changes}
            if self.ledger.is_active(code)
        }
        history_mark = self.ledger.snapshot()
        try:
            assigned, recycled, released, label = self.pager.replace(changes, phase)
        except PageBudgetExceeded:
            raise
        # 仅正式发布才会换页；刷盘前/刷盘后断电不生效，归属保持原样。
        if phase == "published" and assigned:
            for recipe in changes:
                old_page = previous_pages.get(recipe.code)
                new_page = self.pager.root.pointers.get(recipe.code)
                if old_page and new_page and old_page != new_page:
                    self._sync_aliases(old_page, new_page)
            self._publish_ledger_root(history_mark)
        return assigned, recycled, released, label

    # -- 证据视图 -------------------------------------------------------

    def registry_rows(self):
        # 每个编号的最终状态以最后一条历史为准（停用后可能再次绑定）。
        final_kind = {}
        for entry in self.ledger.history:
            final_kind[entry.code] = entry.kind
        rows = []
        for entry in self.ledger.history:
            current_page = self.ledger.bindings.get(entry.code)
            is_latest = entry is next(
                item for item in reversed(self.ledger.history) if item.code == entry.code
            )
            if final_kind[entry.code] == "retire":
                state = "停用"
            elif is_latest and current_page == entry.bound_page_id:
                state = "有效（当前归属）"
            else:
                state = "有效（历史归属）"
            rows.append({
                "编号": entry.code,
                "动作": {"bind": "绑定/换号绑定", "retire": "停用"}[entry.kind],
                "归属物理页": entry.bound_page_id,
                "发生根次": entry.at_root,
                "当前状态": state,
            })
        return rows

    def order_rows(self):
        rows = []
        for order in self.orders.values():
            for line in order.lines:
                actual_page = self.ledger.bindings.get(line.code)
                rows.append({
                    "订单": order.order_id,
                    "原填编号": line.code,
                    "接单时归属页": line.page_id,
                    "细料基点": line.fine,
                    "粗料基点": line.coarse,
                    "接单根次": order.root_at_accept,
                    "执行根次": order.applied_root if order.applied else "未执行",
                    "编号当前归属页": actual_page if actual_page is not None else "已停用",
                    "实际改的配方": f"物理页 {line.page_id}",
                })
        return rows

    def effective_rows(self):
        rows = []
        for page_id in sorted(self.pager.pages):
            recipe = self.pager.pages[page_id]
            codes = self.ledger.effective_codes(page_id)
            rows.append({
                "物理页": page_id,
                "有效编号": "、".join(codes) or "（无有效编号）",
                "细料基点": recipe.fine,
                "粗料基点": recipe.coarse,
                "生效根": self.pager.root.number,
            })
        return rows
