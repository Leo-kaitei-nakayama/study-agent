// Canvasページ内で実行されるスクリプト。
//
// 役割は2つ:
//   1. 練習問題の解答欄への下書き入力
//   2. そのページ上のリンクを検知してサーバーに報告(常時)
//      これにより、学生が普段通りCanvasを見ているだけで、外部サイトへの
//      リンクが自然に集まっていく。ページを開く回数を増やす必要はない。
//
// 絶対にしないこと:
//   提出/保存ボタンを探す・クリックする処理は、このファイルに一切実装しない。
//   トグルをどう設定してもこの一線は変わらない。

(async function main() {
  const state = await chrome.storage.sync.get(
    { autoFillAnswers: false, syncSyllabus: false, token: "" });
  if (!state.token) return;

  if (state.syncSyllabus) scanPageLinks(); // 常時検知(軽量、ページ内のみ)

  if (state.autoFillAnswers && isPracticeQuestionPage()) {
    await handlePracticeQuestions(guessCourseName());
  }
})();

// ------------------------------------------------------- リンクの常時検知
// 今開いているページのDOMからリンクを拾うだけ。他のページには一切アクセスしない。
function scanPageLinks() {
  const courseName = guessCourseName();
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll("a[href]")) {
    let abs;
    try { abs = new URL(a.getAttribute("href"), location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(abs.protocol)) continue;
    if (abs.origin === location.origin && !/\/files\//.test(abs.pathname)) continue; // Canvas内部の通常ページは対象外
    if (seen.has(abs.href)) continue;
    seen.add(abs.href);
    const label = (a.textContent || "").trim().slice(0, 200);
    links.push({ url: abs.href, label: label || abs.hostname });
    if (links.length >= 30) break;
  }
  if (links.length === 0) return;
  chrome.runtime.sendMessage(
    { action: "reportLinks", courseName, links, sourceUrl: location.href },
    () => { /* 失敗しても静かに無視。次のページ訪問でまた拾える */ }
  );
}

function isPracticeQuestionPage() {
  return document.querySelectorAll(".question_holder, .question").length > 0;
}

function guessCourseName() {
  const crumb = document.querySelector("#breadcrumbs a[href*='/courses/']");
  return crumb ? crumb.textContent.trim() : document.title.split(":")[0].trim();
}

async function handlePracticeQuestions(courseName) {
  const blocks = document.querySelectorAll(".question_holder, .question");
  for (const block of blocks) {
    const qTextEl = block.querySelector(".question_text, .text");
    const answerEl = findAnswerField(block);
    if (!qTextEl || !answerEl) continue;
    if (getAnswerValue(answerEl).trim()) continue; // 既に書かれているものは触らない

    const questionText = qTextEl.innerText.trim();
    if (!questionText) continue;

    chrome.runtime.sendMessage(
      { action: "getAnswer", courseName, questionText },
      (res) => {
        if (res && res.ok && res.data && res.data.answer) {
          fillAnswerField(answerEl, res.data.answer);
          showBanner("📝 下書きを入力しました。内容を確認してから提出してください。");
        }
      }
    );
  }
}

function findAnswerField(block) {
  return block.querySelector("textarea") ||
         block.querySelector("[contenteditable='true']");
}

function getAnswerValue(el) {
  if (el.tagName === "TEXTAREA") return el.value;
  return el.innerText || "";
}

function fillAnswerField(el, text) {
  if (el.tagName === "TEXTAREA") {
    el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    el.innerText = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  // 送信/保存ボタンには一切触れない。ここで処理は終わり。
}

function showBanner(message) {
  let banner = document.getElementById("study-agent-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "study-agent-banner";
    banner.style.cssText =
      "position:fixed;bottom:16px;right:16px;z-index:99999;" +
      "background:#185fa5;color:white;padding:10px 16px;border-radius:8px;" +
      "font-size:13px;font-family:sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);";
    document.body.appendChild(banner);
  }
  banner.textContent = message;
  setTimeout(() => banner.remove(), 5000);
}