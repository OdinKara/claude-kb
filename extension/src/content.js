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

  if (res.status === 401 || res.status === 403) {
    throw fail("auth", `HTTP ${res.status} - the session is not authenticated`);
  }
  // A login redirect answers 200 with HTML rather than JSON. Treating that as a
  // shape change would send someone hunting an API break when they are simply
  // logged out.
  const ctype = (res.headers.get("content-type") || "").toLowerCase();
  if (ctype.includes("text/html")) {
    throw fail("auth", "the API returned HTML, which means a login page");
  }
  if (res.status === 404) {
    throw fail("notfound", "HTTP 404 - no such conversation for this account");
  }
  if (!res.ok) {
    throw fail("transport", `HTTP ${res.status}`);
  }

  let json;
  try {
    json = await res.json();
  } catch (e) {
    throw fail("shape", `response was not JSON (${e.message})`);
  }
  return json;
}

async function listOrganizations() {
  const json = await getJson("/api/organizations");
  if (!Array.isArray(json)) {
    throw fail("shape", "/api/organizations did not return an array");
  }
  const orgs = json
    .map((o) => (o && typeof o === "object" ? String(o.uuid || "") : ""))
    .filter(Boolean);
  if (!orgs.length) {
    throw fail("shape", "/api/organizations returned no organisation uuids");
  }
  return orgs;
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

async function capture() {
  const urlUuid = uuidFromPath(location.pathname);
  if (!urlUuid) {
    return fail(
      "not_a_conversation",
      "this page is not a conversation - open a chat first"
    );
  }

  let orgs;
  try {
    orgs = await listOrganizations();
  } catch (e) {
    return e.kind ? e : fail("transport", String(e));
  }

  // The account may have several organisations and only one owns this chat.
  // Try each rather than hardcoding one; a 404 from the wrong org is expected,
  // not an error worth reporting.
  let conversation = null;
  let lastError = null;
  for (const org of orgs) {
    try {
      conversation = await fetchConversation(org, urlUuid);
      break;
    } catch (e) {
      lastError = e.kind ? e : fail("transport", String(e));
      if (lastError.kind !== "notfound") break; // auth/shape: stop, do not mask
    }
  }
  if (!conversation) {
    return lastError || fail("notfound", "conversation not found in any organisation");
  }

  const verdict = validateConversation(conversation, urlUuid);
  if (!verdict.ok) {
    return fail(verdict.kind, verdict.detail);
  }

  const now = new Date();
  const envelope = buildEnvelope(conversation, urlUuid, now.toISOString());
  return {
    ok: true,
    filename: captureFilename(now),
    content: JSON.stringify(envelope),
    title: String(conversation.name || "").slice(0, 120) || "(untitled)",
    uuid: urlUuid,
    messages: verdict.indexable,
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "capture") {
    capture()
      .then(sendResponse)
      .catch((e) => sendResponse(fail("internal", String((e && e.message) || e))));
    return true; // async response
  }
  return false;
});
