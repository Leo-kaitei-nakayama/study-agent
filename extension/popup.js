// トグル・トークン・手動同期・ページ取り込みのUI。
// デフォルトは全てオフ ― ユーザーが明示的にオンにしない限り何もしない。

const DEFAULTS = { syncSyllabus: false, autoFillAnswers: false, token: "" };

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

function loadState() {
  chrome.storage.sync.get(DEFAULTS, (state) => {
    document.getElementById("syncSyllabus").checked = state.syncSyllabus;
    document.getElementById("autoFillAnswers").checked = state.autoFillAnswers;
    document.getElementById("token").value = state.token || "";
    if (state.token) loadCourses();
  });
  chrome.storage.local.get({ lastStatus: "" }, (s) => {
    setStatus(s.lastStatus || "まだ同期していません");
  });
}

function loadCourses() {
  chrome.runtime.sendMessage({ action: "getCourses" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) return;
    const sel = document.getElementById("captureCourse");
    for (const name of res.courses) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    renderPendingLinks(res.pendingLinks || []);
  });
}

// シラバス本体が外部にある科目を一覧表示。クリックでそのページを開く。
function renderPendingLinks(links) {
  const thin = links.filter((l) => l.is_thin_syllabus);
  const wrap = document.getElementById("pendingWrap");
  const list = document.getElementById("pendingList");
  if (thin.length === 0) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  list.textContent = "";
  for (const l of thin) {
    const row = document.createElement("div");
    row.className = "pending";
    const c = document.createElement("div");
    c.className = "c";
    c.textContent = (l.course || "科目不明") + " — " + (l.label || "シラバス");
    const u = document.createElement("div");
    u.className = "u";
    u.textContent = l.url;
    row.appendChild(c);
    row.appendChild(u);
    row.addEventListener("click", () => {
      // そのページを開き、科目を選択済みにしておく
      chrome.tabs.create({ url: l.url });
      if (l.course) {
        const sel = document.getElementById("captureCourse");
        for (const opt of sel.options) {
          if (opt.value === l.course) { sel.value = l.course; break; }
        }
      }
      setStatus("ページを開きました。読み込み後に「このページを取り込む」を押してください。");
    });
    list.appendChild(row);
  }
}

function saveState() {
  const state = {
    syncSyllabus: document.getElementById("syncSyllabus").checked,
    autoFillAnswers: document.getElementById("autoFillAnswers").checked,
    token: document.getElementById("token").value.trim(),
  };
  chrome.storage.sync.set(state, () => {
    setStatus("保存しました");
    setTimeout(loadState, 1200);
  });
}

function syncNow() {
  setStatus("同期中...");
  chrome.runtime.sendMessage({ action: "syncNow" }, (res) => {
    if (chrome.runtime.lastError) {
      setStatus("エラー: " + chrome.runtime.lastError.message);
    } else if (res && res.ok) {
      setStatus(`✅ ${res.courses} 科目を同期しました`);
    } else {
      setStatus("失敗: " + (res && res.error));
    }
  });
}

// ページから本文を抜き出す関数。activeTab 権限のもと、
// ユーザーがボタンを押した「今のタブ」でのみ実行される。
function extractPageContent() {
  const drop = ["SCRIPT", "STYLE", "NAV", "HEADER", "FOOTER", "ASIDE", "NOINSCRIPT"];
  const root = document.querySelector("article, main, #content, .content") || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll(drop.join(",").toLowerCase()).forEach((el) => el.remove());
  // textContent なので、折りたたまれて非表示の部分も拾える
  const text = (clone.textContent || "").replace(/[ \t]+/g, " ")
                 .replace(/\n\s*\n\s*\n+/g, "\n\n").trim();
  return { url: location.href, title: document.title, text };
}

async function capturePage() {
  setStatus("ページを読み取り中...");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) { setStatus("タブが見つかりません"); return; }

    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractPageContent,
    });
    const payload = results && results[0] && results[0].result;
    if (!payload || !payload.text) {
      setStatus("このページからテキストを取得できませんでした");
      return;
    }

    setStatus(`送信中... (${payload.text.length.toLocaleString()} 文字)`);
    chrome.runtime.sendMessage({
      action: "capturePage",
      url: payload.url,
      title: payload.title,
      text: payload.text,
      courseName: document.getElementById("captureCourse").value,
      kind: document.getElementById("captureKind").value,
    }, (res) => {
      if (chrome.runtime.lastError) {
        setStatus("エラー: " + chrome.runtime.lastError.message);
      } else if (res && res.ok) {
        const extra = res.data && res.data.resolved_pending_link
          ? " — 未対応リストから外しました" : "";
        setStatus(`✅ 取り込みました (${payload.text.length.toLocaleString()} 文字)${extra}`);
        loadCourses();
      } else {
        setStatus("失敗: " + (res && res.error));
      }
    });
  } catch (e) {
    setStatus("失敗: " + String(e.message || e));
  }
}

document.addEventListener("DOMContentLoaded", loadState);
document.getElementById("save").addEventListener("click", saveState);
document.getElementById("syncNow").addEventListener("click", syncNow);
document.getElementById("recrawl").addEventListener("click", () => {
  setStatus("科目リストを再取得中...");
  chrome.runtime.sendMessage({ action: "forceRecrawl" }, (res) => {
    if (res && res.ok) {
      setStatus(`✅ ${res.courses} 科目を再取得しました`);
      loadCourses();
    } else {
      setStatus("失敗: " + (res && res.error));
    }
  });
});
document.getElementById("capturePage").addEventListener("click", capturePage);