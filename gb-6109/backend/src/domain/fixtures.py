BASE = [
    {"recipe": "BR-051", "fine_bp": 2500, "coarse_bp": 7500},
    {"recipe": "BR-052", "fine_bp": 3000, "coarse_bp": 7000},
    {"recipe": "BR-053", "fine_bp": 2800, "coarse_bp": 7200},
]
REVISION = [
    {"recipe": "BR-051", "fine_bp": 2700, "coarse_bp": 7300},
    {"recipe": "BR-052", "fine_bp": 3200, "coarse_bp": 6800},
]

FIXTURES = [
    {
        "code": "BR-K7-1124",
        "title": "K7 段配方签发重试",
        "description": "先在影子页刷盘后断电，再重试发布；比对封存根、孤儿页和释放后的页号复用。",
        "payload": {
            "initial": BASE, "keep_baseline": True,
            "events": [
                {"op": "revise", "phase": "flushed", "changes": REVISION},
                {"op": "revise", "phase": "published", "changes": REVISION},
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-053", "fine_bp": 3100, "coarse_bp": 6900}]},
                {"op": "release_baseline"},
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-053", "fine_bp": 3100, "coarse_bp": 6900}]},
            ],
        },
    },
    {
        "code": "BR-K9-1124",
        "title": "K9 段刷盘前断电",
        "description": "暂存配方未落盘时不产生孤儿页；后续正式签发与同值重试可对比。",
        "payload": {
            "initial": BASE, "keep_baseline": False,
            "events": [
                {"op": "revise", "phase": "volatile", "changes": REVISION[:1]},
                {"op": "revise", "phase": "published", "changes": REVISION[:1]},
                {"op": "revise", "phase": "published", "changes": REVISION[:1]},
            ],
        },
    },
    {
        "code": "BR-R8-1124",
        "title": "R8 段配方换号与接单重演",
        "description": "BR-051 换号 BR-101（新旧号同页、旧号停用后改派 BR-052）；含同号不同配比整批退回、接单归属快照与重跑幂等。",
        "payload": {
            "initial": BASE, "keep_baseline": True,
            "events": [
                # 换号：新旧号先指向同一物理页，再停用旧号；不新增页面
                {"op": "renumber", "pairs": [{"old_code": "BR-051", "new_code": "BR-101"}]},
                # 停用的旧号改派给另一张配方
                {"op": "reassign", "code": "BR-051", "to_code": "BR-052"},
                # 同一步同号不同配比：整批退回，页池原样
                {"op": "intake", "order_id": "SO-4001", "lines": [
                    {"recipe": "BR-101", "fine_bp": 2600, "coarse_bp": 7400},
                    {"recipe": "BR-101", "fine_bp": 2700, "coarse_bp": 7300},
                ]},
                # 合法接单：归属在接单时快照
                {"op": "intake", "order_id": "SO-4002", "lines": [
                    {"recipe": "BR-101", "fine_bp": 2600, "coarse_bp": 7400},
                ]},
                # 执行 + 重跑：重跑不改换归属，不产生新页
                {"op": "apply_order", "order_id": "SO-4002"},
                {"op": "apply_order", "order_id": "SO-4002"},
                {"op": "release_baseline"},
            ],
        },
    },
]
