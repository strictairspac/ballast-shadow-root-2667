from .fixtures import FIXTURES
from .pager import PageBudgetExceeded
from .recipe import Recipe, recipe_set
from .workflow import OrderRejected, OrderWorkflow


CONFIG = {
    "title": "岑砾道料·道砟配方影子页签发",
    "team": "岑砾道料 8 人试配与线下签发小组",
    "description": "用影子分页提交配方，对照断电前后有效根和可复用页，再安排生产签发。",
    "direction": "影子分页与写时复制发布",
    "mechanism": "不可变配方页、原子根指针切换、引用保护及孤儿页回收。",
    "modules": ["道料交班", "配方签发单", "发布重演", "可见配方根"],
    "unit": "配方签发单",
    "max_parameter": 128,
    "parameter_label": "可用物理页数（1–128）",
    "default_parameter": 5,
    "facts": ["已发布根次", "回收页次", "预算拒绝", "接单整批退回"],
    "palette": 9,
    "fixture": "BR-K7-1124 / K7 段",
}

PHASES = {"volatile": "刷盘前", "flushed": "刷盘后换根前", "published": "换根成功"}


def _pairs(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("renumber 的 pairs 须有 1 至 32 对编号")
    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("renumber 各项必须是对象")
        old, new = row.get("old_code"), row.get("new_code")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("renumber 需提供 old_code 与 new_code 字符串")
        pairs.append((old, new))
    return pairs


def normalize(payload):
    if not isinstance(payload, dict):
        raise ValueError("输入需为对象")
    initial = recipe_set(payload.get("initial"), "initial")
    keep = payload.get("keep_baseline")
    if type(keep) is not bool:
        raise ValueError("keep_baseline 须为布尔值")
    events = payload.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 64:
        raise ValueError("events 须有 1 至 64 个操作")
    normalized = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("events 各项必须是对象")
        op = event.get("op")
        if op == "release_baseline":
            normalized.append(("release_baseline", []))
        elif op == "renumber":
            normalized.append(("renumber", _pairs(event.get("pairs"))))
        elif op == "reassign":
            code, target = event.get("code"), event.get("to_code")
            if not isinstance(code, str) or not isinstance(target, str):
                raise ValueError("reassign 需提供 code 与 to_code 字符串")
            normalized.append(("reassign", (code, target)))
        elif op == "intake":
            order_id = event.get("order_id")
            if not isinstance(order_id, str) or not 1 <= len(order_id) <= 48:
                raise ValueError("intake 需提供 1 至 48 字符的 order_id")
            raw_lines = event.get("lines")
            if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 32:
                raise ValueError("intake 的 lines 须有 1 至 32 行")
            # 接单允许同号多行（冲突由工作流在接单时整批退回），此处逐行做格式校验。
            lines = [Recipe.from_row(row) for row in raw_lines]
            normalized.append(("intake", (order_id, lines)))
        elif op == "apply_order":
            order_id = event.get("order_id")
            if not isinstance(order_id, str) or not 1 <= len(order_id) <= 48:
                raise ValueError("apply_order 需提供 1 至 48 字符的 order_id")
            normalized.append(("apply_order", order_id))
        elif op == "revise":
            phase = event.get("phase")
            if not isinstance(phase, str) or phase not in PHASES:
                raise ValueError("revise 需提供 volatile、flushed 或 published 阶段")
            normalized.append((phase, recipe_set(event.get("changes"), "changes")))
        else:
            raise ValueError(
                "未知事件类型，支持 revise、renumber、reassign、intake、apply_order、release_baseline"
            )
    return initial, keep, normalized


def validate(payload):
    normalize(payload)


def page_list(page_ids):
    return "、".join(str(page_id) for page_id in page_ids) or "—"


def run(payload, parameter, db=None):
    initial, keep, events = normalize(payload)
    if parameter < len(initial):
        raise ValueError("物理页预算放不下初始配方根")
    flow = OrderWorkflow.build(initial, parameter, keep)
    pager = flow.pager
    denied = recovered = reused = 0
    rows = []
    for index, (op, detail) in enumerate(events, 1):
        assigned = recycled = released = []
        label = ""
        if op == "release_baseline":
            released = flow.release_baseline()
            label = "释放基准快照"
        elif op == "renumber":
            try:
                released = flow.renumber(detail)
                label = f"换号 {len(detail)} 项，新旧号同页后停用旧号，未新增物理页"
            except ValueError as exc:
                label = f"换号退回：{exc}"
        elif op == "reassign":
            code, target = detail
            try:
                page_id = flow.reassign(code, target)
                label = f"停用编号 {code} 改派给 {target} 所在物理页 {page_id}"
            except ValueError as exc:
                label = f"改派退回：{exc}"
        elif op == "intake":
            order_id, lines = detail
            try:
                flow.intake(order_id, lines)
                label = f"接单 {order_id}，按接单时归属快照 {len(lines)} 行"
            except OrderRejected as exc:
                label = f"整批退回：{exc}"
        elif op == "apply_order":
            try:
                assigned, recycled, released, label = flow.apply_order(detail)
            except (OrderRejected, PageBudgetExceeded) as exc:
                if isinstance(exc, PageBudgetExceeded):
                    denied += 1
                label = f"执行退回：{exc}"
        else:  # 断电三阶段的普通修订
            phase = op
            try:
                assigned, recycled, released, label = flow.revise(
                    [Recipe(item.code, item.fine, item.coarse) for item in detail], phase
                )
            except PageBudgetExceeded as exc:
                denied += 1
                label = str(exc)
        recovered += len(released)
        reused += len(recycled)
        rows.append({
            "记录": f"步骤 {index}",
            "阶段": PHASES.get(
                op,
                "编号归属" if op in ("renumber", "reassign", "intake", "apply_order")
                else "解封基准",
            ),
            "当前根": pager.root.number, "影子页": page_list(assigned),
            "回收页": page_list(released), "复用页": page_list(recycled),
            "结论": label,
        })
    rows.extend(flow.effective_rows())
    return {
        "summary": (
            f"生效根 R{pager.root.number}，回收 {recovered} 页次，"
            f"预算拒绝 {denied} 次，接单退回 {flow.rejected} 批，"
            f"占用物理页 {len(pager.pages)}。"
        ),
        "metrics": {
            "已发布根次": pager.root.number, "回收页次": recovered,
            "预算拒绝": denied, "复用页次": reused, "占用物理页": len(pager.pages),
            "接单整批退回": flow.rejected, "有效编号数": len(flow.ledger.bindings),
        },
        "rows": rows,
        "registry": flow.registry_rows(),
        "orders": flow.order_rows(),
    }
