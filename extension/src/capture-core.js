/* Pure capture logic: no fetch, no chrome.*, no DOM.
 *
 * Split out so it can be exercised outside a browser. Everything that decides
 * whether a capture is valid lives here, because that is the part where being
 * wrong is expensive: a malformed capture reaching incoming/ is worse than no
 * capture at all.
 *
 * indexableMessages() MUST agree with _conv_messages() in claude_kb.py. That
 * agreement is what makes the capture's message count comparable to the stored
 * one, and the shrink guard compares those two numbers. If they diverge, the
 * guard is comparing things that do not mean the same thing.
 */

const CONV_UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const FORMAT = "claude-kb-web-export";
const FORMAT_VERSION = 1;

/* The uuid the capture is being taken FOR, from the page URL. */
function uuidFromPath(pathname) {
  const m = String(pathname || "").match(/\/chat\/([^/?#]+)/i);
  if (!m) return null;
  const id = m[1].toLowerCase();
  return CONV_UUID_RE.test(id) ? id : null;
}

/* Mirrors flatten_text(): content blocks of type "text", else the text field.
 * Blocks of any other type are ignored, which is why render_all_tools is not
 * requested - those blocks are dropped here anyway and only add divergence. */
function flattenText(message) {
  const parts = [];
  if (Array.isArray(message.content)) {
    for (const block of message.content) {
      if (block && block.type === "text" && block.text) parts.push(block.text);
    }
  }
  if (parts.length) return parts.join("\n").trim();
  return String(message.text || "").trim();
}

/* Mirrors _conv_messages(): human/assistant only, non-empty flattened text,
 * non-dict entries skipped. */
function indexableMessages(conversation) {
  const out = [];
  const msgs = (conversation && conversation.chat_messages) || [];
  if (!Array.isArray(msgs)) return out;
  for (const m of msgs) {
    if (!m || typeof m !== "object" || Array.isArray(m)) continue;
    const role = m.sender || m.role || "";
    if (role !== "human" && role !== "assistant") continue;
    const text = flattenText(m);
    if (!text) continue;
    out.push({ text, role });
  }
  return out;
}

/* Validate an API response before it is allowed to become a capture.
 *
 * Returns {ok:true, indexable:n} or {ok:false, kind, detail}. `kind` is the
 * machine-readable reason - the popup shows it, and six months from now it is
 * the difference between "the shape changed" and "you were logged out".
 *
 * A shape change must NEVER be reportable as an empty conversation: a missing
 * chat_messages key is "shape", an empty array is "empty". Conflating them is
 * how a capture could come to replace a real conversation with nothing.
 */
function validateConversation(conversation, expectedUuid) {
  if (!conversation || typeof conversation !== "object" || Array.isArray(conversation)) {
    return { ok: false, kind: "shape", detail: "response was not a conversation object" };
  }
  if (!("uuid" in conversation)) {
    return { ok: false, kind: "shape", detail: "response has no uuid field" };
  }
  if (!("chat_messages" in conversation)) {
    return {
      ok: false,
      kind: "shape",
      detail: "response has no chat_messages field - the API shape has changed",
    };
  }
  if (!Array.isArray(conversation.chat_messages)) {
    return { ok: false, kind: "shape", detail: "chat_messages is not an array" };
  }

  const actual = String(conversation.uuid || "").toLowerCase();
  const expected = String(expectedUuid || "").toLowerCase();
  if (!actual) {
    return { ok: false, kind: "shape", detail: "conversation uuid is empty" };
  }
  if (expected && actual !== expected) {
    return {
      ok: false,
      kind: "mismatch",
      detail: `API returned ${actual} but the page is ${expected}`,
    };
  }

  const truncated = conversation.chat_messages.filter(
    (m) => m && typeof m === "object" && m.truncated
  ).length;
  if (truncated) {
    return {
      ok: false,
      kind: "truncated",
      detail: `${truncated} message(s) came back truncated - the text is incomplete`,
    };
  }

  if (conversation.chat_messages.length === 0) {
    return { ok: false, kind: "empty", detail: "the conversation has no messages" };
  }

  const indexable = indexableMessages(conversation).length;
  if (indexable === 0) {
    return {
      ok: false,
      kind: "empty",
      detail: "no indexable messages (nothing from a human or assistant with text)",
    };
  }
  return { ok: true, indexable };
}

/* Wrap the conversation object VERBATIM. The API's shape is a superset of the
 * official export's, so wrapping keeps the ingest side translation-free -
 * translation is where a divergence between the two counts would come from.
 *
 * current_leaf_message_uuid is carried as metadata and nothing more. Pruning to
 * the active path would make every forked conversation capture short, and a
 * short capture is held back by the shrink guard forever. */
function buildEnvelope(conversation, urlUuid, capturedAt) {
  return {
    format: FORMAT,
    format_version: FORMAT_VERSION,
    captured_at: capturedAt,
    source: "web_export",
    conversation_uuid: String(urlUuid).toLowerCase(),
    current_leaf_message_uuid: conversation.current_leaf_message_uuid || null,
    conversation,
  };
}

/* claude-web-YYYYMMDD-HHMMSS.json - the prefix is required by the native
 * host's write guard, which refuses anything else. */
function captureFilename(date) {
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return (
    "claude-web-" +
    date.getFullYear() +
    p(date.getMonth() + 1) +
    p(date.getDate()) +
    "-" +
    p(date.getHours()) +
    p(date.getMinutes()) +
    p(date.getSeconds()) +
    ".json"
  );
}

/* ------------------------------------------------------------------ listing */

/* Normalise one page of GET .../chat_conversations.
 *
 * Returns {ok:true, items} or {ok:false, kind, detail}. Same discipline as
 * validateConversation: a response that is not the expected shape is a SHAPE
 * failure, never an empty list. "You have no conversations" and "the API
 * changed" must not look alike. */
function normalizeListPage(json) {
  if (!Array.isArray(json)) {
    return {
      ok: false,
      kind: "shape",
      detail: "chat_conversations did not return an array",
    };
  }
  const items = [];
  for (const raw of json) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const uuid = String(raw.uuid || "").toLowerCase();
    if (!CONV_UUID_RE.test(uuid)) continue;
    items.push({
      uuid,
      name: String(raw.name || "").trim() || "(untitled)",
      updated_at: String(raw.updated_at || ""),
      project_uuid: raw.project_uuid || null,
    });
  }
  if (json.length && !items.length) {
    // A full page that yielded nothing usable is a shape change wearing an
    // empty list as a disguise.
    return {
      ok: false,
      kind: "shape",
      detail: "chat_conversations returned rows with no usable uuid field",
    };
  }
  return { ok: true, items };
}

/* Most recent first. Undated rows sort last rather than pretending to be old. */
function sortByUpdatedDesc(items) {
  return items.slice().sort((a, b) => {
    const x = a.updated_at || "";
    const y = b.updated_at || "";
    if (x === y) return a.name.localeCompare(b.name);
    if (!x) return 1;
    if (!y) return -1;
    return x < y ? 1 : -1;
  });
}

/* Label a row against what the KB already holds.
 *
 *   new       not indexed at all
 *   grown     indexed, but the conversation has changed since
 *   indexed   indexed and unchanged since
 *
 * "grown" is inferred from updated_at, which is the only signal the LIST
 * endpoint offers - it carries no message count. So it means "changed since it
 * was indexed", which is a useful hint and not a promise: a capture may still
 * come back SKIPPED if the change did not alter indexable text, or PARTIAL if
 * it somehow holds fewer messages. The label exists to stop selection being
 * guesswork, not to predict the outcome. */
function classifyRow(item, indexed) {
  const rec = indexed && indexed[item.uuid];
  if (!rec) return "new";
  const seen = String(rec.updated_at || "");
  const now = String(item.updated_at || "");
  if (now && seen && now > seen) return "grown";
  return "indexed";
}

function annotateRows(items, indexed) {
  return sortByUpdatedDesc(items).map((it) =>
    Object.assign({}, it, {
      state: classifyRow(it, indexed),
      msg_count: (indexed && indexed[it.uuid] && indexed[it.uuid].msg_count) || 0,
    })
  );
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CONV_UUID_RE,
    FORMAT,
    FORMAT_VERSION,
    uuidFromPath,
    flattenText,
    indexableMessages,
    validateConversation,
    buildEnvelope,
    captureFilename,
    normalizeListPage,
    sortByUpdatedDesc,
    classifyRow,
    annotateRows,
  };
}
