/* Popup. Its one real job is to never let a held-back or refused capture read
 * as a success - the host already reports INGESTED, PARTIAL, SKIPPED and
 * REJECTED distinctly, so this only has to stop conflating them. */

const hostEl = document.getElementById("host");
const outEl = document.getElementById("out");
const btnIngest = document.getElementById("ingest");
const btnSave = document.getElementById("save");

/* Why a capture never happened, in the caller's terms. The kind matters: six
 * months from now "auth" and "shape" send you to completely different places. */
const CAPTURE_REASONS = {
  auth: [
    "Not signed in",
    "claude.ai answered as if logged out. Sign in in this browser and try again.",
  ],
  shape: [
    "The API shape has changed",
    "The response came back but not in the form this extension understands. " +
      "These endpoints are internal and unsupported; this is the failure that " +
      "means the extension needs updating. Nothing was written.",
  ],
  transport: [
    "Could not reach claude.ai",
    "A network-level failure, not an authentication or API problem.",
  ],
  notfound: ["Conversation not found", "No conversation with this id for this account."],
  mismatch: [
    "Identity mismatch - refused",
    "The API returned a different conversation than the page URL names. " +
      "Capturing it would file it under the wrong identity, so nothing was written.",
  ],
  truncated: [
    "Transcript incomplete - refused",
    "At least one message came back truncated, so the text is incomplete. " +
      "Ingesting it would degrade what is already indexed.",
  ],
  empty: ["Nothing to capture", "This conversation has no indexable messages."],
  not_a_conversation: ["Not a conversation", "Open a chat on claude.ai first."],
  no_content_script: ["Page not ready", "Reload the claude.ai tab and try again."],
};

function render(cls, label, body, why) {
  outEl.className = "out " + cls;
  outEl.textContent = "";
  const l = document.createElement("span");
  l.className = "label";
  l.textContent = label;
  outEl.appendChild(l);
  outEl.appendChild(document.createTextNode(body || ""));
  if (why) {
    const w = document.createElement("span");
    w.className = "why";
    w.textContent = why;
    outEl.appendChild(w);
  }
}

function busy(on) {
  btnIngest.disabled = on;
  btnSave.disabled = on;
}

function describeCaptured(c) {
  if (!c) return "";
  return `\n${c.title} - ${c.messages} messages`;
}

function show(reply) {
  if (!reply) {
    render("bad", "No response", "The extension got no reply at all.");
    return;
  }

  // Capture never happened: classified, never collapsed into one message.
  if (reply.status === "capture_failed") {
    const [label, why] = CAPTURE_REASONS[reply.kind] || [
      "Capture failed",
      "An unclassified failure.",
    ];
    render("bad", label, reply.message || "", why);
    return;
  }

  if (reply.status === "no_host") {
    render("bad", "Native host not reachable", reply.message || "");
    return;
  }

  const cap = describeCaptured(reply.captured);

  switch (reply.status) {
    case "saved":
      render("ok", "Saved (not ingested)", (reply.message || "") + cap);
      return;
    case "ingested":
      render("ok", "Ingested", (reply.message || "") + cap);
      return;
    case "unchanged":
      // Not a failure, but emphatically not new data either.
      render("warn", "Already indexed, unchanged", (reply.message || "") + cap);
      return;
    case "partial":
      render(
        "warn",
        "Held back - PARTIAL",
        (reply.message || "") + cap,
        "The capture has fewer messages than the copy already indexed, so it " +
          "was not allowed to replace it. Nothing was lost. This is expected " +
          "when an older export already covers this conversation."
      );
      return;
    case "mixed":
      render(
        "warn",
        "Partly ingested",
        (reply.message || "") + cap,
        "Some captures landed and others were held back or refused. The " +
          "refused files are still in incoming/ for you to look at."
      );
      return;
    case "rejected":
      render(
        "bad",
        "Refused",
        (reply.message || "") + cap,
        "Nothing was ingested. The file is still in incoming/."
      );
      return;
    case "none":
      render("warn", "Nothing to do", reply.message || "");
      return;
    default:
      render(reply.ok ? "ok" : "bad", reply.ok ? "Done" : "Failed", reply.message || "");
  }
}

async function send(type) {
  busy(true);
  outEl.classList.remove("hidden");
  render("warn", "Working...", "Fetching the conversation.");
  try {
    show(await chrome.runtime.sendMessage({ type }));
  } catch (e) {
    render("bad", "Extension error", String((e && e.message) || e));
  } finally {
    busy(false);
  }
}

btnIngest.addEventListener("click", () => send("capture_and_ingest"));
btnSave.addEventListener("click", () => send("capture_only"));

(async () => {
  const reply = await chrome.runtime.sendMessage({ type: "ping_host" });
  if (reply && reply.ok) {
    hostEl.textContent = "Host ready - writes to " + (reply.incoming || "incoming");
    btnIngest.disabled = false;
    btnSave.disabled = false;
  } else {
    hostEl.className = "host bad";
    hostEl.textContent = (reply && reply.message) || "The native host did not answer.";
  }
})();
