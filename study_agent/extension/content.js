// Canvasページ内で実行されるスクリプト。
//
// できること:
//   1. シラバスページを検知 → 本文を抽出 → 許可されていればサーバーへ送信
//   2. 練習問題(記述式)ページを検知 → 各設問を抽出 → 許可されていれば
//      サーバーから解答を取得し、解答欄に「下書きとして入力」
//
// 絶対にしないこと:
//   提出/保存ボタンを探す・クリックする処理は、このファイルのどこにも実装しない。
//   トグルをどう設定しても、この一線だけは変わらない。

(async function main() {
  const state = await chrome.storage.sync.get(
    { syncSyllabus: false, autoFillAnswers: false, token: "" }
  );
  if (!state.token) return; // 未接続なら何もしない

  const courseName = guessCourseName();

  if (state.syncSyllabus && isSyllabusPage()) {
    await handleSyllabusPage(courseName);
  }
  if (state.autoFillAnswers && isPracticeQuestionPage()) {
    await handlePracticeQuestions(courseName);
  }
})();

// ------------------------------------------------------------ ページ判定
function isSyllabusPage() {
  return /\/courses\/\d+\/assignments\/syllabus/.test(location.pathname);
}

function isPracticeQuestionPage() {
  // Classic Quizzes の記述式問題を想定した簡易検知。
  // Canvasのテーマ/新Quizzes(別オリジンのiframe)では要調整。
  return document.querySelectorAll(".question_holder, .question").length > 0;
}

function guessCourseName() {
  const crumb = document.querySelector("#breadcrumbs a[href*='/courses/']");
  return crumb ? crumb.textContent.trim() : document.title.split(":")[0].trim();
}

// ---------------------------------------------------------------- シラバス
async function handleSyllabusPage(courseName) {
  const el = document.querySelector("#course_syllabus, .syllabus, #content");
  if (!el) return;
  const text = el.innerText.trim();
  if (!text) return;

  chrome.runtime.sendMessage(
    { action: "syncSyllabus", courseName, text },
    (res) => {
      if (res && res.ok) {
        showBanner("✅ シラバスを Study Agent に同期しました");
      } else {
        showBanner("⚠ シラバス同期に失敗: " + (res && res.error));
      }
    }
  );
}

// ------------------------------------------------------------ 練習問題
async function handlePracticeQuestions(courseName) {
  const blocks = document.querySelectorAll(".question_holder, .question");
  for (const block of blocks) {
    const qTextEl = block.querySelector(".question_text, .text");
    const answerEl = findAnswerField(block);
    if (!qTextEl || !answerEl) continue;
    if (getAnswerValue(answerEl).trim()) continue; // 既に何か入っているものは触らない

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
  // textarea(素のフォーム)を優先。TinyMCEのリッチテキストは
  // contenteditable の iframe/divで、ページ構造がテーマ次第で変わるため
  // 見つかった場合のみ対応する。
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

// ------------------------------------------------------------------ UI
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
