const state = {
  config: null,
  history: [],
  busy: false,
  currentSources: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, ms = 2200) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), ms);
}

/* ---------------- 初始化 ---------------- */
async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    state.config = await res.json();
    renderChips();
  } catch (err) {
    toast("无法连接后端服务，请确认服务已启动");
  }
}

function renderChips() {
  const c = state.config;
  if (!c) return;
  const chipLlm = $("#chip-llm");
  chipLlm.classList.add(c.llm_ready ? "on" : "off");
  chipLlm.querySelector("span").textContent = c.llm_ready
    ? `DeepSeek · ${c.model}`
    : "演示模式 · 未配置 Key";

  const chipRetrieval = $("#chip-retrieval");
  chipRetrieval.classList.add("on");
  chipRetrieval.querySelector("span").textContent =
    c.retrieval_mode === "hybrid" ? "混合检索 · BM25 + 向量" : "关键词检索 · BM25";

  const chipKb = $("#chip-kb");
  chipKb.classList.add(c.doc_count > 0 ? "on" : "off");
  chipKb.querySelector("span").textContent = `${c.doc_count} 份文档 · ${c.chunk_count} 切片`;
  $("#kb-count").textContent = `${c.doc_count} 份文档`;
  $("#chunk-count").textContent = `${c.chunk_count} 切片`;
}

async function loadDocuments() {
  const res = await fetch("/api/documents");
  const data = await res.json();
  const list = $("#doc-list");
  if (!data.documents.length) {
    list.innerHTML = '<li class="doc-empty">暂无文档，上传后自动切片入库</li>';
  } else {
    list.innerHTML = data.documents.map((d) => {
      const ext = (d.ext || "").replace(".", "");
      return `
        <li class="doc-item" data-id="${d.id}">
          <div class="doc-ext ${escapeHtml(ext)}">${escapeHtml(ext)}</div>
          <div class="doc-main" title="${escapeHtml(d.filename)}">
            <span class="doc-name">${escapeHtml(d.filename)}</span>
            <div class="doc-sub">${d.chunk_count} 个切片 · ${formatSize(d.size)}</div>
          </div>
          <div class="doc-actions">
            <button class="icon-btn view" title="查看切片">切片</button>
            <button class="icon-btn del" title="删除">删除</button>
          </div>
        </li>`;
    }).join("");
  }
  renderChips();
}

function formatSize(bytes) {
  if (!bytes) return "0 KB";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function loadStats() {
  const res = await fetch("/api/feedback/stats");
  const s = await res.json();
  $("#feedback-stats").innerHTML = `
    <div class="stat"><strong>${s.total}</strong><span>总反馈</span></div>
    <div class="stat"><strong>${s.rate}%</strong><span>有用率</span></div>
    <div class="stat"><strong>${s.useless}</strong><span>待优化</span></div>`;
}

/* ---------------- 上传 ---------------- */
async function uploadFiles(files) {
  if (!files || !files.length) return;
  const tip = $("#upload-tip");
  const zone = $("#dropzone");
  zone.classList.add("loading");
  tip.className = "upload-tip";
  tip.textContent = `正在解析 ${files.length} 个文件并切片入库…`;

  const fd = new FormData();
  Array.from(files).forEach((f) => fd.append("files", f));
  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "上传失败");
    }
    const ok = data.uploaded || [];
    const errs = data.errors || [];
    tip.className = "upload-tip " + (errs.length ? "err" : "ok");
    tip.innerHTML = [
      ok.length ? `已入库：${ok.map((x) => `${escapeHtml(x.filename)}（${x.chunks} 切片）`).join("、")}` : "",
      errs.length ? `失败：${errs.map((x) => `${escapeHtml(x.filename)} — ${escapeHtml(x.error)}`).join("；")}` : "",
    ].filter(Boolean).join("\n");
    await loadDocuments();
    await loadConfig();
    toast("知识库已更新");
  } catch (err) {
    tip.className = "upload-tip err";
    tip.textContent = err.message || "上传失败";
  } finally {
    zone.classList.remove("loading");
    $("#file-input").value = "";
  }
}

/* ---------------- 文档切片弹窗 ---------------- */
async function openChunks(docId) {
  const modal = $("#modal");
  $("#modal-title").textContent = "加载中…";
  $("#modal-sub").textContent = "";
  $("#modal-body").innerHTML = '<div class="thinking"><span class="spinner"></span>读取切片…</div>';
  modal.classList.add("show");
  try {
    const res = await fetch(`/api/documents/${docId}/chunks`);
    if (!res.ok) throw new Error("读取失败");
    const data = await res.json();
    $("#modal-title").textContent = data.document.filename;
    $("#modal-sub").textContent =
      `${data.chunks.length} 个切片 · 切片长度 ${state.config?.chunk_size || "-"} 字 · 重叠 ${state.config?.chunk_overlap ?? "-"} 字`;
    $("#modal-body").innerHTML = data.chunks.map((c) => `
      <div class="chunk-item">
        <div class="chunk-head"><strong>#${c.seq + 1}</strong><span>${escapeHtml(c.locator_text)}</span><span>${c.content.length} 字</span></div>
        <div class="chunk-text">${escapeHtml(c.content)}</div>
      </div>`).join("") || '<div class="doc-empty">该文档没有切片</div>';
  } catch (err) {
    $("#modal-title").textContent = "读取失败";
    $("#modal-body").innerHTML = `<div class="doc-empty">${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------- 对话 ---------------- */
function hideEmptyState() {
  const empty = $("#empty-state");
  if (empty) empty.remove();
}

function chatInner() {
  let inner = $(".chat-inner");
  if (!inner) {
    inner = document.createElement("div");
    inner.className = "chat-inner";
    $("#chat").appendChild(inner);
  }
  return inner;
}

function scrollToBottom() {
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

function addUserMessage(text) {
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  wrap.innerHTML = `<div class="user-bubble">${escapeHtml(text)}</div>`;
  chatInner().appendChild(wrap);
  scrollToBottom();
}

function renderAnswer(text) {
  let html = escapeHtml(text);
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[(\d+)\]/g, '<span class="cite" data-idx="$1">[$1]</span>');
  return html;
}

function createBotMessage() {
  const wrap = document.createElement("div");
  wrap.className = "msg bot";
  wrap.innerHTML = `
    <div class="bot-row">
      <div class="bot-avatar">朴</div>
      <div class="bot-body">
        <div class="plan-bar" hidden></div>
        <div class="answer-card">
          <div class="answer-text"><span class="thinking"><span class="spinner"></span>正在解析问题…</span><span class="cursor"></span></div>
        </div>
      </div>
    </div>`;
  chatInner().appendChild(wrap);
  scrollToBottom();
  return {
    root: wrap,
    textEl: wrap.querySelector(".answer-text"),
    bodyEl: wrap.querySelector(".bot-body"),
    planEl: wrap.querySelector(".plan-bar"),
  };
}

function renderPlan(el, plan) {
  if (!el || !plan) return;
  const toolNames = { query_tickets: "工单库", search_kb: "知识库" };
  const tools = (plan.tools || []).map((t) => toolNames[t] || t).join(" + ") || "无";
  const chips = [
    `意图：${plan.intent_label || "知识库检索"}`,
    plan.slots_text && plan.slots_text !== "无结构化条件" ? plan.slots_text : "",
    `工具：${tools}`,
  ].filter(Boolean);
  el.innerHTML = `
    <span class="plan-tag">已解析</span>
    ${chips.map((c) => `<span class="plan-chip">${escapeHtml(c)}</span>`).join("")}
    <span class="plan-src">${plan.parsed_by === "llm" ? "DeepSeek 解析" : "规则解析"}</span>`;
  el.hidden = false;
}

function renderSources(container, sources) {
  if (!sources || !sources.length) {
    container.insertAdjacentHTML("beforeend", `
      <div class="sources">
        <div class="sources-head"><span>参考来源</span><span class="line"></span><span>未命中知识库</span></div>
      </div>`);
    return;
  }
  const cards = sources.map((s) => `
    <div class="source-card" data-idx="${s.index}">
      <div class="sc-top">
        <span class="sc-idx">${s.index}</span>
        <span class="sc-name">${escapeHtml(s.filename)}</span>
        <span class="sc-loc">${escapeHtml(s.locator)}</span>
        <span class="sc-score">匹配 ${(s.score * 100).toFixed(0)}%</span>
      </div>
      <div class="sc-snippet">${escapeHtml(s.snippet)}</div>
    </div>`).join("");
  container.insertAdjacentHTML("beforeend", `
    <div class="sources">
      <div class="sources-head"><span>参考来源 · ${sources.length} 条</span><span class="line"></span><span>点击展开原文</span></div>
      ${cards}
    </div>`);

  container.querySelectorAll(".source-card").forEach((card) => {
    card.addEventListener("click", () => card.classList.toggle("open"));
  });
  container.querySelectorAll(".cite").forEach((el) => {
    el.addEventListener("click", () => {
      const idx = el.dataset.idx;
      const card = container.querySelector(`.source-card[data-idx="${idx}"]`);
      if (card) {
        card.classList.add("open", "highlight");
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(() => card.classList.remove("highlight"), 1200);
      }
    });
  });
}

function addFeedback(container, question, answer, sources) {
  const bar = document.createElement("div");
  bar.className = "feedback-bar";
  bar.innerHTML = `
    <span>这个回答有用吗？</span>
    <button class="fb-btn up" data-useful="1">有用</button>
    <button class="fb-btn down" data-useful="0">无用</button>`;
  container.appendChild(bar);

  const send = async (useful, comment = "") => {
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          answer,
          useful: !!useful,
          comment,
          source_docs: (sources || []).map((s) => s.filename),
        }),
      });
      bar.innerHTML = `<span class="fb-thanks">已记录反馈，${useful ? "感谢认可" : "会用于优化知识库与回答"}</span>`;
      loadStats();
    } catch (err) {
      toast("反馈提交失败，请稍后重试");
    }
  };

  bar.querySelector(".up").addEventListener("click", (e) => {
    e.currentTarget.classList.add("active");
    send(true);
  });
  bar.querySelector(".down").addEventListener("click", (e) => {
    e.currentTarget.classList.add("active");
    bar.insertAdjacentHTML("beforeend", `
      <div class="fb-note">
        <input type="text" placeholder="补充说明（选填）：哪里不准确？" maxlength="200">
        <button>提交</button>
      </div>`);
    const note = bar.querySelector(".fb-note");
    const input = note.querySelector("input");
    input.focus();
    note.querySelector("button").addEventListener("click", () => send(false, input.value.trim()));
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") send(false, input.value.trim());
    });
  });
}

function parseSSE(raw) {
  let event = "message";
  const data = [];
  raw.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  });
  return { event, data: data.join("\n") };
}

async function ask(question) {
  if (state.busy) return;
  const q = (question || "").trim();
  if (!q) return;

  hideEmptyState();
  state.busy = true;
  $("#send").disabled = true;
  $("#composer-status").textContent = "正在回答…";
  addUserMessage(q);
  $("#question").value = "";
  autoResize();

  const bot = createBotMessage();
  let answer = "";
  let sources = [];

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, history: state.history.slice(-6), top_k: 5 }),
    });
    if (!res.ok || !res.body) throw new Error("服务返回异常");

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let firstDelta = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!raw.trim()) continue;
        const { event, data } = parseSSE(raw);
        let payload = {};
        try { payload = JSON.parse(data); } catch (e) { continue; }

        if (event === "plan") {
          renderPlan(bot.planEl, payload);
        } else if (event === "sources") {
          sources = payload.sources || [];
          renderSources(bot.bodyEl, sources);
        } else if (event === "delta") {
          if (!firstDelta) {
            bot.textEl.innerHTML = "";
            firstDelta = true;
          }
          answer += payload.text || "";
          bot.textEl.innerHTML = renderAnswer(answer) + '<span class="cursor"></span>';
          scrollToBottom();
        } else if (event === "done") {
          answer = payload.answer || answer;
        }
      }
    }

    if (!answer) answer = "没有收到回答，请重试。";
    bot.textEl.innerHTML = renderAnswer(answer);
    addFeedback(bot.bodyEl, q, answer, sources);
    state.history.push({ role: "user", content: q });
    state.history.push({ role: "assistant", content: answer });
    if (state.history.length > 8) state.history = state.history.slice(-8);
  } catch (err) {
    bot.textEl.innerHTML = `<span style="color:#b42318">请求失败：${escapeHtml(err.message)}</span>`;
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    $("#composer-status").textContent = "";
    scrollToBottom();
  }
}

/* ---------------- 交互绑定 ---------------- */
function autoResize() {
  const ta = $("#question");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
}

function bindEvents() {
  const zone = $("#dropzone");
  const input = $("#file-input");

  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", () => uploadFiles(input.files));

  ["dragenter", "dragover"].forEach((evt) => {
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.remove("drag");
    });
  });
  zone.addEventListener("drop", (e) => {
    if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
  });

  $("#doc-list").addEventListener("click", async (e) => {
    const item = e.target.closest(".doc-item");
    if (!item) return;
    const id = item.dataset.id;
    if (e.target.closest(".view") || e.target.closest(".doc-main")) {
      openChunks(id);
    } else if (e.target.closest(".del")) {
      const name = item.querySelector(".doc-name").textContent;
      if (!confirm(`确定从知识库删除「${name}」？`)) return;
      try {
        const res = await fetch(`/api/documents/${id}`, { method: "DELETE" });
        if (!res.ok) throw new Error("删除失败");
        toast("已删除该文档及其切片");
        await loadDocuments();
        await loadConfig();
      } catch (err) {
        toast(err.message);
      }
    }
  });

  $("#send").addEventListener("click", () => ask($("#question").value));
  $("#question").addEventListener("input", autoResize);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask($("#question").value);
    }
  });

  $("#examples").addEventListener("click", (e) => {
    const btn = e.target.closest(".example");
    if (btn) ask(btn.textContent.trim());
  });

  $("#modal-close").addEventListener("click", () => $("#modal").classList.remove("show"));
  $("#modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") $("#modal").classList.remove("show");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#modal").classList.remove("show");
  });
}

async function init() {
  bindEvents();
  await loadConfig();
  await loadDocuments();
  await loadStats();
}

init();
