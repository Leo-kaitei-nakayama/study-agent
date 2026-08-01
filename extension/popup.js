// Toggles, term selection, token, manual sync, and live sync progress.
// Everything defaults to off — nothing happens until the user opts in.

const DEFAULTS = { syncSyllabus: false, autoFillAnswers: false, token: "",
                   selectedTerm: "" };

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

function loadState() {
  chrome.storage.sync.get(DEFAULTS, (state) => {
    document.getElementById("syncSyllabus").checked = state.syncSyllabus;
    document.getElementById("autoFillAnswers").checked = state.autoFillAnswers;
    document.getElementById("token").value = state.token || "";
    if (state.token) { loadCourses(); loadTerms(state.selectedTerm); }
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

// -------------------------------------------------------------------- terms
// Pull the term list from Canvas. If today falls inside a term, mark it current.
function loadTerms(currentValue) {
  const sel = document.getElementById("selectedTerm");
  chrome.runtime.sendMessage({ action: "getTerms" }, (res) => {
    sel.textContent = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All terms";
    sel.appendChild(all);

    if (chrome.runtime.lastError || !res || !res.ok) {
      sel.value = currentValue || "";
      return;
    }
    const terms = (res.terms || []).sort((a, b) => b.name.localeCompare(a.name));
    for (const t of terms) {
      const opt = document.createElement("option");
      opt.value = t.name;
      opt.textContent = `${t.name} (${t.count})` + (t.isCurrent ? " · current" : "");
      sel.appendChild(opt);
    }
    if (currentValue) {
      sel.value = currentValue;
    } else {
      const cur = terms.find((t) => t.isCurrent);
      if (cur) sel.value = cur.name;   // suggested only; saved when you hit Save
    }
  });
}

// ------------------------------------------------------------------ actions
function saveState() {
  const state = {
    syncSyllabus: document.getElementById("syncSyllabus").checked,
    autoFillAnswers: document.getElementById("autoFillAnswers").checked,
    token: document.getElementById("token").value.trim(),
    selectedTerm: document.getElementById("selectedTerm").value,
  };
  chrome.storage.sync.set(state, () => {
    setStatus("Saved");
    setTimeout(loadState, 1200);
  });
}

function runAction(action, busyText) {
  setStatus(busyText);
  document.getElementById("syncNow").disabled = true;
  document.getElementById("recrawl").disabled = true;
  chrome.runtime.sendMessage({ action }, (res) => {
    document.getElementById("syncNow").disabled = false;
    document.getElementById("recrawl").disabled = false;
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
document.getElementById("syncNow").addEventListener("click",
  () => runAction("syncNow", "Syncing..."));
document.getElementById("recrawl").addEventListener("click",
  () => runAction("forceRecrawl", "Re-fetching course list..."));

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
