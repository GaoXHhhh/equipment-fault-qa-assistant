"""FastAPI 应用：知识库上传、切片、检索、流式问答、来源回溯、反馈。"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, config, db, ingest, llm, retrieval

STATIC_DIR = Path(__file__).resolve().parent / "static"

SAMPLE_FILES = [
    "01_部署实施手册_朴食智能AI结算台.docx",
    "02_设备手册_PS-AI200智能结算台.pdf",
    "03_历史工单_2025年Q1-Q3.xlsx",
    "04_常见问题FAQ.txt",
    "05_现场运维SOP.md",
]


def ingest_file(path: Path, display_name: str | None = None, doc_id: str | None = None) -> dict:
    name = display_name or path.name
    ext = path.suffix.lower()
    chunks = ingest.build_chunks(path)

    if retrieval.embedding_enabled():
        try:
            vectors = retrieval.embed_texts([c["content"] for c in chunks])
            if vectors:
                for c, v in zip(chunks, vectors):
                    c["embedding"] = v
        except Exception:  # noqa: BLE001  向量服务异常不阻塞入库
            pass

    doc_id = db.add_document(name, ext, path.stat().st_size, len(chunks), doc_id=doc_id)
    db.add_chunks(doc_id, chunks)
    try:
        tickets = ingest.extract_tickets(path)
        db.add_tickets(doc_id, tickets)
    except Exception as exc:  # noqa: BLE001  工单抽取失败不影响文档入库
        print(f"[ingest] 工单抽取失败 {name}: {exc}")
    retrieval.kb.reload()
    return {"doc_id": doc_id, "filename": name, "chunks": len(chunks)}


def seed_samples() -> None:
    if db.list_documents():
        return
    if not config.SAMPLE_DIR.exists():
        return
    for name in SAMPLE_FILES:
        path = config.SAMPLE_DIR / name
        if path.exists():
            try:
                ingest_file(path)
                print(f"[seed] 已载入示例知识库：{name}")
            except Exception as exc:  # noqa: BLE001
                print(f"[seed] 载入失败 {name}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    retrieval.kb.reload()
    seed_samples()
    print(f"[startup] 知识库切片数：{retrieval.kb.size}，检索模式：{retrieval.kb.retrieval_mode()}")
    print(f"[startup] DeepSeek：{'已配置（来源：' + config.DEEPSEEK_KEY_SOURCE + '）' if config.DEEPSEEK_API_KEY else '未配置（演示模式）'}")
    yield


app = FastAPI(title="朴食智能AI结算台 · 设备故障问答助手", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)
    top_k: int = Field(default=config.TOP_K, ge=1, le=10)


class FeedbackIn(BaseModel):
    question: str = ""
    answer: str = ""
    useful: bool
    comment: str = ""
    source_docs: list[str] = Field(default_factory=list)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config():
    return {
        "llm_ready": bool(config.DEEPSEEK_API_KEY),
        "key_source": config.DEEPSEEK_KEY_SOURCE,
        "model": config.DEEPSEEK_MODEL,
        "base_url": config.DEEPSEEK_BASE_URL,
        "retrieval_mode": retrieval.kb.retrieval_mode(),
        "embedding_provider": config.EMBEDDING_PROVIDER,
        "embedding_model": config.EMBEDDING_MODEL,
        "doc_count": len(db.list_documents()),
        "chunk_count": retrieval.kb.size,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "top_k": config.TOP_K,
    }


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    results, errors = [], []
    for f in files:
        name = Path(f.filename or "未命名文件").name
        ext = Path(name).suffix.lower()
        if ext not in config.ALLOWED_EXTS:
            errors.append({"filename": name, "error": f"不支持的文件类型 {ext or '(无扩展名)'}"})
            continue
        raw = await f.read()
        if len(raw) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append({"filename": name, "error": f"文件超过 {config.MAX_UPLOAD_MB}MB 限制"})
            continue
        doc_id = uuid.uuid4().hex[:12]
        save_path = config.UPLOAD_DIR / f"{doc_id}{ext}"
        save_path.write_bytes(raw)
        try:
            info = ingest_file(save_path, display_name=name, doc_id=doc_id)
            results.append(info)
        except Exception as exc:  # noqa: BLE001
            if save_path.exists():
                save_path.unlink()
            errors.append({"filename": name, "error": str(exc)})
    if not results and errors:
        raise HTTPException(status_code=400, detail="；".join(f"{e['filename']}：{e['error']}" for e in errors))
    return {"uploaded": results, "errors": errors,
            "doc_count": len(db.list_documents()), "chunk_count": retrieval.kb.size}


@app.get("/api/documents")
def documents():
    return {"documents": db.list_documents(), "chunk_count": retrieval.kb.size}


@app.get("/api/documents/{doc_id}/chunks")
def document_chunks(doc_id: str):
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    chunks = db.doc_chunks(doc_id)
    for c in chunks:
        c["locator_text"] = retrieval.locator_text(c["locator"])
    return {"document": doc, "chunks": chunks}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    path = config.UPLOAD_DIR / f"{doc_id}{doc['ext']}"
    if path.exists():
        path.unlink()
    db.delete_document(doc_id)
    retrieval.kb.reload()
    return {"ok": True, "doc_count": len(db.list_documents()), "chunk_count": retrieval.kb.size}


@app.post("/api/search")
def search(payload: ChatIn):
    hits = retrieval.kb.search(payload.question, payload.top_k)
    for h in hits:
        h["locator_text"] = retrieval.locator_text(h["locator"])
    return {"hits": hits}


@app.post("/api/chat")
async def chat(payload: ChatIn):
    question = payload.question.strip()
    plan = await asyncio.to_thread(agent.make_plan, question, payload.top_k)
    hits = plan["hits"]

    async def event_stream():
        yield sse("plan", agent.public_plan(plan))
        sources = [{
            "index": i,
            "filename": h["filename"],
            "locator": retrieval.locator_text(h["locator"]),
            "score": h["score"],
            "snippet": retrieval.make_snippet(h["content"], question),
            "doc_id": h["doc_id"],
            "chunk_id": h["chunk_id"],
        } for i, h in enumerate(hits, start=1)]
        yield sse("sources", {"sources": sources,
                              "retrieval_mode": retrieval.kb.retrieval_mode(),
                              "llm_ready": bool(config.DEEPSEEK_API_KEY)})
        full = []
        async for delta in llm.stream_answer(question, hits, payload.history, note=plan["note"]):
            full.append(delta)
            yield sse("delta", {"text": delta})
        yield sse("done", {"answer": "".join(full)})

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.post("/api/feedback")
def feedback(payload: FeedbackIn):
    db.add_feedback(payload.question, payload.answer, payload.useful, payload.comment, payload.source_docs)
    return {"ok": True, "stats": db.feedback_stats()}


@app.get("/api/feedback/stats")
def feedback_stats():
    return db.feedback_stats()


@app.get("/api/health")
def health():
    return {"ok": True, "docs": len(db.list_documents()), "chunks": retrieval.kb.size}
