// 権限トグルとトークンを chrome.storage.sync に保存/読込するだけのシンプルな画面。
// デフォルトは全てオフ ― ユーザーが明示的にオンにしない限り何もしない。

const DEFAULTS = { syncSyllabus: false, autoFillAnswers: false, token: "" };

function loadState() {
  chrome.storage.sync.get(DEFAULTS, (state) => {
    document.getElementById("syncSyllabus").checked = state.syncSyllabus;
    document.getElementById("autoFillAnswers").checked = state.autoFillAnswers;
    document.getElementById("token").value = state.token || "";
  });
}

function saveState() {
  const state = {
    syncSyllabus: document.getElementById("syncSyllabus").checked,
    autoFillAnswers: document.getElementById("autoFillAnswers").checked,
    token: document.getElementById("token").value.trim(),
  };
  chrome.storage.sync.set(state, () => {
    document.getElementById("status").textContent = "保存しました";
    setTimeout(() => { document.getElementById("status").textContent = ""; }, 1500);
  });
}

document.addEventListener("DOMContentLoaded", loadState);
document.getElementById("save").addEventListener("click", saveState);