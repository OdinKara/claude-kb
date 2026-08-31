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
              "). If it worked before, the most likely cause is that this " +
              "extension's folder was moved or renamed: an unpacked extension's " +
              "ID is derived from its path, so moving it changes the ID and the " +
              "host registration no longer matches. Re-run native/install-host.ps1 " +
              "with the ID shown on this extension's card, then reload. " +
              "This extension's ID is " +
              chrome.runtime.id +
              ".",
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

/* The last bulk run's report, kept so closing the popup mid-run does not lose
 * it. A run is paced and can take a minute; losing the account of what landed
 * would defeat the point of reporting per conversation at all. */
async function rememberRun(report) {
  try {
    await chrome.storage.session.set({ lastRun: report });
  } catch (_e) {
    /* storage is a convenience here, never a dependency */
  }
}

/* What the cap deferred, so closing the popup mid-sequence does not lose the
 * thread of a multi-batch run. Only the uuids are kept, not the list itself: a
 * few thousand list rows is not something to push into session storage, and the
 * list can be reloaded cheaply while the selection cannot be reconstructed. */
async function rememberPending(uuids) {
  try {
    if (uuids && uuids.length) {
      await chrome.storage.session.set({
        pending: uuids,
        pendingAt: new Date().toISOString(),
      });
    } else {
      await chrome.storage.session.remove(["pending", "pendingAt"]);
    }
  } catch (_e) {
    /* as above */
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "ping_host") {
    sendNative({ type: "ping" }).then(sendResponse);
    return true;
  }

  if (msg.type === "last_run") {
    chrome.storage.session
      .get(["lastRun", "pending"])
      .then((v) =>
        sendResponse({ lastRun: (v && v.lastRun) || null,
                       pending: (v && v.pending) || [] })
      )
      .catch(() => sendResponse({ lastRun: null, pending: [] }));
    return true;
  }

  // Re-read what is indexed WITHOUT re-listing conversations. After a batch the
  // labels are stale - the 25 just captured now read "new" - and re-paging
  // claude.ai to fix a local display is the wrong trade against endpoints this
  // deliberately paces.
  if (msg.type === "refresh_indexed") {
    sendNative({ type: "indexed" }).then((idx) =>
      sendResponse({
        ok: !!(idx && idx.ok),
        indexed: (idx && idx.ok && idx.indexed) || {},
        count: (idx && idx.ok && idx.count) || 0,
      })
    );
    return true;
  }

  if (msg.type === "clear_pending") {
    rememberPending([]).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "list_conversations") {
    (async () => {
      const tab = await activeTab();
      if (!tab || !/^https:\/\/claude\.ai\//i.test(tab.url || "")) {
        sendResponse({
          ok: false,
          status: "error",
          message: "Open claude.ai in this tab first.",
        });
        return;
      }
      let listed;
      try {
        listed = await chrome.tabs.sendMessage(tab.id, { type: "list" });
      } catch (_e) {
        sendResponse({
          ok: false,
          status: "error",
          message: "The page did not respond. Reload the claude.ai tab.",
        });
        return;
      }
      // The indexed set is a separate READ-ONLY host call. If the host is not
      // reachable the list is still useful, just unannotated - so a host
      // problem degrades the labels rather than the feature.
      const idx = await sendNative({ type: "indexed" });
      sendResponse({
        ok: !!listed.ok,
        listed,
        indexed: (idx && idx.ok && idx.indexed) || {},
        indexedCount: (idx && idx.ok && idx.count) || 0,
        indexedError: idx && idx.ok ? null : (idx && idx.message) || "unavailable",
      });
    })();
    return true;
  }

  if (msg.type === "capture_selected") {
    (async () => {
      const tab = await activeTab();
      if (!tab || !/^https:\/\/claude\.ai\//i.test(tab.url || "")) {
        sendResponse({
          ok: false,
          status: "error",
          message: "Open claude.ai in this tab first.",
        });
        return;
      }
      const uuids = Array.isArray(msg.uuids) ? msg.uuids : [];
      if (!uuids.length) {
        sendResponse({ ok: false, status: "error", message: "Nothing selected." });
        return;
      }

      let run;
      try {
        run = await chrome.tabs.sendMessage(tab.id, { type: "capture_many", uuids });
      } catch (_e) {
        sendResponse({
          ok: false,
          status: "error",
          message: "The page stopped responding mid-run. Reload the tab and retry.",
        });
        return;
      }
      if (!run || !run.ok) {
        const report = {
          ok: false,
          status: "capture_failed",
          kind: (run && run.kind) || "unknown",
          message: (run && run.detail) || "Capture failed before anything was written.",
          perConversation: [],
        };
        await rememberRun(report);
        sendResponse(report);
        return;
      }

      // Send whatever was captured, even after a fatal stop: files already in
      // hand should not be discarded because a later one failed.
      // ingest defaults to true: "Capture + Ingest" is the ordinary action, and
      // an older popup that sends no flag should keep behaving as it did.
      const doIngest = msg.ingest !== false;
      let reply = { ok: true, status: "none", message: "Nothing captured." };
      if (run.captured.length) {
        reply = await sendNative({
          type: doIngest ? "save_and_ingest" : "save",
          files: run.captured.map((c) => ({ name: c.filename, content: c.content })),
        });
      }

      // Fold the host's per-file disposition back onto the conversations, so
      // every selected uuid has an outcome by name.
      const byFile = {};
      // A "save" run has no per-file disposition to report - nothing was
      // ingested, by request - so every file it wrote is SAVED. Leaving these
      // UNKNOWN would read as something having gone wrong.
      for (const w of reply.written || []) byFile[w.name] = "SAVED";
      for (const r of reply.refused || []) {
        byFile[r.name] = "REFUSED: " + (r.reason || "write guard");
      }
      for (const n of reply.ingested || []) byFile[n] = "INGESTED";
      for (const n of reply.partial || []) byFile[n] = "PARTIAL";
      for (const n of reply.skipped || []) byFile[n] = "SKIPPED";
      for (const r of reply.rejected || []) byFile[r.name] = "REJECTED: " + r.reason;

      const perConversation = run.captured.map((c) => ({
        uuid: c.uuid,
        title: c.title,
        messages: c.messages,
        outcome: byFile[c.filename] || "UNKNOWN",
      }));
      const OUTCOME_LABEL = {
        capped: "CAPPED",
        not_attempted: "NOT ATTEMPTED",
      };
      for (const f of run.failed || []) {
        perConversation.push({
          uuid: f.uuid,
          title: "",
          messages: 0,
          outcome:
            (OUTCOME_LABEL[f.kind] || "NOT CAPTURED") + ": " + (f.detail || f.kind),
        });
      }

      // The conversations the cap deferred, as uuids rather than as a count.
      // A count can only be described; a list can be re-selected, which is what
      // turns "run it again" from an instruction into a button.
      const remaining = (run.failed || [])
        .filter((f) => f.kind === "capped")
        .map((f) => f.uuid);

      const report = Object.assign({}, reply, {
        perConversation,
        selected: uuids.length,
        capturedCount: run.captured.length,
        // failedCount excludes the capped ones: they are not failures, and
        // counting them as such is what made a working run look broken.
        failedCount: (run.failed || []).length - remaining.length,
        cappedCount: remaining.length,
        remaining,
        stoppedBy: run.stoppedBy || null,
        limit: run.limit,
      });
      await rememberRun(report);
      await rememberPending(remaining);
      sendResponse(report);
    })();
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
