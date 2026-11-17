"""检索与路由验证脚本：空库时自动载入示例知识库，再逐题打印意图、命中与统计说明。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, db, main, retrieval  # noqa: E402

QUESTIONS = [
    "查一下故障现象为刷卡不识别 这种情况在2025年出现过几次",
    "刷卡不识别 2025年出现过几次",
    "工单号为PS-2025-0215这个工单的信息",
    "E08 刷卡无反应 怎么排查",
    "2025.5.7 有哪些设备出故障",
    "结算台称重漂移怎么校准",
    "打印机不出票怎么办",
    "设备离线掉线怎么处理",
]

db.init_db()
if not db.list_documents():
    main.seed_samples()
retrieval.kb.reload()
print("mode:", retrieval.kb.retrieval_mode(), "chunks:", retrieval.kb.size,
      "tickets:", len(db.query_tickets(limit=100)))

for q in QUESTIONS:
    plan = agent.make_plan(q, 5)
    print("\nQ:", q)
    print("  ", plan["intent_label"], "|", plan["slots_text"], "| tools:", plan["tools"])
    if plan["note"]:
        print("  note:", plan["note"])
    for h in plan["hits"]:
        print(f"  {h['score']:.3f}  {h['filename']}  {retrieval.locator_text(h['locator'])}  {h['content'][:55]!r}")
