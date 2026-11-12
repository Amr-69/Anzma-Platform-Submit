/* Floating building-assistant chat widget.
   Add ONE line before </body> in dashboard.html:
       <script src="/static/chat-widget.js" defer></script>
   The script loads chat-widget.css by itself, builds the button and popup, and talks to POST /chat. */
(function () {
  "use strict";
  if (window.__buildingChatLoaded) return;
  window.__buildingChatLoaded = true;

  const script = document.currentScript;
  const BASE = script && script.src ? script.src.replace(/[^/?#]*([?#].*)?$/, "") : "/static/";
  const API = { chat: "/chat", questions: "/chat/questions" };
  const STORE_KEY = "building-chat-v1";
  const STATE_KEY = "building-chat-state-v1";
  const MAX_STORED = 20;
  const TIMEOUT_MS = 180000;       // local model can take a while (two attempts)
  const SLOW_NOTE_MS = 10000;

  const FALLBACK_QUESTIONS = [
    "What is the current building status?",
    "What were the recent faults?",
    "What is the CCTV availability percentage?",
    "Are there any network issues?",
    "What are the repair priorities?",
    "How did energy consumption change?"
  ];

  let chatState = { last_alert_id: null, last_incident_id: null, last_assets: [] };

  function loadState() {
    try {
      const raw = sessionStorage.getItem(STATE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") chatState = { ...chatState, ...parsed };
      }
    } catch (_) { /* ignore state load errors */ }
  }
  function saveState() {
    try { sessionStorage.setItem(STATE_KEY, JSON.stringify(chatState)); } catch (_) { /* storage may be unavailable */ }
  }

  loadState();

  // ---------- helpers ----------
  const SVG_NS = "http://www.w3.org/2000/svg";

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    kids.flat().forEach(k => { if (k != null) el.append(k); });
    return el;
  }

  function icon(paths) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    paths.forEach(d => {
      const p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }

  const ICONS = {
    chat: ["M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.6-.8L3 21l1.9-5.1A8.4 8.4 0 1 1 21 11.5z"],
    close: ["M18 6 6 18", "M6 6l12 12"],
    reset: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5"],
    send: ["M22 2 11 13", "M22 2l-7 20-4-9-9-4 20-7z"]
  };

  const isArabic = s => /[\u0600-\u06FF]/.test(s);

  function detailText(body, status) {
    const d = body && body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) return d[0].msg || `Request rejected (HTTP ${status}).`;
    if (d && d.message) return d.message;
    return `The assistant returned an error (HTTP ${status}).`;
  }

  // ---------- state ----------
  let messages = [];       // {role: "user"|"bot", text?, data?}
  let busy = false;
  let unread = false;
  let questions = FALLBACK_QUESTIONS;

  function load() {
    try { messages = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]"); } catch (_) { messages = []; }
    if (!Array.isArray(messages)) messages = [];
  }
  function save() {
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(messages.slice(-MAX_STORED))); } catch (_) { /* storage may be unavailable */ }
  }

  // ---------- load stylesheet ----------
  if (!document.querySelector('link[href*="chat-widget.css"]')) {
    document.head.appendChild(h("link", { rel: "stylesheet", href: BASE + "chat-widget.css" }));
  }

  // ---------- build DOM ----------
  const dot = h("span", { class: "cw-dot", hidden: true });
  const fab = h("button", {
    class: "cw-fab", type: "button", "aria-label": "Open building assistant",
    "aria-expanded": "false", "aria-controls": "cw-panel"
  }, icon(ICONS.chat), dot);

  const btnReset = h("button", { class: "cw-iconbtn", type: "button", "aria-label": "Start a new chat", title: "New chat" }, icon(ICONS.reset));
  const btnClose = h("button", { class: "cw-iconbtn", type: "button", "aria-label": "Close chat", title: "Close" }, icon(ICONS.close));

  const head = h("header", { class: "cw-head" },
    h("div", { class: "cw-head-text" },
      h("h2", { class: "cw-title", text: "Building assistant" }),
      h("p", { class: "cw-sub", text: "Answers are built from your building data" })),
    btnReset, btnClose);

  const log = h("div", { class: "cw-log", role: "log", "aria-live": "polite", "aria-label": "Conversation" });

  const input = h("textarea", {
    class: "cw-input", rows: "1", maxlength: "500",
    placeholder: "Ask about the building…  (English or Arabic)", "aria-label": "Your question"
  });
  const send = h("button", { class: "cw-send", type: "submit", "aria-label": "Send", disabled: true }, icon(ICONS.send));
  const win = h("select", { class: "cw-win", "aria-label": "Time window" },
    h("option", { value: "", text: "Auto window" }),
    h("option", { value: "24", text: "Last 24 h" }),
    h("option", { value: "72", text: "Last 72 h" }),
    h("option", { value: "168", text: "Last 7 days" }));
  const form = h("form", { class: "cw-form", autocomplete: "off" },
    h("div", { class: "cw-row" }, input, send),
    h("div", { class: "cw-foot" }, h("span", { text: "Enter to send · Shift+Enter for a new line" }), win));

  const panel = h("section", { class: "cw-panel", id: "cw-panel", role: "dialog", "aria-label": "Building assistant", hidden: true },
    head, log, form);
  const root = h("div", { class: "cw-root" }, panel, fab);
  document.body.appendChild(root);

  // ---------- rendering ----------
  function scrollToBottom() { log.scrollTop = log.scrollHeight; }
  function scrollToStartOf(node) { log.scrollTop = Math.max(0, node.offsetTop - 8); }

  function renderAnswer(container, text, refs) {
    String(text || "").split("\n").forEach(raw => {
      const line = raw.trim();
      if (!line) return;
      const bullet = /^[-*\u2022]\s+/.test(line);
      const body = bullet ? line.replace(/^[-*\u2022]\s+/, "") : line;
      const row = h("div", { class: "cw-line" + (bullet ? " cw-li" : "") });
      body.split(/(\[F\d+\])/).forEach(part => {
        const m = part.match(/^\[(F\d+)\]$/);
        if (!m) { if (part) row.append(part); return; }
        if (!refs.has(m[1])) { row.append(part); return; }
        row.append(h("button", { class: "cw-ref", type: "button", "data-ref": m[1], title: "Show evidence " + m[1] }, m[1]));
      });
      container.appendChild(row);
    });
  }

  function userNode(text) {
    return h("div", { class: "cw-msg cw-user" },
      h("div", { class: "cw-bubble", dir: "auto", text }));
  }

  function botNode(d) {
    const lang = d.language === "ar" ? "ar" : "en";
    const dir = lang === "ar" ? "rtl" : "ltr";
    const sources = Array.isArray(d.sources) ? d.sources : [];
    const refs = new Set(sources.map(s => s.ref));

    const bubble = h("div", { class: "cw-bubble", dir, lang });
    renderAnswer(bubble, d.answer, refs);

    const wrap = h("div", { class: "cw-msg cw-bot" }, bubble);

    // trust indicators
    const g = d.grounding || {};
    const tags = [];
    if (d.source !== "RULES") {
      tags.push(h("span", { class: "cw-tag " + (g.passed ? "ok" : "warn"), text: g.passed ? "✓ Numbers matched the query results (grounding check, not a meaning check)" : "⚠ Not fully verified" }));
      tags.push(h("span", { class: "cw-tag", text: d.source === "OLLAMA" ? "AI answer" : "Data summary" }));
    }
    if (d.data_sufficient === false) tags.push(h("span", { class: "cw-tag warn", text: "Limited data" }));
    if (d.window && d.window.hours) tags.push(h("span", { class: "cw-tag", text: "Last " + d.window.hours + " h" }));
    if (d.confidence && d.source !== "RULES") tags.push(h("span", { class: "cw-tag", text: "Confidence: " + d.confidence }));
    if (tags.length) wrap.appendChild(h("div", { class: "cw-meta" }, tags));

    // evidence
    if (sources.length) {
      const ul = h("ul", { dir, lang });
      sources.forEach(s => ul.appendChild(h("li", { "data-ref": s.ref }, h("b", { text: s.ref }), s.text)));
      const ev = h("details", { class: "cw-ev" }, h("summary", { text: "Evidence (" + sources.length + ")" }), ul);
      const notes = (g.rejected_answers || []).map(r => "Attempt " + r.attempt + " rejected: " + r.violations.join("; "));
      if (g.llm_error) notes.push("Model: " + g.llm_error);
      if (notes.length) {
        const n = h("div", { class: "cw-notes" }, h("b", { text: "Validation notes" }));
        notes.forEach(t => n.appendChild(h("div", { text: t })));
        ev.appendChild(n);
      }
      wrap.appendChild(ev);
    }
    return wrap;
  }

  function errorNode(text, retryText) {
    const bubble = h("div", { class: "cw-bubble", role: "alert", dir: "auto", text });
    if (retryText) {
      const retry = h("button", { class: "cw-retry", type: "button" }, "Try again");
      retry.addEventListener("click", () => {
        const prev = wrapper.previousElementSibling;
        if (prev && prev.classList.contains("cw-user")) prev.remove();   // ask() adds the question again
        wrapper.remove();
        ask(retryText);
      });
      bubble.appendChild(h("div", {}, retry));
    }
    const wrapper = h("div", { class: "cw-msg cw-error" }, bubble);
    return wrapper;
  }

  function welcomeNode() {
    const chips = h("div", { class: "cw-chips" });
    questions.forEach(q => {
      const c = h("button", { class: "cw-chip", type: "button", text: q });
      c.addEventListener("click", () => ask(q));
      chips.appendChild(c);
    });
    return h("div", { class: "cw-msg cw-bot cw-welcome" },
      h("div", { class: "cw-bubble", text: "Hi! Ask me about the building: status, faults, cameras, network, repair priorities or energy. I only answer from the database and I'll say when the data isn't enough." }),
      chips);
  }

  function renderAll() {
    log.textContent = "";
    if (!messages.length) { log.appendChild(welcomeNode()); return; }
    messages.forEach(m => log.appendChild(m.role === "user" ? userNode(m.text) : botNode(m.data)));
    scrollToBottom();
  }

  function removeWelcome() {
    const w = log.querySelector(".cw-welcome");
    if (w) w.remove();
  }

  // ---------- typing indicator ----------
  let typingEl = null, slowTimer = null;
  function showTyping() {
    const note = h("span", { text: "" });
    typingEl = h("div", { class: "cw-msg cw-bot cw-typing", "aria-label": "The assistant is working" },
      h("div", { class: "cw-bubble" }, h("i"), h("i"), h("i"), note));
    log.appendChild(typingEl);
    scrollToBottom();
    slowTimer = setTimeout(() => { note.textContent = "Still working, the local model can take a moment…"; }, SLOW_NOTE_MS);
  }
  function hideTyping() {
    clearTimeout(slowTimer);
    if (typingEl) { typingEl.remove(); typingEl = null; }
  }

  // Grow/shrink the textarea to fit its content (max ~120px)
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  // ---------- sending ----------
  function setBusy(v) {
    busy = v;
    input.disabled = v;
    updateSend();
  }
  function updateSend() { send.disabled = busy || input.value.trim().length < 2; }

  function recentTurns() {
    return messages
      .slice(-10)
      .filter(m => (m.text || m.data && m.data.answer))
      .map(m => ({ role: m.role, text: m.role === "user" ? m.text : (m.data && m.data.answer ? m.data.answer : "") }));
  }

  function buildRequestBody(text, hoursValue) {
    const body = { message: text };
    if (hoursValue) body.hours = Number(hoursValue);
    body.history = recentTurns();
    body.session_state = { ...chatState };
    return body;
  }

  function mergeSessionState(data) {
    if (!data || typeof data !== "object") return;
    const next = data.session_state || {};
    chatState = { ...chatState, ...next };
    saveState();
  }

  async function ask(text) {
    text = String(text || "").trim();
    if (text.length < 2 || busy) return;

    removeWelcome();
    messages.push({ role: "user", text });
    log.appendChild(userNode(text));
    input.value = "";
    autosize();
    setBusy(true);
    showTyping();

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const body = buildRequestBody(text, win.value);
      const res = await fetch(API.chat, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), signal: ctrl.signal
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
      if (!res.ok) throw new Error(detailText(data, res.status));
      if (!data || typeof data.answer !== "string") throw new Error("The assistant returned an unexpected response.");

      mergeSessionState(data);

      hideTyping();
      messages.push({ role: "bot", data });
      const node = botNode(data);
      log.appendChild(node);
      scrollToStartOf(node);
      if (data.window) win.value = data.window.hours;
    } catch (e) {
      hideTyping();
      log.appendChild(errorNode(e.message, text));
    } finally {
      setBusy(false);
      save();
    }
  }

  // ---------- events ----------
  input.addEventListener("input", () => { updateSend(); autosize(); });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.ctrlKey) { e.preventDefault(); send.click(); }
  });
  send.addEventListener("click", () => { ask(input.value); });

  btnReset.addEventListener("click", () => {
    messages = [];
    save();
    log.textContent = "";
    log.appendChild(welcomeNode());
    scrollToBottom();
  });
  btnClose.addEventListener("click", () => {
    panel.hidden = true;
    fab.classList.remove("cw-active");
    setBusy(false);
  });

  fab.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    fab.classList.toggle("cw-active");
    if (!panel.hidden) { load(); renderAll(); scrollToBottom(); }
  });

  // ---------- init ----------
  document.body.classList.add("cw-ready");
  panel.hidden = true;
  fab.classList.remove("cw-active");
  saveState();
})();