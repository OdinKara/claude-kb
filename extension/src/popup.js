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

/* ------------------------------------------------------------------ list UI */

const listWrap = document.getElementById("listwrap");
const listEl = document.getElementById("list");
const countsEl = document.getElementById("counts");
const btnLoad = document.getElementById("load");
const btnCapSel = document.getElementById("capsel");

let rows = [];

function selected() {
  return Array.from(listEl.querySelectorAll("input:checked")).map((i) => i.value);
}

function refreshSelection() {
  const n = selected().length;
  btnCapSel.disabled = n === 0;
  btnCapSel.textContent = n ? `Capture selected (${n})` : "Capture selected";
}

function renderList(annotated, note) {
  rows = annotated;
  listEl.textContent = "";
  const tally = { new: 0, grown: 0, indexed: 0 };

  for (const r of annotated) {
    tally[r.state] = (tally[r.state] || 0) + 1;

    const item = document.createElement("label");
    item.className = "item";

    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = r.uuid;
    box.addEventListener("change", refreshSelection);

    const meta = document.createElement("span");
    meta.className = "meta";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = r.name;
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent =
      (r.updated_at ? r.updated_at.slice(0, 10) : "no date") +
      (r.msg_count ? ` - ${r.msg_count} indexed` : "");
    meta.appendChild(name);
    meta.appendChild(sub);

    const tag = document.createElement("span");
    tag.className = "tag " + r.state;
    tag.textContent = r.state === "grown" ? "grown" : r.state;

    item.appendChild(box);
    item.appendChild(meta);
    item.appendChild(tag);
    listEl.appendChild(item);
  }

  countsEl.textContent =
    `${annotated.length} chats - ${tally.new} new, ${tally.grown} grown, ` +
    `${tally.indexed} indexed` + (note ? ` - ${note}` : "");
  listWrap.classList.remove("hidden");
  refreshSelection();
}

function renderPerConversation(report) {
  const box = document.createElement("div");
  box.className = "results";
  for (const c of report.perConversation || []) {
    const line = document.createElement("div");
    const o = document.createElement("span");
    const head = String(c.outcome || "").split(":")[0];
    o.className =
      "o " +
      (head === "INGESTED" ? "o-ing"
        : head === "PARTIAL" ? "o-par"
        : head === "SKIPPED" ? "o-skp"
        : "o-rej");
    o.textContent = c.outcome;
    line.appendChild(o);
    line.appendChild(
      document.createTextNode(" - " + (c.title || c.uuid.slice(0, 8)))
    );
    box.appendChild(line);
  }
  outEl.appendChild(box);
}

function showRun(report) {
  if (!report) return;
  if (report.status === "capture_failed" || report.ok === false) {
    render("bad", "Run failed", report.message || "");
    if (report.perConversation && report.perConversation.length) {
      renderPerConversation(report);
    }
    return;
  }

  const ing = (report.ingested || []).length;
  const par = (report.partial || []).length;
  const skp = (report.skipped || []).length;
  const rej = (report.rejected || []).length;
  const notCaptured = report.failedCount || 0;

  const cls = rej || notCaptured ? "bad" : par ? "warn" : "ok";
  const head =
    `${report.selected} selected: ${ing} ingested, ${par} PARTIAL, ` +
    `${skp} unchanged, ${rej} refused, ${notCaptured} not captured`;
  const why = report.stoppedBy
    ? `The run stopped early after a ${report.stoppedBy} failure. ` +
      `Conversations after that point were not attempted.`
    : "";

  outEl.classList.remove("hidden");
  render(cls, "Run complete", head, why);
  renderPerConversation(report);
}

btnLoad.addEventListener("click", async () => {
  btnLoad.disabled = true;
  outEl.classList.remove("hidden");
  render("warn", "Loading...", "Paging through the conversation list.");
  try {
    const res = await chrome.runtime.sendMessage({ type: "list_conversations" });
    if (!res || !res.ok) {
      const l = res && res.listed;
      const [label, why] = (l && CAPTURE_REASONS[l.kind]) || [
        "Could not load the list",
        "",
      ];
      render("bad", label, (res && (res.message || (l && l.detail))) || "", why);
      return;
    }
    const notes = [];
    if (res.listed.truncated) notes.push(`capped at ${res.listed.max}`);
    if (res.indexedError) notes.push("index unknown: " + res.indexedError);
    renderList(annotateRows(res.listed.items, res.indexed), notes.join(", "));
    render("ok", "List loaded", `${res.indexedCount} conversations already indexed.`);
  } catch (e) {
    render("bad", "Extension error", String((e && e.message) || e));
  } finally {
    btnLoad.disabled = false;
  }
});

document.getElementById("selnew").addEventListener("click", (e) => {
  e.preventDefault();
  const wanted = new Set(
    rows.filter((r) => r.state === "new" || r.state === "grown").map((r) => r.uuid)
  );
  listEl.querySelectorAll("input").forEach((i) => (i.checked = wanted.has(i.value)));
  refreshSelection();
});

document.getElementById("selnone").addEventListener("click", (e) => {
  e.preventDefault();
  listEl.querySelectorAll("input").forEach((i) => (i.checked = false));
  refreshSelection();
});

btnCapSel.addEventListener("click", async () => {
  const uuids = selected();
  busy(true);
  btnCapSel.disabled = true;
  outEl.classList.remove("hidden");
  render("warn", "Capturing...", `0 of ${uuids.length}. This is paced deliberately.`);
  try {
    showRun(await chrome.runtime.sendMessage({ type: "capture_selected", uuids }));
  } catch (e) {
    render("bad", "Extension error", String((e && e.message) || e));
  } finally {
    busy(false);
    refreshSelection();
  }
});

chrome.runtime.onMessage.addListener((m) => {
  if (m && m.type === "capture_progress") {
    render(
      "warn",
      "Capturing...",
      `${m.done + 1} of ${m.total}. This is paced deliberately.`
    );
  }
});

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
    btnLoad.disabled = false;
  } else {
    hostEl.className = "host bad";
    hostEl.textContent = (reply && reply.message) || "The native host did not answer.";
  }

  // A bulk run outlives the popup. Show the last one on open so closing the
  // window mid-run does not lose the account of what landed.
  const last = await chrome.runtime.sendMessage({ type: "last_run" });
  if (last && last.perConversation && last.perConversation.length) {
    showRun(last);
    const note = document.createElement("span");
    note.className = "why";
    note.textContent = "This is the previous run's report.";
    outEl.appendChild(note);
  }
})();
