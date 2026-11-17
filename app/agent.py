"""两段式问答：先让大模型解析意图与条件，再路由到工单库 / 知识库工具。"""
import json
import re
import time
from datetime import datetime

import httpx

from . import config, db, retrieval

INTENT_LABELS = {
    "troubleshoot": "故障排查",
    "ticket_query": "工单查询",
    "ticket_stats": "故障统计",
    "deploy": "部署咨询",
    "kb_search": "知识库检索",
    "smalltalk": "闲聊",
}

PLAN_PROMPT = """你是设备故障问答系统的意图解析器。今天是 {today}。
请把用户问题解析成 JSON，只输出 JSON，不要解释。字段：
{{
  "intent": "troubleshoot|ticket_query|ticket_stats|deploy|kb_search|smalltalk",
  "rewritten_query": "用于知识库检索的关键词，保留设备型号、故障码、现象，去掉口语词",
  "keywords": ["故障现象关键词数组，保留部件名与现象词，如 刷卡 读卡失败；没有则空数组"],
  "date": "YYYY-MM-DD，用户明确提到某天时填写，否则空字符串",
  "year": "四位年份如 2025，只提到年份时填写，否则空字符串",
  "ticket_no": "工单号如 PS-2025-0506，没有则空",
  "fault_code": "故障码如 E05，没有则空",
  "customer": "客户名称，没有则空"
}}
判断规则：
- 问某天/某段时间有哪些工单、哪台设备出过故障 → ticket_query
- 问某故障码或某故障现象出现几次、统计数量 → ticket_stats，并把现象词放入 keywords
- 问怎么排查、怎么修、报错原因 → troubleshoot
- 问安装、部署、网络、端口、验收 → deploy
- 只是寒暄 → smalltalk
- 其他知识型问题 → kb_search
日期要归一化：2025.5.7 / 2025年5月7日 / 2025/5/7 都转成 2025-05-07；只说 5月7日 时可填写当年 5 月 7 日。"""

_parse_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600


def rule_slots(question: str) -> dict:
    text = question or ""
    dates = retrieval.extract_dates(text)
    month_day = ""
    if not dates:
        match = retrieval.MONTH_DAY_RE.search(text)
        if match:
            month_day = f"{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    codes = [f"E{c}" for c in re.findall(r"\bE\s?(\d{2})\b", text, re.I)]
    ticket_nos = [re.sub(r"\s+", "", m).upper() for m in retrieval.TICKET_RE.findall(text)]
    # 现象关键词：去掉故障码形式（故障码走独立槽位），保留部件/现象词
    kw = [t for t in retrieval.key_terms(text) if not re.fullmatch(r"(?i)e\d{2}", t)]
    return {
        "date": dates[0] if dates else "",
        "month_day": month_day,
        "year": retrieval.extract_year(text),
        "fault_code": codes[0] if codes else "",
        "ticket_no": ticket_nos[0] if ticket_nos else "",
        "customer": "",
        "keywords": kw[:4],
    }


def rule_intent(question: str, slots: dict) -> str:
    text = question or ""
    if re.search(r"(几次|多少次|统计|数量|一共|频次|占比)", text) and (
            slots["fault_code"] or slots["keywords"]):
        return "ticket_stats"
    if slots["date"] or slots["month_day"] or slots["ticket_no"]:
        if re.search(r"(工单|记录|故障|设备|查|哪些|哪台|谁|出过)", text):
            return "ticket_query"
    if re.search(r"(工单|报障|历史记录)", text) and (slots["fault_code"] or slots["customer"]):
        return "ticket_query"
    if re.search(r"(部署|安装|实施|上线|端口|网络|配置|验收)", text):
        return "deploy"
    if re.search(r"(你好|您好|谢谢|在吗|测试)", text.strip()):
        return "smalltalk"
    return "kb_search"


def _llm_parse(question: str) -> dict | None:
    if not config.DEEPSEEK_API_KEY:
        return None
    cached = _parse_cache.get(question)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": PLAN_PROMPT.format(today=datetime.now().strftime("%Y-%m-%d"))},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        _parse_cache[question] = (time.time(), parsed)
        return parsed
    except Exception:  # noqa: BLE001  解析失败不影响主流程
        return None


def _merge(rule: dict, parsed: dict) -> dict:
    slots = dict(rule)

    # 年份与现象关键词：两条分支都需要，先统一处理
    if parsed.get("year") and re.fullmatch(r"20\d{2}", str(parsed["year"]).strip()):
        slots["year"] = str(parsed["year"]).strip()
    llm_kw = [str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()]
    if llm_kw:
        merged_kw = llm_kw + [k for k in slots.get("keywords", []) if k not in llm_kw]
        slots["keywords"] = merged_kw[:5]

    # 用户没写年份（如“5月7日”）时只用月日匹配，避免被解析成今年而查不到历史工单
    if slots.get("month_day") and not slots.get("date"):
        if parsed.get("date") and retrieval.extract_dates(str(parsed["date"])):
            slots["month_day"] = retrieval.extract_dates(str(parsed["date"]))[0][5:]
        if parsed.get("ticket_no"):
            slots["ticket_no"] = str(parsed["ticket_no"]).upper().strip()
        if parsed.get("fault_code"):
            slots["fault_code"] = str(parsed["fault_code"]).upper().strip()
        return slots
    if parsed.get("date") and retrieval.extract_dates(parsed["date"]):
        slots["date"] = retrieval.extract_dates(parsed["date"])[0]
    if parsed.get("ticket_no"):
        slots["ticket_no"] = str(parsed["ticket_no"]).upper().strip()
    if parsed.get("fault_code"):
        slots["fault_code"] = str(parsed["fault_code"]).upper().strip()
    if parsed.get("customer"):
        slots["customer"] = str(parsed["customer"]).strip()
    if slots["date"]:
        slots["month_day"] = ""
    return slots


def _ticket_content(t: dict) -> str:
    lines = [
        f"工单号：{t.get('ticket_no','')}",
        f"报障日期：{t.get('report_date','')}",
        f"客户：{t.get('customer','')}",
        f"场景：{t.get('scene','')}",
        f"设备型号：{t.get('model','')}",
        f"故障现象：{t.get('symptom','')}",
        f"故障码：{t.get('fault_code','')}",
        f"根因：{t.get('root_cause','')}",
        f"解决方案：{t.get('solution','')}",
        f"处理时长：{t.get('hours','')}",
        f"状态：{t.get('status','')}",
    ]
    return "\n".join(line for line in lines if line.split("：", 1)[-1].strip())


def _ticket_hit(t: dict) -> dict:
    locator = {"rows": str(t.get("row_no", ""))}
    if t.get("sheet"):
        locator["sheet"] = t["sheet"]
    return {
        "chunk_id": f"ticket-{t['id']}",
        "doc_id": t["doc_id"],
        "filename": t["filename"],
        "ext": t.get("ext", ""),
        "locator": locator,
        "content": _ticket_content(t),
        "score": 1.0,
    }


def _slots_text(slots: dict, rows: int) -> str:
    parts = []
    if slots.get("date"):
        parts.append(f"日期 {slots['date']}")
    elif slots.get("month_day"):
        parts.append(f"日期 {slots['month_day']}")
    elif slots.get("year"):
        parts.append(f"年份 {slots['year']}")
    if slots.get("ticket_no"):
        parts.append(f"工单号 {slots['ticket_no']}")
    if slots.get("fault_code"):
        parts.append(f"故障码 {slots['fault_code']}")
    if slots.get("customer"):
        parts.append(f"客户 {slots['customer']}")
    has_structured = bool(slots.get("date") or slots.get("month_day") or slots.get("ticket_no")
                          or slots.get("fault_code") or slots.get("customer"))
    if slots.get("keywords") and not has_structured:
        parts.append("关键词 " + "/".join(slots["keywords"][:3]))
    if rows:
        parts.append(f"命中工单 {rows} 条")
    return " · ".join(parts) if parts else "无结构化条件"


def make_plan(question: str, top_k: int = 5) -> dict:
    slots = rule_slots(question)
    parsed = _llm_parse(question)
    parsed_by = "llm" if parsed else "rule"
    if parsed:
        slots = _merge(slots, parsed)

    intent = (parsed or {}).get("intent") or rule_intent(question, slots)
    if intent not in INTENT_LABELS:
        intent = rule_intent(question, slots)
    rewritten = (parsed or {}).get("rewritten_query") or question

    structured = bool(slots["date"] or slots["month_day"] or slots["ticket_no"]
                      or slots["fault_code"] or slots["customer"])
    has_filter = structured or bool(slots["year"] or slots["keywords"])
    need_ticket = intent in ("ticket_query", "ticket_stats") and has_filter
    rows: list[dict] = []
    note_parts: list[str] = []

    if need_ticket:
        if structured:
            rows = db.query_tickets(
                date=slots["date"],
                month_day=slots["month_day"],
                ticket_no=slots["ticket_no"],
                fault_code=slots["fault_code"],
                customer=slots["customer"],
                year=slots["year"],
            )
        else:
            # 无结构化槽位：按部件现象词组检索（组内 OR、组间 AND），防止泛词扩散
            groups = retrieval.ticket_keyword_groups(question)
            _, rows = db.ticket_keyword_stats(groups, year=slots["year"])

        if intent == "ticket_stats":
            if slots["fault_code"]:
                count, stats_rows = db.ticket_code_stats(slots["fault_code"])
                note_parts.append(f"工单库统计：{slots['fault_code']} 共 {count} 条记录。")
                if stats_rows:
                    latest = stats_rows[0]
                    note_parts.append(
                        f"最近一条为 {latest['report_date']}（{latest['ticket_no']}，{latest['customer']}）。"
                    )
            else:
                year_hint = f"{slots['year']} 年" if slots["year"] else ""
                note_parts.append(f"工单库按现象关键词统计：{year_hint}共命中 {len(rows)} 条记录。")
        if not rows and (slots["date"] or slots["month_day"] or slots["year"]):
            start, end, total = db.ticket_coverage()
            note_parts.append(
                f"工单库中未找到条件匹配的工单；当前工单库共 {total} 条，覆盖 {start} 至 {end}。"
            )
        if len(rows) > 6:
            note_parts.append(f"共命中 {len(rows)} 条，以下只列出最近 6 条。")
            rows = rows[:6]

    ticket_hits = [_ticket_hit(t) for t in rows]

    need_kb = intent != "smalltalk"
    kb_hits: list[dict] = []
    if need_kb:
        query = rewritten
        if rows and slots["fault_code"]:
            query = f"{slots['fault_code']} 故障 原因 排查 处理"
        kb_hits = retrieval.kb.search(query, top_k)
        if rows:
            kb_hits = [
                h for h in kb_hits
                if not (str(h.get("filename", "")).lower().endswith((".xlsx", ".csv"))
                        and "历史工单" in str(h.get("locator", {}).get("sheet", "")))
            ]
        max_kb = max(0, top_k - len(ticket_hits))
        kb_hits = kb_hits[:max_kb]

    hits = ticket_hits + kb_hits
    tools = []
    if need_ticket:
        tools.append("query_tickets")
    if need_kb:
        tools.append("search_kb")

    return {
        "intent": intent,
        "intent_label": INTENT_LABELS.get(intent, "知识库检索"),
        "slots": slots,
        "slots_text": _slots_text(slots, len(rows)),
        "tools": tools,
        "parsed_by": parsed_by,
        "ticket_count": len(rows),
        "hits": hits,
        "note": "\n".join(note_parts),
        "rewritten": rewritten,
    }


def public_plan(plan: dict) -> dict:
    return {
        "intent": plan["intent"],
        "intent_label": plan["intent_label"],
        "slots_text": plan["slots_text"],
        "tools": plan["tools"],
        "parsed_by": plan["parsed_by"],
        "ticket_count": plan["ticket_count"],
        "rewritten": plan["rewritten"],
    }
