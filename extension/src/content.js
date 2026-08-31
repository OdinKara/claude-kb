/* Runs on claude.ai. Fetches the conversation from the web app's own API and
 * returns a validated envelope.
 *
 * Why the fetch happens HERE and not in the service worker: these requests must
 * be same-origin to carry the session cookie. A content script on claude.ai is
 * same-origin by definition; an extension-origin fetch is not, and a Lax cookie
 * would simply not be sent - which would look like being logged out.
 *
 * Why the API and not the DOM: the API returns the whole transcript in one call
 * with an honest message count. A DOM scrape of a virtualised transcript
 * under-counts by design, and an under-count is precisely the input that the
 * ingest side's shrink guard exists to reject. A short capture that looked
 * complete would be held back forever; there is no version of "degrade to
 * scraping" that is better than failing loudly.
 *
 * THESE ENDPOINTS ARE INTERNAL AND UNSUPPORTED. They were observed working on
 * one account in one browser on one day. They can change with no notice, no
 * deprecation and no version header, so every failure below is classified
 * rather than collapsed into "could not export".
 */

const API_TIMEOUT_MS = 20000;

/* Pacing. These endpoints are internal and unsupported, and a tight bulk loop
 * is the single behaviour most likely to get throttled or noticed. None of this
 * is latency-critical - a capture run is something you start and walk away
 * from - so the delays are deliberately generous rather than tuned. */
const LIST_PAGE_SIZE = 100;
const LIST_PAGE_DELAY_MS = 250;
const LIST_MAX_ITEMS = 2000;
const CAPTURE_DELAY_MS = 500;
// CAPTURE_MAX_PER_RUN comes from capture-core.js, which the popup also loads so
// it can warn at selection time. One definition, two consumers.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(kind, detail, extra) {
  return Object.assign({ ok: false, kind, detail }, extra || {});
}

async function getJson(url) {
  /* Returns {ok, status, json} or throws a classified error object. */
  let res;
  try {
    res = await fetch(url, {
      credentials: "include",
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch (e) {
    // Network-level: offline, DNS, timeout, connection reset. Not auth, and
    // not a shape change - saying so saves a wrong investigation later.
    throw fail("transport", `${e.name}: ${e.message}`);
  }

  // Read the body once, up front: a 403's body is what distinguishes "not
  // permitted" from anything else, and it cannot be read after the fact.
  let text = "";
  try {
    text = await res.text();
  } catch (e) {
    throw fail("transport", `could not read the response body (${e.message})`);
  }

  const verdict = classifyResponse(
    res.status,
    res.headers.get("content-type"),
    text
  );
  if (!verdict.ok) {
    throw fail(verdict.kind, verdict.detail, { errorType: verdict.errorType });
  }

  try {
    return JSON.parse(text);
  } catch (e) {
    throw fail("shape", `response was not JSON (${e.message})`);
  }
}

/* Resolve the organisation(s) that can serve conversations.
 *
 * Every path that needs an org goes through here - the single capture, the
 * list, and the bulk run - so none of them can end up selecting differently.
 * The single path worked before by luck of array order; that is not a property
 * worth relying on. */
async function chatOrgs() {
  const json = await getJson("/api/organizations");
  const picked = selectChatOrgs(json);
  if (!picked.ok) throw fail(picked.kind, picked.detail);
  return picked.orgs;
}

async function fetchConversation(orgUuid, convUuid) {
  // tree=True deliberately: the official export ships the WHOLE tree including
  // off-path branches, so capturing only the active path would make every
  // forked conversation come back short. render_all_tools is deliberately NOT
  // sent - those blocks are dropped by the normalizer anyway.
  const url =
    `/api/organizations/${encodeURIComponent(orgUuid)}` +
    `/chat_conversations/${encodeURIComponent(convUuid)}` +
    `?tree=True&rendering_mode=messages`;
  return getJson(url);
}

/* Capture ONE conversation by uuid. Both the single-capture button and the
 * multi-select run go through here - there is no second implementation for the
 * bulk case, because two capture paths would drift and only one of them would
 * be the one that got the validation right. */
async function captureOne(convUuid, orgs, seq) {
  let conversation = null;
  let lastError = null;
  // `orgs` is already filtered to chat-capable organisations, in a
  // deterministic order. Where more than one qualifies, a 404 from the wrong
  // one is expected and worth moving past; anything else is not.
  for (const org of orgs) {
    try {
      conversation = await fetchConversation(org, convUuid);
      break;
    } catch (e) {
      lastError = e.kind ? e : fail("transport", String(e));
      if (lastError.kind !== "notfound") break; // auth/shape: stop, do not mask
    }
  }
  if (!conversation) {
    return lastError || fail("notfound", "conversation not found in any organisation");
  }

  const verdict = validateConversation(conversation, convUuid);
  if (!verdict.ok) return fail(verdict.kind, verdict.detail);

  const now = new Date();
  const envelope = buildEnvelope(conversation, convUuid, now.toISOString());
  // A run captures several conversations inside the same second, so the
  // timestamp alone is not unique. The suffix keeps the required prefix and the
  // shape the write guard expects.
  const base = captureFilename(now);
  const filename =
    typeof seq === "number" ? base.replace(/\.json$/, `-${seq}.json`) : base;
  return {
    ok: true,
    filename,
    content: JSON.stringify(envelope),
    title: String(conversation.name || "").slice(0, 120) || "(untitled)",
    uuid: convUuid,
    messages: verdict.indexable,
  };
}

async function captureActive() {
  const urlUuid = uuidFromPath(location.pathname);
  if (!urlUuid) {
    return fail(
      "not_a_conversation",
      "this page is not a conversation - open a chat first"
    );
  }
  let orgs;
  try {
    orgs = await chatOrgs();
  } catch (e) {
    return e.kind ? e : fail("transport", String(e));
  }
  return captureOne(urlUuid, orgs);
}

/* Page through the conversation list. Paced, and capped so a runaway or a
 * changed pagination contract cannot spin forever. */
async function listConversations() {
  let orgs;
  try {
    orgs = await chatOrgs();
  } catch (e) {
    return e.kind ? e : fail("transport", String(e));
  }

  const seen = new Set();
  const items = [];
  let truncated = false;

  for (const org of orgs) {
    let offset = 0;
    for (;;) {
      let page;
      try {
        page = await getJson(
          `/api/organizations/${encodeURIComponent(org)}/chat_conversations` +
            `?limit=${LIST_PAGE_SIZE}&offset=${offset}`
        );
      } catch (e) {
        const err = e.kind ? e : fail("transport", String(e));
        if (err.kind === "notfound") break; // this org has no chats; try the next
        // Report what was collected rather than throwing it away: a partial
        // list is still usable, and silence about why is not.
        return Object.assign({}, err, { partialItems: items, partial: true });
      }

      const norm = normalizeListPage(page);
      if (!norm.ok) return fail(norm.kind, norm.detail, { partialItems: items });

      for (const it of norm.items) {
        if (seen.has(it.uuid)) continue;
        seen.add(it.uuid);
        items.push(it);
      }

      if (!Array.isArray(page) || page.length < LIST_PAGE_SIZE) break;
      offset += LIST_PAGE_SIZE;
      if (items.length >= LIST_MAX_ITEMS) {
        truncated = true;
        break;
      }
      await sleep(LIST_PAGE_DELAY_MS);
    }
    if (truncated) break;
  }

  return { ok: true, items, truncated, max: LIST_MAX_ITEMS };
}

/* Capture many. Reports per conversation, always - including the ones a fatal
 * error meant were never attempted, because a bulk run that stops halfway and
 * says only "failed" leaves you guessing what landed. */
async function captureMany(uuids) {
  let orgs;
  try {
    orgs = await chatOrgs();
  } catch (e) {
    return e.kind ? e : fail("transport", String(e));
  }

  const wanted = uuids.slice(0, CAPTURE_MAX_PER_RUN);
  const overflow = uuids.slice(CAPTURE_MAX_PER_RUN);
  const captured = [];
  const failed = [];
  let stoppedBy = null;

  for (let i = 0; i < wanted.length; i++) {
    const uuid = wanted[i];
    if (i) await sleep(CAPTURE_DELAY_MS);
    try {
      chrome.runtime.sendMessage({
        type: "capture_progress",
        done: i,
        total: wanted.length,
        uuid,
      });
    } catch (_e) {
      /* the popup may be closed; progress is a courtesy, not a dependency */
    }

    let res;
    try {
      res = await captureOne(uuid, orgs, i);
    } catch (e) {
      res = fail("internal", String((e && e.message) || e));
    }

    if (res.ok) {
      captured.push(res);
      continue;
    }
    failed.push({ uuid, kind: res.kind, detail: res.detail });
    // These repeat for every remaining conversation - they are properties of
    // the session or the account, not of one chat. Hammering the endpoint to
    // collect identical failures is exactly what the pacing exists to avoid.
    if (
      res.kind === "auth" ||
      res.kind === "shape" ||
      res.kind === "forbidden" ||
      res.kind === "no_chat_org"
    ) {
      stoppedBy = res.kind;
      for (const rest of wanted.slice(i + 1)) {
        failed.push({
          uuid: rest,
          kind: "not_attempted",
          detail: `stopped after a ${res.kind} failure`,
        });
      }
      break;
    }
  }

  // "capped" is a distinct state from "not_attempted". Both mean the
  // conversation was not captured, and they mean opposite things about whether
  // anything is wrong: capped is the run working as designed and re-running
  // continues, while not_attempted follows a fatal error and re-running repeats
  // it until the cause is fixed. Sharing one label made a normal outcome read
  // as failure.
  for (const rest of overflow) {
    failed.push({
      uuid: rest,
      kind: "capped",
      detail: `beyond the ${CAPTURE_MAX_PER_RUN}-per-run cap`,
    });
  }

  return {
    ok: true,
    captured,
    failed,
    stoppedBy,
    limit: CAPTURE_MAX_PER_RUN,
    cappedCount: overflow.length,
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  const run = (p) =>
    p
      .then(sendResponse)
      .catch((e) => sendResponse(fail("internal", String((e && e.message) || e))));

  if (msg.type === "capture") {
    run(captureActive());
    return true;
  }
  if (msg.type === "list") {
    run(listConversations());
    return true;
  }
  if (msg.type === "capture_many") {
    run(captureMany(Array.isArray(msg.uuids) ? msg.uuids : []));
    return true;
  }
  return false;
});
