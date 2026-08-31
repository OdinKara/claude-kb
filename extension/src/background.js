/* Service worker: the bridge between the page and the native host.
 *
 * Native messaging is not reachable from a content script, and the popup can be
 * dismissed mid-flight, so the round trip lives here.
 */

const HOST_NAME = "com.claude_kb.export";

function sendNative(message) {
  return new Promise((resolve) => {
    let settled = false;
    try {
      chrome.runtime.sendNativeMessage(HOST_NAME, message, (reply) => {
        if (settled) return;
        settled = true;
        const err = chrome.runtime.lastError;
        if (err) {
          // "Specified native messaging host not found" is by far the most
          // common case, and it is a setup problem rather than a bug - say so
          // instead of surfacing the raw string.
          const msg = String(err.message || err);
          resolve({
            ok: false,
            status: "no_host",
            message:
              "The native host did not answer (" +
              msg +
              "). Run native/install-host.ps1 with this extension's ID, then reload.",
          });
          return;
        }
        if (!reply || typeof reply !== "object") {
          resolve({
            ok: false,
            status: "error",
            message: "The native host returned nothing usable.",
          });
          return;
        }
        resolve(reply);
      });
    } catch (e) {
      if (!settled) {
        settled = true;
        resolve({ ok: false, status: "error", message: String(e && e.message) || String(e) });
      }
    }
  });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

async function captureFromTab(tab) {
  try {
    return await chrome.tabs.sendMessage(tab.id, { type: "capture" });
  } catch (e) {
    return {
      ok: false,
      kind: "no_content_script",
      detail:
        "The page did not respond. Open a claude.ai conversation and reload the tab.",
    };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "ping_host") {
    sendNative({ type: "ping" }).then(sendResponse);
    return true;
  }

  if (msg.type === "capture_and_ingest" || msg.type === "capture_only") {
    (async () => {
      const tab = await activeTab();
      if (!tab || !/^https:\/\/claude\.ai\//i.test(tab.url || "")) {
        sendResponse({
          ok: false,
          status: "error",
          message: "Open a claude.ai conversation in this tab first.",
        });
        return;
      }

      const cap = await captureFromTab(tab);
      if (!cap || !cap.ok) {
        sendResponse({
          ok: false,
          status: "capture_failed",
          kind: (cap && cap.kind) || "unknown",
          message: (cap && cap.detail) || "Capture failed for an unknown reason.",
        });
        return;
      }

      const reply = await sendNative({
        type: msg.type === "capture_only" ? "save" : "save_and_ingest",
        files: [{ name: cap.filename, content: cap.content }],
      });
      reply.captured = {
        title: cap.title,
        uuid: cap.uuid,
        messages: cap.messages,
        filename: cap.filename,
      };
      sendResponse(reply);
    })();
    return true;
  }

  return false;
});
