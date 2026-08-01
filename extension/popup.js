// Toggles, term selection, token, manual sync, and live sync progress.
// Everything defaults to off — nothing happens until the user opts in.

const DEFAULTS = { syncSyllabus: false, autoFillAnswers: false, token: "",
                   selectedCourseIds: [] };

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

function loadState() {
  chrome.storage.sync.get(DEFAULTS, (state) => {
    document.getElementById("syncSyllabus").checked = state.syncSyllabus;
    document.getElementById("autoFillAnswers").checked = state.autoFillAnswers;
    document.getElementById("token").value = state.token || "";
    if (state.token) { loadCourses(); loadPicker(state.selectedCourseIds); }
  });
  chrome.storage.local.get({ lastStatus: "", syncProgress: null }, (s) => {
    setStatus(s.lastStatus || "Not synced yet");
    renderProgress(s.syncProgress);   // reopening mid-sync still shows progress
  });
}

// ------------------------------------------------------- live sync progress
const ICONS = { pending: "○", running: "◍", done: "✓", error: "✕", skipped: "–" };

function renderProgress(p) {
  const wrap = document.getElementById("progressWrap");
  if (!p || !p.courses || p.courses.length === 0) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";

  document.getElementById("progressMode").textContent =
    p.running ? (p.mode || "Syncing") : (p.mode || "Sync") + " — finished";
  document.getElementById("progressCount").textContent =
    `${p.completed || 0} / ${p.total || p.courses.length}`;

  const pct = p.total ? Math.round(((p.completed || 0) / p.total) * 100) : 0;
  document.getElementById("progressBar").style.width = pct + "%";

  const list = document.getElementById("progressList");
  list.textContent = "";
  for (const c of p.courses) {
    const row = document.createElement("div");
    row.className = "prow" + (c.status === "error" ? " err" : "");

    const ic = document.createElement("span");
    ic.className = "ic" + (c.status === "running" ? " spin" : "");
    ic.textContent = ICONS[c.status] || "○";

    const right = document.createElement("div");
    right.style.flex = "1";
    const nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = c.name;
    right.appendChild(nm);
    if (c.detail) {
      const dt = document.createElement("div");
      dt.className = "dt";
      dt.textContent = c.detail;
      right.appendChild(dt);
    }

    row.appendChild(ic);
    row.appendChild(right);
    list.appendChild(row);
  }
}

// background.js writes progress to storage.local; mirror it live here
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.syncProgress) renderProgress(changes.syncProgress.newValue);
  if (changes.lastStatus) setStatus(changes.lastStatus.newValue || "");
});

// ------------------------------------------------------------------ courses
function loadCourses() {
  chrome.runtime.sendMessage({ action: "getCourses" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) return;
    renderPendingLinks(res.pendingLinks || []);
  });
}

// Courses whose syllabus really lives on an external site.
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
    c.textContent = (l.course || "Unknown course") + " — " + (l.label || "syllabus");
    const u = document.createElement("div");
    u.className = "u";
    u.textContent = l.url;
    row.appendChild(c);
    row.appendChild(u);
    row.addEventListener("click", () => {
      chrome.tabs.create({ url: l.url });
      setStatus("Opened the syllabus page.");
    });
    list.appendChild(row);
  }
}

// ------------------------------------------------------------------ picker
// どの科目を同期するかは **本人が選ぶ**。
//
// 以前は Canvas の「学期」で自動的に絞ろうとしていたが、うまくいかなかった。
// Canvas はオリエンテーション用のスペースや、年をまたぐ研修コースを、普通の
// 授業とまったく同じ形で返してくる。学期名が付いていないものも多く、名前を
// 見ても何年のものか分からない。だからここで一覧に出して選んでもらう。
let canvasCourses = [];

function loadPicker(selectedIds) {
  const list = document.getElementById("courseList");
  list.textContent = "";
  list.appendChild(makeEmpty("Loading your Canvas courses…"));

  chrome.runtime.sendMessage({ action: "listCanvasCourses" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) {
      list.textContent = "";
      list.appendChild(makeEmpty(
        (res && res.error) || "Could not read your Canvas courses. Open Canvas and sign in."));
      return;
    }
    canvasCourses = res.courses || [];
    renderPicker(new Set((selectedIds || []).map(String)));
  });
}

function makeEmpty(text) {
  const d = document.createElement("div");
  d.className = "pickempty";
  d.textContent = text;
  return d;
}

function renderPicker(checkedIds) {
  const list = document.getElementById("courseList");
  list.textContent = "";
  if (canvasCourses.length === 0) {
    list.appendChild(makeEmpty("No active courses found in Canvas."));
    return;
  }
  for (const c of canvasCourses) {
    const row = document.createElement("label");
    row.className = "pick";

    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(c.id);
    box.checked = checkedIds.has(String(c.id));
    box.addEventListener("change", () => {
      row.classList.toggle("is-on", box.checked);
    });

    const right = document.createElement("div");
    const nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = c.name;
    right.appendChild(nm);
    // 学期は「絞る条件」ではなく、見分けるための手がかりとして出す
    const tm = document.createElement("div");
    tm.className = "tm";
    tm.textContent = c.term || "(no term)";
    right.appendChild(tm);

    row.classList.toggle("is-on", box.checked);
    row.appendChild(box);
    row.appendChild(right);
    list.appendChild(row);
  }
}

function pickedIds() {
  return Array.from(
    document.querySelectorAll("#courseList input[type=checkbox]:checked"))
    .map((b) => Number(b.value))
    .filter((n) => !Number.isNaN(n));
}

function setAllPicked(on) {
  for (const b of document.querySelectorAll("#courseList input[type=checkbox]")) {
    b.checked = on;
    b.closest(".pick").classList.toggle("is-on", on);
  }
}

document.getElementById("pickAll").addEventListener("click", (e) => {
  e.preventDefault(); setAllPicked(true);
});
document.getElementById("pickNone").addEventListener("click", (e) => {
  e.preventDefault(); setAllPicked(false);
});
document.getElementById("pickReload").addEventListener("click", (e) => {
  e.preventDefault();
  loadPicker(pickedIds());     // いま選んでいるものは保ったまま取り直す
});

// ------------------------------------------------------------------ actions
function saveState() {
  const state = {
    syncSyllabus: document.getElementById("syncSyllabus").checked,
    autoFillAnswers: document.getElementById("autoFillAnswers").checked,
    token: document.getElementById("token").value.trim(),
    selectedCourseIds: pickedIds(),
  };
  chrome.storage.sync.set(state, () => {
    setStatus("Saved");
    setTimeout(loadState, 1200);
  });
}

// Sync exactly what is ticked on screen, even if Save was never pressed.
// If the ticks differ from what is stored, saving them is enough: background.js
// watches storage, drops the cached course list and re-crawls on its own — so we
// must not also send the message, or two syncs would run over each other.
function runAction(action, busyText) {
  const picked = pickedIds();
  chrome.storage.sync.get({ selectedCourseIds: [] }, (s) => {
    const before = (s.selectedCourseIds || []).map(Number).sort().join(",");
    const now = picked.slice().sort().join(",");
    if (before !== now) {
      setStatus("Course selection changed — re-fetching…");
      chrome.storage.sync.set({ selectedCourseIds: picked });
      return;
    }
    if (picked.length === 0) {
      setStatus("Tick at least one course first.");
      return;
    }
    sendAction(action, busyText);
  });
}

function sendAction(action, busyText) {
  setStatus(busyText);
  document.getElementById("syncNow").disabled = true;
  chrome.runtime.sendMessage({ action }, (res) => {
    document.getElementById("syncNow").disabled = false;
    if (chrome.runtime.lastError) {
      setStatus("Error: " + chrome.runtime.lastError.message);
    } else if (res && res.ok) {
      setStatus(`✓ Synced ${res.courses} course(s)`);
      loadCourses();
    } else {
      setStatus("Failed: " + (res && res.error));
    }
  });
}

document.addEventListener("DOMContentLoaded", loadState);
document.getElementById("save").addEventListener("click", saveState);
// 同期の入口はこの1つだけ。フルクロールが要るかどうかは needsFullCrawl()
// が自分で判断するので、「取り直す」ボタンを分ける必要がない。
document.getElementById("syncNow").addEventListener("click",
  () => runAction("syncNow", "Syncing..."));

// ---------------------------------------------- スクリーンショットで質問する
// 「Other」を選んだときだけ自由入力欄を出す。
function selectedShotMode() {
  const el = document.querySelector("input[name=shotMode]:checked");
  return el ? el.value : "explanation";
}

document.getElementById("shotModes").addEventListener("change", () => {
  document.getElementById("shotPrompt").style.display =
    selectedShotMode() === "other" ? "block" : "none";
});

document.getElementById("shotGo").addEventListener("click", () => {
  const btn = document.getElementById("shotGo");
  const out = document.getElementById("shotResult");
  const mode = selectedShotMode();
  const prompt = document.getElementById("shotPrompt").value.trim();

  if (mode === "other" && !prompt) {
    out.style.display = "block";
    out.className = "shot-result is-error";
    out.textContent = "Type what you want the agent to do with the screenshot.";
    return;
  }

  btn.disabled = true;
  out.style.display = "block";
  out.className = "shot-result";
  out.textContent = "Taking a screenshot…";

  chrome.runtime.sendMessage(
    { action: "screenshot", mode, prompt,
      courseName: document.getElementById("courseName")?.value || "" },
    (res) => {
      btn.disabled = false;
      if (chrome.runtime.lastError) {
        out.className = "shot-result is-error";
        out.textContent = chrome.runtime.lastError.message;
        return;
      }
      if (!res || !res.ok) {
        out.className = "shot-result is-error";
        out.textContent = (res && res.error) || "Something went wrong.";
        return;
      }
      out.className = "shot-result";
      out.textContent = res.data.answer || "(empty reply)";
    });
});
