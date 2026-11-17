"""SQLite 存储层：文档、切片、反馈。每次操作独立连接，简单可靠。"""
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'ready',
    message     TEXT DEFAULT '',
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id        TEXT PRIMARY KEY,
    doc_id    TEXT NOT NULL,
    seq       INTEGER,
    content   TEXT NOT NULL,
    locator   TEXT,
    embedding TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question     TEXT,
    answer       TEXT,
    useful       INTEGER,
    comment      TEXT,
    source_docs  TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    doc_id      TEXT,
    ticket_no   TEXT,
    report_date TEXT,
    customer    TEXT,
    scene       TEXT,
    model       TEXT,
    symptom     TEXT,
    fault_code  TEXT,
    root_cause  TEXT,
    solution    TEXT,
    hours       TEXT,
    status      TEXT,
    sheet       TEXT,
    row_no      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_date ON tickets(report_date);
CREATE INDEX IF NOT EXISTS idx_tickets_code ON tickets(fault_code);
CREATE INDEX IF NOT EXISTS idx_tickets_no ON tickets(ticket_no);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def add_document(filename: str, ext: str, size: int, chunk_count: int,
                 status: str = "ready", message: str = "", doc_id: str | None = None) -> str:
    doc_id = doc_id or uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents(id, filename, ext, size, chunk_count, status, message, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (doc_id, filename, ext, size, chunk_count, status, message, now()),
        )
    return doc_id


def add_chunks(doc_id: str, chunks: Iterable[dict]) -> None:
    rows = []
    for i, ch in enumerate(chunks):
        rows.append((
            uuid.uuid4().hex[:12],
            doc_id,
            i,
            ch["content"],
            json.dumps(ch.get("locator") or {}, ensure_ascii=False),
            json.dumps(ch.get("embedding")) if ch.get("embedding") is not None else None,
        ))
    with connect() as conn:
        conn.executemany(
            "INSERT INTO chunks(id, doc_id, seq, content, locator, embedding) VALUES (?,?,?,?,?,?)",
            rows,
        )


def list_documents() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, filename, ext, size, chunk_count, status, message, created_at "
            "FROM documents ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_document(doc_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def delete_document(doc_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM tickets WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))


# ---------------------------------------------------------------- 结构化工单
def add_tickets(doc_id: str, tickets: list[dict]) -> None:
    if not tickets:
        return
    rows = [(
        uuid.uuid4().hex[:12], doc_id,
        t.get("ticket_no", ""), t.get("report_date", ""), t.get("customer", ""),
        t.get("scene", ""), t.get("model", ""), t.get("symptom", ""),
        t.get("fault_code", ""), t.get("root_cause", ""), t.get("solution", ""),
        t.get("hours", ""), t.get("status", ""), t.get("sheet", ""), t.get("row_no", 0),
    ) for t in tickets]
    with connect() as conn:
        conn.executemany(
            "INSERT INTO tickets(id, doc_id, ticket_no, report_date, customer, scene, model, "
            "symptom, fault_code, root_cause, solution, hours, status, sheet, row_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def query_tickets(date: str = "", month_day: str = "", ticket_no: str = "",
                  fault_code: str = "", customer: str = "", keyword: str = "",
                  year: str = "", keywords: list[str] | None = None,
                  keyword_match: str = "and", keyword_groups: list[list[str]] | None = None,
                  limit: int = 30) -> list[dict]:
    where, args = [], []
    if date:
        where.append("t.report_date = ?")
        args.append(date)
    if year:
        where.append("t.report_date LIKE ?")
        args.append(f"{year}-%")
    if month_day:
        where.append("t.report_date LIKE ?")
        args.append(f"%-{month_day}")
    if ticket_no:
        where.append("UPPER(t.ticket_no) LIKE ?")
        args.append(f"%{ticket_no.upper()}%")
    if fault_code:
        where.append("UPPER(t.fault_code) = ?")
        args.append(fault_code.upper())
    if customer:
        where.append("t.customer LIKE ?")
        args.append(f"%{customer}%")
    if keyword:
        where.append("(t.symptom LIKE ? OR t.root_cause LIKE ? OR t.solution LIKE ?)")
        args.extend([f"%{keyword}%"] * 3)
    # 现象关键词列表：在故障现象/根因/方案/故障码/工单号多字段匹配
    terms = [k.strip() for k in (keywords or []) if k and k.strip()]
    if terms:
        field_scope = ("t.symptom", "t.root_cause", "t.solution", "t.fault_code", "t.ticket_no")
        clauses = []
        for term in terms:
            one = " OR ".join(f"{f} LIKE ?" for f in field_scope)
            clauses.append(f"({one})")
            args.extend([f"%{term}%"] * len(field_scope))
        joiner = " AND " if keyword_match == "and" else " OR "
        where.append(joiner.join(clauses))
    # 关键词组：组内 OR、组间 AND（用于现象类工单统计，避免泛词扩散）
    groups = [g for g in (keyword_groups or []) if g]
    if groups:
        field_scope = ("t.symptom", "t.root_cause", "t.solution", "t.fault_code", "t.ticket_no")
        group_clauses = []
        for group in groups:
            inner = []
            for term in group:
                inner.append("(" + " OR ".join(f"{f} LIKE ?" for f in field_scope) + ")")
                args.extend([f"%{term}%"] * len(field_scope))
            group_clauses.append("(" + " OR ".join(inner) + ")")
        where.append(" AND ".join(group_clauses))
    sql = (
        "SELECT t.*, d.filename, d.ext FROM tickets t JOIN documents d ON d.id = t.doc_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY t.report_date DESC, t.row_no ASC LIMIT ?"
    )
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def ticket_coverage() -> tuple[str, str, int]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        if not total:
            return "", "", 0
        row = conn.execute("SELECT MIN(report_date) a, MAX(report_date) b FROM tickets "
                           "WHERE report_date != ''").fetchone()
    return row["a"] or "", row["b"] or "", total


def ticket_code_stats(fault_code: str) -> tuple[int, list[dict]]:
    rows = query_tickets(fault_code=fault_code, limit=50)
    return len(rows), rows


def ticket_keyword_stats(groups: list[list[str]], year: str = "") -> tuple[int, list[dict]]:
    """按现象关键词组统计工单：组内 OR、组间 AND，部件词收敛，避免泛词误命中。"""
    rows = query_tickets(year=year, keyword_groups=groups, limit=100)
    return len(rows), rows


def all_chunks() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.doc_id, c.seq, c.content, c.locator, c.embedding, d.filename, d.ext "
            "FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.status='ready' "
            "ORDER BY d.created_at DESC, c.seq ASC"
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["locator"] = json.loads(item["locator"] or "{}")
        item["embedding"] = json.loads(item["embedding"]) if item["embedding"] else None
        out.append(item)
    return out


def doc_chunks(doc_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, seq, content, locator FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["locator"] = json.loads(item["locator"] or "{}")
        out.append(item)
    return out


def add_feedback(question: str, answer: str, useful: bool, comment: str, source_docs: list[str]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO feedback(question, answer, useful, comment, source_docs, created_at) VALUES (?,?,?,?,?,?)",
            (question, answer, 1 if useful else 0, comment,
             json.dumps(source_docs, ensure_ascii=False), now()),
        )


def feedback_stats() -> dict:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]
        useful = conn.execute("SELECT COUNT(*) c FROM feedback WHERE useful=1").fetchone()["c"]
        rows = conn.execute(
            "SELECT question, useful, comment, created_at FROM feedback ORDER BY id DESC LIMIT 8"
        ).fetchall()
    rate = round(useful / total * 100, 1) if total else 0.0
    return {"total": total, "useful": useful, "useless": total - useful, "rate": rate,
            "recent": [dict(r) for r in rows]}
