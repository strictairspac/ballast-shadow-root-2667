"""编号登记簿：管理配方编号与物理页之间的归属关系。

登记簿与影子页池相互独立：
- 页池保存不可变的配方内容页（``ShadowPager.pages``）；
- 登记簿保存「编号 -> 物理页」的当前绑定，并留存完整绑定历史。

换号（renumber）只改变登记簿和根指针中的编号归属，从不分配新的物理页；
停用（retire）解除编号的当前绑定，停用后的编号可以再绑定到另一张配方。
归属判断一律以接单/换号发生那一刻的登记簿状态为准。
"""

from dataclasses import dataclass, field
from typing import Mapping


class LedgerError(ValueError):
    """编号归属操作不合法（编号不存在、已停用或目标编号冲突）。"""


@dataclass(frozen=True)
class Binding:
    """一次编号归属记录。

    ``page_id`` 为 ``None`` 表示该编号当前已停用；历史条目里始终保留
    发生停用时它曾指向的物理页，便于根页面追溯「原填编号」。
    """

    code: str
    page_id: int | None
    bound_page_id: int | None
    kind: str  # bind（初始/换号绑定）、retire（停用）
    at_root: int


@dataclass
class CodeLedger:
    """随演练推进的编号归属登记簿。"""

    bindings: dict[str, int] = field(default_factory=dict)
    history: list[Binding] = field(default_factory=list)
    root_number: int = 0

    @classmethod
    def from_pointers(cls, pointers: Mapping[str, int], root_number: int = 0):
        ledger = cls(root_number=root_number)
        for code, page_id in pointers.items():
            ledger.bind(code, page_id, at_root=root_number)
        return ledger

    def bind(self, code, page_id, at_root=None):
        root = self.root_number if at_root is None else at_root
        self.bindings[code] = page_id
        self.history.append(Binding(code, page_id, page_id, "bind", root))

    def retire(self, code, at_root=None):
        if code not in self.bindings:
            raise LedgerError(f"编号 {code} 已停用或从未登记，不能再次停用")
        root = self.root_number if at_root is None else at_root
        previous = self.bindings.pop(code)
        self.history.append(Binding(code, None, previous, "retire", root))
        return previous

    def resolve(self, code):
        """按当前登记簿解析编号，未找到（含已停用）时抛出 ``LedgerError``。"""
        page_id = self.bindings.get(code)
        if page_id is None:
            raise LedgerError(f"编号 {code} 已停用或未登记，无法接单")
        return page_id

    def is_active(self, code):
        return code in self.bindings

    def renumber(self, pairs, at_root=None):
        """把若干旧编号改绑到当前物理页（换号不换配方内容）。

        ``pairs`` 为 ``(旧编号, 新编号)`` 序列。整体校验、整体生效：
        任一旧编号不存在、新编号仍是其他配方的有效编号，都拒绝整组换号，
        登记簿保持原样。停用编号允许作为新编号重新分配给当前物理页。
        """
        if not pairs:
            raise LedgerError("换号操作至少包含一对编号")
        target_codes = [new for _, new in pairs]
        if len(set(target_codes)) != len(target_codes):
            raise LedgerError("同一步换号的新编号不能重复")
        planned = {}
        for old, new in pairs:
            if old not in self.bindings:
                raise LedgerError(f"旧编号 {old} 已停用或未登记，不能换号")
            if new in self.bindings and self.bindings[new] != self.bindings[old]:
                raise LedgerError(f"新编号 {new} 仍是其他配方的有效编号")
            planned[old] = (new, self.bindings[old])
        root = self.root_number if at_root is None else at_root
        for old, (new, page_id) in planned.items():
            # 先解除旧编号归属，再以新编号绑定同一物理页；不产生新页面。
            del self.bindings[old]
            self.history.append(Binding(old, None, page_id, "retire", root))
            self.bindings[new] = page_id
            self.history.append(Binding(new, page_id, page_id, "bind", root))
        return {new: page_id for new, page_id in planned.values()}

    def rollback(self, mark):
        """回滚到 ``snapshot()`` 留下的标记，丢弃未生效的归属改动。"""
        self.bindings = dict(mark["bindings"])
        del self.history[mark["history_len"]:]
        self.root_number = mark["root_number"]

    def stamp_since(self, mark, root_number):
        """把 ``mark`` 之后新增的历史条目统一记到指定根次。"""
        for index in range(mark["history_len"], len(self.history)):
            entry = self.history[index]
            self.history[index] = Binding(
                entry.code, entry.page_id, entry.bound_page_id,
                entry.kind, root_number,
            )
        self.root_number = root_number

    def snapshot(self):
        return {
            "bindings": dict(self.bindings),
            "history_len": len(self.history),
            "root_number": self.root_number,
        }

    def effective_codes(self, page_id):
        """当前指向某张物理页的全部有效编号（一页可被新旧两号同时指向）。"""
        return sorted(code for code, bound in self.bindings.items() if bound == page_id)

    def trail(self, code):
        """某编号的完整归属轨迹，按发生顺序返回。"""
        return [entry for entry in self.history if entry.code == code]
