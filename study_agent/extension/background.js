// バックグラウンドで実際のサーバー通信を行う (MV3 service worker)。
// Canvasページ自身のCSPに影響されないよう、通信は content.js からではなく
// ここで一括して行う。

const API_BASE = "https://study-agent-500j.onrender.com"; // デプロイ先に合わせて変更

async function apiPost(path, token, body) {
  const resp = await fetch(API_BASE + path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + token,
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`API error ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp.json();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      const { token } = await chrome.storage.sync.get({ token: "" });
      if (!token) throw new Error("トークンが未設定です。拡張機能のポップアップから設定してください。");

      if (msg.action === "syncSyllabus") {
        const data = await apiPost("/api/extension/syllabus", token, {
          course_name: msg.courseName,
          text: msg.text,
        });
        sendResponse({ ok: true, data });
      } else if (msg.action === "getAnswer") {
        const data = await apiPost("/api/extension/answer", token, {
          course_name: msg.courseName,
          question_text: msg.questionText,
        });
        sendResponse({ ok: true, data });
      } else {
        sendResponse({ ok: false, error: "unknown action" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true; // 非同期でsendResponseを使うために必要
});

// Study Agentのサイト(manifest.jsonのexternally_connectableに登録済み)からの
// メッセージのみ受け付ける。他のサイトはこのリスナーを呼び出せない。
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (msg.action === "connectToken" && msg.token) {
    chrome.storage.sync.set({ token: msg.token }, () => {
      sendResponse({ ok: true });
    });
    return true;
  }
  sendResponse({ ok: false, error: "unknown action" });
});