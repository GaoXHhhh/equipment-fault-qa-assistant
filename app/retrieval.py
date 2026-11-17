"""检索层：中文分词 + BM25 关键词召回 + 本地/API 语义召回，RRF 排名融合。

设计要点：
1. 领域同义词等价组同时作用于 BM25 查询和语义查询（旧版只作用于 BM25，
   导致“刷卡不识别”这类口语问法在语义通道里仍是纯字面匹配）。
2. BM25 与语义通道各自排名后用 RRF（Reciprocal Rank Fusion）融合，
   避免两种分数尺度不一致时线性加权互相压制。
3. 日期、故障码、工单号等精确实体在融合分之上额外加权。
"""
import math
import re
import zlib
from collections import Counter

import httpx
import jieba

from . import config, db

# ---------------------------------------------------------------- 领域同义词
# 等价组：组内任意词出现在问题中，整组词都作为扩展词进入 BM25 与语义查询。
# 短语和单字部件混排，覆盖口语化提问与工单/手册里的规范表述。
SYNONYM_GROUPS = [
    # 刷卡 / 读卡（核心场景：刷卡不识别 ↔ 刷卡无反应 / 读卡失败 / E08）
    ["刷卡", "读卡", "读卡器", "一卡通", "校园卡", "银行卡", "刷卡不识别", "刷不了卡",
     "刷卡没反应", "刷卡无反应", "读卡失败", "读卡没反应", "读卡器故障", "卡没反应",
     "读卡错误", "e08"],
    ["不识别", "无法识别", "识别失败", "认不出", "识别不了", "读卡失败", "无反应", "没反应"],
    # 设备 / 结算台
    ["结算台", "收银机", "一体机", "自助结算", "设备", "机器", "主机"],
    # 称重
    ["称重", "重量", "秤", "传感器", "漂移", "称重漂移", "数值跳动", "重量不准"],
    # 摄像头 / 视觉识别
    ["摄像头", "相机", "图像", "画面", "镜头", "识别", "认错", "置信度", "模型", "菜品"],
    # 网络
    ["网络", "断网", "掉线", "离线", "心跳", "连不上网"],
    # 打印机
    ["打印机", "小票", "打票", "打印", "纸"],
    # 部署 / 校准 / 报错 / 登录
    ["部署", "安装", "实施", "上线"],
    ["校准", "标定", "砝码", "零点"],
    ["报错", "故障码", "错误码", "异常", "故障代码"],
    ["登录", "账号", "密码", "授权"],
    # 时间 / 工单类口语
    ["日期", "报障日期", "工单", "哪天"],
]

STOPWORDS = set("的 了 是 在 和 与 或 及 请 我 你 他 她 它 这 那 有 无 吗 呢 啊 吧 怎么 如何 什么 可以 一下 需要 为什么 请问 帮 我们".split())

DATE_RE = re.compile(r"(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?")
YEAR_RE = re.compile(r"(20\d{2})\s*年")
MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
CODE_RE = re.compile(r"\bE\s?(\d{2})\b", re.I)
TICKET_RE = re.compile(r"\b[A-Z]{2,3}[-\s]?\d{4}[-\s]\d{3,4}\b", re.I)


def extract_dates(text: str) -> list[str]:
    """把 2025.5.7 / 2025/5/7 / 2025年5月7日 统一成 2025-05-07。"""
    out = []
    for m in DATE_RE.finditer(text or ""):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.append(f"{year}-{month:02d}-{day:02d}")
    return out


def extract_year(text: str) -> str:
    """提取单独出现的年份（如“2025年出现过几次”），用于按年过滤工单。"""
    m = YEAR_RE.search(text or "")
    return m.group(1) if m else ""


def tokenize(text: str) -> list[str]:
    raw = text or ""
    tokens: list[str] = []
    # 日期、故障码、工单号单独成 token，避免被分词拆成无意义的单数字
    for date in extract_dates(raw):
        tokens.append(date)
        tokens.append(date[5:])
    for m in MONTH_DAY_RE.finditer(raw):
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            tokens.append(f"{month:02d}-{day:02d}")
    for code in CODE_RE.findall(raw):
        tokens.append(f"E{code}")
    for m in TICKET_RE.finditer(raw):
        tokens.append(re.sub(r"\s+", "", m.group(0)).upper())

    text = raw.lower()
    for tok in jieba.lcut_for_search(text):
        tok = tok.strip()
        if not tok or tok in STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", tok):
            continue
        if re.fullmatch(r"\d{1,2}", tok):
            continue
        tokens.append(tok)
    return tokens


def _matched_groups(question: str) -> list[list[str]]:
    """返回问题命中的同义词等价组（子串匹配，大小写不敏感）。"""
    q = (question or "").lower()
    groups = []
    for group in SYNONYM_GROUPS:
        if any(term.lower() in q for term in group):
            groups.append(group)
    return groups


def expand_query(question: str) -> list[str]:
    """BM25 查询词：分词结果 + 命中等价组的全部词。"""
    base = tokenize(question)
    extra: list[str] = []
    for group in _matched_groups(question):
        extra.extend(group)
    seen, out = set(), []
    for t in base + extra:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def semantic_query_text(question: str) -> str:
    """语义通道查询文本：原问题拼接扩展词，让 n-gram/embedding 也能理解同义表述。"""
    parts = [question or ""]
    for group in _matched_groups(question):
        parts.extend(group)
    return " ".join(parts)


# ---------------------------------------------------------------- 向量部分
def embedding_enabled() -> bool:
    return config.EMBEDDING_PROVIDER != "none" and bool(config.EMBEDDING_API_KEY)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not embedding_enabled():
        return []
    vectors: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), 10):
            batch = texts[i:i + 10]
            resp = client.post(
                f"{config.EMBEDDING_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {config.EMBEDDING_API_KEY}"},
                json={"model": config.EMBEDDING_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            vectors.extend([d["embedding"] for d in data])
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- 本地语义向量
NGRAM_DIM = 1024


def _clean_for_ngram(text: str) -> str:
    """只保留文字和数字，去掉标点与空白，中英文都适用。"""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _ngrams(text: str) -> list[str]:
    """字符 2-gram + 3-gram：无需模型即可捕捉“刷卡不识别/刷卡无反应”这类近义表述。"""
    clean = _clean_for_ngram(text)
    if not clean:
        return []
    grams: list[str] = []
    for n in (2, 3):
        grams.extend(clean[i:i + n] for i in range(0, max(0, len(clean) - n + 1)))
    return grams or [clean]


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _hashed_vector(grams: list[str], idf: dict[str, float], dim: int = NGRAM_DIM) -> list[float]:
    """把 n-gram 哈希到固定维度，按 IDF 加权，得到稀疏向量的稠密表示。"""
    vec = [0.0] * dim
    for g in grams:
        vec[zlib.crc32(g.encode("utf-8")) % dim] += idf.get(g, 1.0)
    return _l2(vec)


GENERIC_TERMS = set("故障 问题 情况 现象 出现 发生 几次 多少 次数 记录 设备 这个 那个 一下 怎么 如何 排查 处理 原因".split())

# 具体部件名词：只有含部件词的同义组才用于工单库检索，
# 排除“不识别/无反应”这类纯现象泛词组，防止统计范围被放大。
PART_TERMS = {"刷卡", "读卡", "读卡器", "打印机", "打印", "摄像头", "相机", "称重",
              "网络", "结算台", "收银机", "一体机", "触摸屏", "主板", "电源", "适配器",
              "风扇", "传感器", "扫码枪", "秤", "镜头"}


def ticket_keyword_groups(question: str) -> list[list[str]]:
    """工单库关键词组：组内 OR、组间 AND。只保留含具体部件且被问题命中的同义组。"""
    q = (question or "").lower()
    out: list[list[str]] = []
    for group in _matched_groups(question):
        terms = [t for t in group if len(t) >= 2 and t not in GENERIC_TERMS]
        if not terms:
            continue
        # 必须有“部件词本身”出现在问题中，排除仅现象词命中、部件词未出现的跨域组
        if any(t in PART_TERMS and t in q for t in terms):
            if terms not in out:
                out.append(terms)
    return out


def key_terms(question: str) -> list[str]:
    """提取用于工单库模糊匹配的关键词：去疑问词、去泛词，保留现象/部件名。"""
    out: list[str] = []
    seen: set[str] = set()
    for t in expand_query(question):
        t = t.strip()
        if len(t) < 2 or t in GENERIC_TERMS or t in STOPWORDS:
            continue
        if re.fullmatch(r"\d+", t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------- BM25
class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs_tokens)
        self.avgdl = (sum(len(d) for d in docs_tokens) / self.n) if self.n else 0
        self.tf = [Counter(d) for d in docs_tokens]
        self.df: Counter = Counter()
        for d in docs_tokens:
            for term in set(d):
                self.df[term] += 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in self.df.items()}

    def score(self, query_tokens: list[str], idx: int) -> float:
        tf = self.tf[idx]
        dl = sum(tf.values())
        s = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            f = tf[term]
            s += self.idf.get(term, 0) * (f * (self.k1 + 1)) / (
                f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            )
        return s


# ---------------------------------------------------------------- RRF 融合
RRF_K = 60


def _rank_map(scores: list[float]) -> dict[int, int]:
    """按分数降序排名（rank 从 1 开始），零分文档不进排名。"""
    order = sorted((i for i, s in enumerate(scores) if s > 0),
                   key=lambda i: scores[i], reverse=True)
    return {i: rank for rank, i in enumerate(order, start=1)}


# ---------------------------------------------------------------- 知识库单例
class KnowledgeBase:
    def __init__(self):
        self.chunks: list[dict] = []
        self.tokens: list[list[str]] = []
        self.bm25: BM25 | None = None
        self.vectors: list[list[float]] | None = None       # 外部 Embedding API 向量
        self.local_vectors: list[list[float]] | None = None  # 本地字符语义向量
        self.gram_idf: dict[str, float] = {}

    def reload(self) -> None:
        self.chunks = db.all_chunks()
        self.tokens = [tokenize(c["content"]) for c in self.chunks]
        self.bm25 = BM25(self.tokens) if self.chunks else None
        if any(c.get("embedding") for c in self.chunks):
            self.vectors = [c.get("embedding") or [] for c in self.chunks]
        else:
            self.vectors = None

        # 本地语义向量：每次重建索引时一次性算好，查询只算问题本身
        if config.LOCAL_SEMANTIC and self.chunks:
            grams_list = [_ngrams(c["content"]) for c in self.chunks]
            df: Counter = Counter()
            for grams in grams_list:
                df.update(set(grams))
            n = len(grams_list)
            self.gram_idf = {
                g: math.log((1 + n) / (1 + c)) + 1.0 for g, c in df.items()
            }
            self.local_vectors = [_hashed_vector(g, self.gram_idf) for g in grams_list]
        else:
            self.local_vectors = None
            self.gram_idf = {}

    @property
    def size(self) -> int:
        return len(self.chunks)

    def retrieval_mode(self) -> str:
        if config.LOCAL_SEMANTIC and self.local_vectors:
            return "hybrid-local"
        if embedding_enabled() and self.vectors:
            return "hybrid-api"
        return "bm25"

    def search(self, question: str, top_k: int = None) -> list[dict]:
        if not self.chunks or not self.bm25:
            return []
        top_k = top_k or config.TOP_K
        q_tokens = expand_query(question)

        # ---- 通道 1：BM25 原始分
        raw_bm25 = [self.bm25.score(q_tokens, i) for i in range(len(self.chunks))]

        # ---- 通道 2：语义分（同义词扩展后的查询文本，本地 n-gram 优先，API 兜底）
        q_sem_text = semantic_query_text(question)
        q_vec = None
        if self.vectors and embedding_enabled():
            try:
                q_vec = embed_texts([q_sem_text])[0]
            except Exception:  # 向量服务不可用时退回本地通道/纯 BM25
                q_vec = None

        local_q = None
        if self.local_vectors and self.gram_idf:
            local_q = _hashed_vector(_ngrams(q_sem_text), self.gram_idf)

        raw_sem = [0.0] * len(self.chunks)
        sem_active = False
        for i in range(len(self.chunks)):
            if local_q and self.local_vectors and self.local_vectors[i]:
                raw_sem[i] = max(0.0, _cosine(local_q, self.local_vectors[i]))
                sem_active = True
            elif q_vec and self.vectors and self.vectors[i]:
                raw_sem[i] = max(0.0, _cosine(q_vec, self.vectors[i]))
                sem_active = True

        if not q_tokens and not sem_active:
            return []

        # ---- RRF 融合两通道排名
        bm_ranks = _rank_map(raw_bm25)
        sem_ranks = _rank_map(raw_sem)
        channels = int(bool(bm_ranks)) + int(bool(sem_ranks))
        max_possible = channels / (RRF_K + 1) if channels else 1.0

        # 精确实体（日期/故障码/工单号）命中加权，避免被语义相似但无关的切片挤掉
        entities: list[str] = []
        entities.extend(extract_dates(question))
        entities.extend(f"E{c}" for c in CODE_RE.findall(question))
        entities.extend(re.sub(r"\s+", "", m).upper() for m in TICKET_RE.findall(question))
        entities.extend(m.group(0) for m in MONTH_DAY_RE.finditer(question))

        scored = []
        for i, ch in enumerate(self.chunks):
            rrf = 0.0
            if i in bm_ranks:
                rrf += 1.0 / (RRF_K + bm_ranks[i])
            if i in sem_ranks:
                rrf += 1.0 / (RRF_K + sem_ranks[i])
            if rrf <= 0:
                continue
            score = rrf / max_possible

            content_lower = ch["content"].lower()
            for ent in entities:
                if ent and ent.lower() in content_lower:
                    score += 0.15
                    break

            # 文件名命中轻微加权（如“部署文档里怎么写”）
            name = ch.get("filename", "").lower()
            name_hits = sum(1 for t in q_tokens if len(t) >= 2 and t in name)
            if name_hits:
                score += min(0.12, 0.04 * name_hits)
            if score <= 0:
                continue
            scored.append((score, i))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits = []
        for score, i in scored[:top_k]:
            ch = self.chunks[i]
            hits.append({
                "chunk_id": ch["id"],
                "doc_id": ch["doc_id"],
                "filename": ch["filename"],
                "ext": ch.get("ext", ""),
                "locator": ch.get("locator") or {},
                "content": ch["content"],
                "score": round(min(1.0, score), 3),
            })
        return hits


kb = KnowledgeBase()


def locator_text(locator: dict) -> str:
    parts = []
    if locator.get("page"):
        parts.append(f"第 {locator['page']} 页")
    if locator.get("sheet"):
        parts.append(f"工作表「{locator['sheet']}」")
    if locator.get("rows"):
        parts.append(f"第 {locator['rows']} 行")
    if locator.get("table"):
        parts.append(f"表格 {locator['table']}")
    if locator.get("section"):
        parts.append(f"章节「{locator['section']}」")
    return " ".join(parts) if parts else "正文"


def make_snippet(content: str, question: str, width: int = 240) -> str:
    """截取命中关键词附近的原文，避免来源卡片只显示无关开头。"""
    terms = [t for t in expand_query(question) if len(t) >= 2]
    positions = [content.find(t) for t in terms if t in content]
    pos = min(positions) if positions else -1
    if pos <= 30:
        snippet = content[:width]
        return snippet + ("…" if len(content) > width else "")
    start = max(0, pos - 50)
    end = min(len(content), start + width)
    snippet = content[start:end]
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(content) else "")
