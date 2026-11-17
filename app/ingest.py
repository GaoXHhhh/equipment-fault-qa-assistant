"""文档解析 + 切片。支持 PDF / Word / Excel / CSV / TXT / Markdown。"""
import csv
import io
import re
from pathlib import Path

from . import config

SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


# ---------------------------------------------------------------- 文本切分
def _split_long(text: str, max_chars: int) -> list[str]:
    sentences = [s for s in SENT_SPLIT.split(text) if s.strip()]
    out, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) <= max_chars:
            buf += s
        else:
            if buf.strip():
                out.append(buf.strip())
            while len(s) > max_chars:          # 极端长句硬切
                out.append(s[:max_chars])
                s = s[max_chars:]
            buf = s
    if buf.strip():
        out.append(buf.strip())
    return out


def split_text(text: str, max_chars: int = None, overlap: int = None) -> list[str]:
    max_chars = max_chars or config.CHUNK_SIZE
    overlap = overlap if overlap is not None else config.CHUNK_OVERLAP
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paragraphs:
        if len(p) <= max_chars:
            units.append(p)
        else:
            units.extend(_split_long(p, max_chars))

    merged: list[str] = []
    buf = ""
    for u in units:
        if not buf:
            buf = u
        elif len(buf) + len(u) + 1 <= max_chars:
            buf += "\n" + u
        else:
            merged.append(buf)
            buf = u
    if buf:
        merged.append(buf)

    # 相邻切片加尾部重叠，避免答案被切断
    result = [merged[0]]
    for prev, cur in zip(merged, merged[1:]):
        tail = prev[-overlap:] if overlap > 0 else ""
        result.append((tail + "\n" + cur).strip() if tail else cur)
    return result


# ---------------------------------------------------------------- 各格式解析
def _docx_sections(path: Path) -> list[dict]:
    from docx import Document

    doc = Document(str(path))
    sections: list[dict] = []
    buf: list[str] = []
    heading = ""

    def flush():
        if buf:
            text = "\n".join(buf).strip()
            if text:
                sections.append({"text": text, "locator": {"section": heading} if heading else {}})
            buf.clear()

    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = (p.style.name or "") if p.style is not None else ""
        if style.lower().startswith("heading") or style.startswith("标题"):
            flush()
            heading = text
            buf.append(text)
        else:
            buf.append(text)
    flush()

    for idx, table in enumerate(doc.tables, start=1):
        lines = []
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            line = " | ".join([c for c in cells if c])
            if line:
                lines.append(line)
        if not lines:
            continue
        sections.append({
            "text": f"[表格{idx}]\n" + "\n".join(lines),
            "locator": {"table": idx, "section": heading} if heading else {"table": idx},
        })
    return sections


def _pdf_sections(path: Path) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("PDF 已加密，无法解析，请提供未加密版本") from exc
    sections = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        text = text.strip()
        if text:
            sections.append({"text": text, "locator": {"page": i}})
    if not sections:
        raise ValueError("PDF 未提取到文字，可能是扫描件（图片型），请提供可复制文字的版本")
    return sections


def _xlsx_sections(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    sections = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
        if not rows:
            continue
        header = [str(c).strip() if c is not None else f"列{i + 1}" for i, c in enumerate(rows[0])]
        data = rows[1:] or [rows[0]]
        row_texts: list[tuple[int, str]] = []
        for offset, row in enumerate(data):
            pairs = []
            for i, cell in enumerate(row):
                if cell is None or str(cell).strip() == "":
                    continue
                name = header[i] if i < len(header) else f"列{i + 1}"
                pairs.append(f"{name}：{str(cell).strip()}")
            if pairs:
                row_texts.append((offset + 2, "；".join(pairs)))

        # 每行独立成块：一行工单自带列名，语义完整，避免多工单合并稀释词频
        for rowno, txt in row_texts:
            sections.append({
                "text": f"【工作表：{ws.title}】第{rowno}行 {txt}",
                "locator": {"sheet": ws.title, "rows": str(rowno)},
            })
    wb.close()
    return sections


def _csv_sections(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 编码无法识别，请另存为 UTF-8")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip() or f"列{i + 1}" for i, c in enumerate(rows[0])]
    data = rows[1:] or [rows[0]]
    row_texts = []
    for offset, row in enumerate(data):
        pairs = [f"{header[i] if i < len(header) else '列' + str(i + 1)}：{c.strip()}"
                 for i, c in enumerate(row) if c.strip()]
        if pairs:
            row_texts.append((offset + 2, "；".join(pairs)))
    sections = []
    for rowno, txt in row_texts:
        sections.append({
            "text": f"第{rowno}行 {txt}",
            "locator": {"rows": str(rowno)},
        })
    return sections


def _text_sections(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        raise ValueError("文本编码无法识别，请另存为 UTF-8")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    if path.suffix.lower() == ".md":
        sections, buf, heading = [], [], ""
        for line in text.split("\n"):
            if re.match(r"^#{1,6}\s+", line):
                if buf:
                    sections.append({"text": "\n".join(buf).strip(), "locator": {"section": heading} if heading else {}})
                    buf = []
                heading = re.sub(r"^#{1,6}\s+", "", line).strip()
                buf.append(heading)
            else:
                buf.append(line)
        if buf:
            sections.append({"text": "\n".join(buf).strip(), "locator": {"section": heading} if heading else {}})
        return [s for s in sections if s["text"]]

    # 问答型文本（FAQ）：按 Q 编号分段，保证“一个问题 = 一个切片”，来源能精确到题
    if re.search(r"(?m)^Q\s*\d+", text):
        sections, buf, head = [], [], ""
        for line in text.split("\n"):
            if re.match(r"^Q\s*\d+", line.strip()):
                if buf:
                    body = "\n".join(buf).strip()
                    if body:
                        sections.append({"text": body, "locator": {"section": head[:40]} if head else {}})
                buf = [line.strip()]
                head = line.strip()
            else:
                buf.append(line)
        if buf:
            body = "\n".join(buf).strip()
            if body:
                sections.append({"text": body, "locator": {"section": head[:40]} if head else {}})
        return [s for s in sections if s["text"]]

    return [{"text": text, "locator": {}}]


def extract_sections(path: Path) -> list[dict]:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _docx_sections(path)
    if ext == ".doc":
        raise ValueError("暂不支持旧版 .doc，请用 Word 另存为 .docx 后上传")
    if ext == ".pdf":
        return _pdf_sections(path)
    if ext == ".xlsx":
        return _xlsx_sections(path)
    if ext == ".xls":
        raise ValueError("暂不支持旧版 .xls，请另存为 .xlsx 后上传")
    if ext == ".csv":
        return _csv_sections(path)
    if ext in (".txt", ".md"):
        return _text_sections(path)
    raise ValueError(f"不支持的文件类型：{ext}")


def build_chunks(path: Path) -> list[dict]:
    """解析 + 切片，返回 [{content, locator}]。"""
    sections = extract_sections(path)
    chunks: list[dict] = []
    for sec in sections:
        for piece in split_text(sec["text"]):
            locator = dict(sec["locator"])
            chunk = {"content": piece, "locator": locator}
            chunks.append(chunk)
    if not chunks:
        raise ValueError("文档内容为空，未生成任何切片")
    return chunks


# ---------------------------------------------------------------- 结构化工单抽取
TICKET_FIELDS = {
    "ticket_no": ["工单号", "工单编号"],
    "report_date": ["报障日期", "故障日期", "发生日期"],
    "customer": ["客户名称", "客户", "单位"],
    "scene": ["场景", "场景类型"],
    "model": ["设备型号", "型号"],
    "symptom": ["故障现象", "故障描述", "现象"],
    "fault_code": ["故障码", "错误码", "故障代码"],
    "root_cause": ["根因", "根本原因", "原因"],
    "solution": ["解决方案", "处理方案", "处理方法"],
    "hours": ["处理时长", "时长"],
    "status": ["工单状态", "状态"],
}


def _norm_date(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    match = re.match(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})", text)
    if match:
        return f"{int(match.group(1))}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text[:10]


def _map_header(row: list) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for index, cell in enumerate(row):
        name = str(cell or "").strip().replace("（", "(").replace("）", ")")
        if not name:
            continue
        for field, aliases in TICKET_FIELDS.items():
            if field in mapping.values():
                continue
            if any(alias in name for alias in aliases):
                mapping[index] = field
                break
    return mapping


def _rows_to_tickets(rows: list[list], sheet: str) -> list[dict]:
    header_index, mapping = -1, {}
    for i, row in enumerate(rows[:8]):
        candidate = _map_header(row)
        if len(candidate) >= 3 and "ticket_no" in candidate.values():
            header_index, mapping = i, candidate
            break
    if header_index < 0:
        return []

    tickets = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 1):
        item = {"sheet": sheet, "row_no": offset + 1}
        for index, field in mapping.items():
            if index < len(row):
                value = row[index]
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                item[field] = str(value).strip() if value is not None else ""
        item["report_date"] = _norm_date(item.get("report_date", ""))
        item["fault_code"] = item.get("fault_code", "").upper()
        if not (item.get("ticket_no") or item.get("report_date") or item.get("fault_code")):
            continue
        tickets.append(item)
    return tickets


def extract_tickets(path: Path) -> list[dict]:
    """从工单类 Excel / CSV 中抽取结构化记录；非工单文件返回空列表。"""
    ext = path.suffix.lower()
    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        tickets = []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            tickets.extend(_rows_to_tickets(rows, ws.title))
        wb.close()
        return tickets

    if ext == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return []
        rows = list(csv.reader(io.StringIO(text)))
        return _rows_to_tickets(rows, "")

    return []
