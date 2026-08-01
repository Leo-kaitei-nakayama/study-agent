// ページの隅に出る引き出し。「参考の下書きを作る」を押すためのUI。
//
// content.js から読み込まれ、Canvas / 埋め込みLTI / 外部サイト のどれでも動く。
// このファイルがやることは 3 つだけ:
//
//   1. いま見ているページがどの種類かを見分ける (detectContext)
//   2. そのページの本文を読む                  (readPageText)
//   3. background 経由でサーバーに送り、返ってきた下書きを引き出しに出す
//
// **押さないと何も起きない。** 開いただけでは送信しないし、提出ボタンは
// 探しにも行かない。作るのは下書きまでで、出すかどうかは本人が決める。

(function () {
  if (window.__studyAgentDrawer) return;   // 二重に差し込まれても1つだけ
  window.__studyAgentDrawer = true;

  const CANVAS_HOSTS = ["canvas.eee.uci.edu", "instructure.com"];
  const PLATFORMS = {
    "gradescope.com": "Gradescope", "zybooks.com": "zyBooks",
    "pearson.com": "Pearson", "prairielearn.org": "PrairieLearn",
    "prairielearn.com": "PrairieLearn", "perusall.com": "Perusall",
    "edstem.org": "Ed", "webassign.net": "WebAssign",
    "mheducation.com": "McGraw Hill", "cengage.com": "Cengage",
  };

  // ------------------------------------------------------------ 1. 見分ける
  function hostOf(url) {
    try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
  }

  function isCanvasHost(host) {
    return CANVAS_HOSTS.some((h) => host === h || host.endsWith("." + h));
  }

  function platformName(host) {
    for (const d in PLATFORMS) {
      if (host === d || host.endsWith("." + d)) return PLATFORMS[d];
    }
    return host;
  }

  // router.py の detect_context() と同じ判定。サーバー側でも上書きされるが、
  // 引き出しの見出しに出すためにこちらでも出しておく。
  function detectContext() {
    const host = hostOf(location.href);
    const inFrame = window.top !== window.self;
    if (isCanvasHost(host)) {
      return /\/external_tools?\/|\/lti\/|\/modules\/items\//.test(location.pathname)
        ? "EMBEDDED_LTI" : "CANVAS_NATIVE";
    }
    return inFrame ? "EMBEDDED_LTI" : "EXTERNAL_PLATFORM";
  }

  // ------------------------------------------------------------ 2. 本文を読む
  // 課題文が入っていそうな場所を順に試し、いちばん中身のあるものを使う。
  // 見つからなければ body 全体から、明らかに関係ない部分を除いて拾う。
  const CONTENT_SELECTORS = [
    ".description", ".assignment-description", "#assignment_show",
    ".show-content", ".user_content", ".question_text",
    "main", "[role=main]", "#content", ".content",
  ];
  const STRIP = "nav, header, footer, script, style, noscript, " +
                "#header, .navigation, .ic-app-header, [role=navigation]";

  function readPageText() {
    let best = "";
    for (const sel of CONTENT_SELECTORS) {
      for (const el of document.querySelectorAll(sel)) {
        const t = cleanText(el);
        if (t.length > best.length) best = t;
      }
      if (best.length > 400) break;      // 十分な量が取れたら打ち切り
    }
    if (best.length < 80) best = cleanText(document.body);
    return best.slice(0, 20000);
  }

  function cleanText(el) {
    if (!el) return "";
    // 元のページを壊さないよう複製してから余計な要素を落とす
    const copy = el.cloneNode(true);
    copy.querySelectorAll(STRIP).forEach((n) => n.remove());
    return (copy.innerText || copy.textContent || "")
      .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  }

  function guessAssignmentName() {
    const h = document.querySelector("h1.title, h1, .assignment-title, .title");
    const t = h && h.textContent.trim();
    return (t || document.title.split("|")[0] || "").trim().slice(0, 200);
  }

  function guessCourseName() {
    const crumb = document.querySelector("#breadcrumbs a[href*='/courses/']");
    if (crumb) return crumb.textContent.trim();
    const m = document.title.split(":");
    return m.length > 1 ? m[0].trim() : "";
  }

  // ------------------------------------------------------------ 3. 引き出し
  let panel, bodyEl, btn, statusEl;

  function buildDrawer() {
    const host = document.createElement("div");
    host.id = "study-agent-drawer";
    // Shadow DOM に入れて、ページ側の CSS と混ざらないようにする
    const root = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = `
      :host { all: initial; }
      .tab {
        position: fixed; right: 0; bottom: 90px; z-index: 2147483646;
        background: #185fa5; color: #fff; font: 600 12px -apple-system, sans-serif;
        padding: 9px 12px; border-radius: 8px 0 0 8px; cursor: pointer;
        box-shadow: 0 2px 10px rgba(0,0,0,.2); border: none;
      }
      .panel {
        position: fixed; right: 0; bottom: 0; top: 0; width: 380px; max-width: 92vw;
        z-index: 2147483647; background: #faf9f6; color: #1f1e1a;
        font: 13px/1.6 -apple-system, sans-serif; box-shadow: -2px 0 18px rgba(0,0,0,.18);
        display: flex; flex-direction: column; transform: translateX(100%);
        transition: transform .18s ease;
      }
      .panel.open { transform: translateX(0); }
      .head { display: flex; justify-content: space-between; align-items: center;
              padding: 12px 14px; border-bottom: 1px solid #e5e2da; background: #fff; }
      .head b { font-size: 13px; }
      .head .x { cursor: pointer; color: #6b6960; font-size: 18px; line-height: 1;
                 background: none; border: none; }
      .meta { font-size: 11px; color: #6b6960; padding: 8px 14px;
              border-bottom: 1px solid #efece4; }
      .meta code { background: #eef2f7; border-radius: 4px; padding: 1px 5px; }
      .actions { padding: 12px 14px; border-bottom: 1px solid #efece4; }
      .actions button { width: 100%; padding: 9px; font: 600 13px -apple-system, sans-serif;
        background: #185fa5; color: #fff; border: none; border-radius: 8px; cursor: pointer; }
      .actions button:disabled { opacity: .5; cursor: default; }
      .note { font-size: 11px; color: #6b6960; margin-top: 8px; }
      .body { flex: 1; overflow-y: auto; padding: 14px; white-space: pre-wrap;
              font-size: 12.5px; }
      .status { padding: 8px 14px; font-size: 11px; color: #6b6960;
                border-top: 1px solid #efece4; background: #fff; }
      .status.err { color: #a52121; }
    `;

    const tab = document.createElement("button");
    tab.className = "tab";
    tab.textContent = "Study Agent";
    tab.addEventListener("click", () => panel.classList.toggle("open"));

    panel = document.createElement("div");
    panel.className = "panel";

    const head = document.createElement("div");
    head.className = "head";
    const title = document.createElement("b");
    title.textContent = "Reference draft";
    const close = document.createElement("button");
    close.className = "x";
    close.textContent = "×";
    close.addEventListener("click", () => panel.classList.remove("open"));
    head.appendChild(title);
    head.appendChild(close);

    const meta = document.createElement("div");
    meta.className = "meta";
    const ctx = detectContext();
    const label = ctx === "CANVAS_NATIVE" ? "Canvas"
                : ctx === "EMBEDDED_LTI" ? "embedded tool"
                : platformName(hostOf(location.href));
    meta.innerHTML = `Reading this page · <code>${escapeHtml(label)}</code>`;

    const actions = document.createElement("div");
    actions.className = "actions";
    btn = document.createElement("button");
    btn.textContent = "Generate reference draft";
    btn.addEventListener("click", generate);
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = "Reads the text on this page only. A draft for you to "
                     + "review — it is never submitted.";
    actions.appendChild(btn);
    actions.appendChild(note);

    bodyEl = document.createElement("div");
    bodyEl.className = "body";

    statusEl = document.createElement("div");
    statusEl.className = "status";
    statusEl.textContent = "Ready.";

    panel.appendChild(head);
    panel.appendChild(meta);
    panel.appendChild(actions);
    panel.appendChild(bodyEl);
    panel.appendChild(statusEl);

    root.appendChild(style);
    root.appendChild(tab);
    root.appendChild(panel);
    document.documentElement.appendChild(host);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;" }[c]));
  }

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.className = "status" + (isError ? " err" : "");
  }

  function generate() {
    const pageText = readPageText();
    if (pageText.length < 40) {
      setStatus("Could not find any assignment text on this page.", true);
      return;
    }

    btn.disabled = true;
    bodyEl.textContent = "";
    setStatus("Working…");

    chrome.runtime.sendMessage({
      action: "generateDraft",
      payload: {
        url: location.href,
        in_iframe: window.top !== window.self,
        context: detectContext(),
        page_text: pageText,
        course_name: guessCourseName(),
        assignment_name: guessAssignmentName(),
      },
    }, (res) => {
      btn.disabled = false;
      if (chrome.runtime.lastError) {
        setStatus(chrome.runtime.lastError.message, true);
        return;
      }
      if (!res || !res.ok) {
        setStatus((res && res.error) || "Something went wrong.", true);
        return;
      }
      bodyEl.textContent = res.data.draft || "(empty reply)";
      setStatus(`Saved to your notes · read from ${res.data.source}`);
    });
  }

  buildDrawer();
})();
