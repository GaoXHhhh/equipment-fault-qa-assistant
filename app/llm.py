"""DeepSeek 调用（OpenAI 兼容）+ 无 Key 时的离线演示回答。"""
import asyncio
import re
from typing import AsyncGenerator

import httpx

from . import config, retrieval

SYSTEM_PROMPT = """你是「朴食科技 PS-AI200 智能AI结算台」的设备故障排查助手，服务对象是实施工程师，以及银行、校园客户的一线运维人员。

回答规则：
1. 只依据【参考资料】回答，不编造设备型号、参数、菜单路径、接口地址。
2. 结构：先用一句话给最可能的原因，再给编号的排查步骤（每步是可执行动作），最后说明何时需要升级到二线/返厂。
3. 关键结论后用 [1]、[2] 标注来源编号，编号必须与参考资料一致；不要标注不存在的编号。
4. 若资料中没有直接依据，要明确写“资料中未找到直接依据”，再给通用排查建议，并提示联系技术支持。
5. 涉及银行客户时，提示遵循内网部署、数据不出内网的合规要求。
6. 用户按日期或工单号查询时，把检索到的工单逐条列出（工单号、客户、故障码、根因、解决方案）；若资料中确实没有该日期，要说明资料覆盖的时间范围，不要编造。
7. 中文回答，简洁专业，不说客套话，不要重复问题。"""

GREETING = re.compile(r"^(你好|您好|hi|hello|在吗|有人吗|测试|test)[!！。.？?]*$", re.I)


def build_context(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        loc = retrieval.locator_text(h["locator"])
        blocks.append(
            f"[{i}] 《{h['filename']}》 · {loc} · 相关度 {h['score']:.2f}\n{h['content']}"
        )
    return "\n\n".join(blocks)


def messages_for(question: str, hits: list[dict], history: list[dict] | None, note: str = "") -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in (history or [])[-6:]:
        role = "user" if item.get("role") == "user" else "assistant"
        content = (item.get("content") or "").strip()
        if content:
            msgs.append({"role": role, "content": content[:2000]})
    if hits:
        user_content = (
            "【参考资料】\n" + build_context(hits) +
            "\n\n【用户问题】\n" + question +
            "\n\n请严格依据参考资料作答，并用 [编号] 标注引用。"
        )
    else:
        user_content = (
            "【参考资料】\n（本次未检索到任何相关资料）\n\n【用户问题】\n" + question +
            "\n\n请说明知识库中未检索到直接依据，不要编造具体参数，可给通用建议并提示联系技术支持。"
        )
    if note:
        user_content += "\n\n【工具执行结果说明】\n" + note
    msgs.append({"role": "user", "content": user_content})
    return msgs


async def stream_answer(question: str, hits: list[dict], history: list[dict] | None,
                        note: str = "") -> AsyncGenerator[str, None]:
    if GREETING.match(question.strip()) and not hits:
        yield "你好，我是朴食智能AI结算台的故障排查助手。你可以直接描述现场现象，例如“结算台识别菜品总认错”“称重数值一直跳”“设备显示E10离线”，我会给出排查步骤并标注依据文档。"
        return

    if not config.DEEPSEEK_API_KEY:
        async for piece in _offline_answer(question, hits, note):
            yield piece
        return

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages_for(question, hits, history, note),
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{config.DEEPSEEK_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    yield f"\n\n[模型调用失败 {resp.status_code}] {body}\n\n已退回知识库检索结果，请参考下方来源自行核对。"
                    async for piece in _offline_answer(question, hits, note):
                        yield piece
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        import json
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content") or ""
                    except Exception:  # noqa: BLE001
                        continue
                    if delta:
                        yield delta
    except Exception as exc:  # noqa: BLE001
        yield f"\n\n[模型网络异常] {type(exc).__name__}：{exc}\n\n已退回知识库检索结果。"
        async for piece in _offline_answer(question, hits, note):
            yield piece


def _key_sentences(content: str, terms: set[str], limit: int = 2) -> list[str]:
    """挑出与问题词重合度最高的原文句子，用于演示模式摘录。"""
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    sentences = []
    for line in lines:
        for sent in re.split(r"(?<=[。！？；])", line):
            sent = sent.strip().lstrip("•-· ")
            if len(sent) < 6:
                continue
            # 跳过“1. 适用范围与场景说明”这类标题行
            if re.match(r"^\d+[\.、]", sent) and "。" not in sent and len(sent) < 26:
                continue
            sentences.append(sent)
    scored = [(sum(1 for t in terms if len(t) >= 2 and t in s), s) for s in sentences]
    scored.sort(key=lambda x: -x[0])
    picked = []
    for score, sent in scored:
        if score == 0 and picked:
            continue
        if sent not in picked:
            picked.append(sent)
        if len(picked) >= limit:
            break
    return picked


async def _offline_answer(question: str, hits: list[dict], note: str = "") -> AsyncGenerator[str, None]:
    if not hits:
        text = (
            "知识库中暂未检索到与该问题直接相关的内容。\n\n"
            "建议：\n1. 补充对应的部署文档、设备手册或历史工单后再提问；\n"
            "2. 先记录设备型号、故障码、发生时间与现场现象，便于二线定位；\n"
            "3. 紧急情况请联系朴食科技技术支持。"
        )
        if note:
            text = note + "\n\n" + text
        for seg in _split_for_stream(text):
            yield seg
            await asyncio.sleep(0.01)
        return

    terms = set(retrieval.expand_query(question))
    text = "（演示模式：未配置 DEEPSEEK_API_KEY，以下为知识库原文摘录；配置 Key 后将由 DeepSeek 生成完整排查步骤）\n\n"
    if note:
        text += f"【工具结果】{note}\n\n"
    text += "【检索到的关键信息】\n"
    for i, h in enumerate(hits[:3], start=1):
        content = h["content"].strip()
        if len(content) <= 480:          # FAQ 这类短切片直接整段摘录，信息更完整
            picks = [content]
        else:
            picks = _key_sentences(content, terms)
        if not picks:
            continue
        loc = retrieval.locator_text(h["locator"])
        text += f"[{i}] 《{h['filename']}》{loc}：\n    " + "\n    ".join(picks) + "\n"
    text += "\n【建议】以上为原文摘录，请按引用位置核对完整内容后执行；"
    text += "若现场现象与资料不一致，请记录故障码与设备序列号，提交工单或联系技术支持。"
    for seg in _split_for_stream(text):
        yield seg
        await asyncio.sleep(0.01)


def _split_for_stream(text: str, size: int = 18) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]
