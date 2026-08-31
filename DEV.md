# Claude KB - developer notes

Architecture and hard-won constraints. User-facing setup lives in `README.md`;
this file is the "why it is built this way" companion, written so the two
automation dead ends below do not get rebuilt by the next person (including the
next you).

## Design

Local full-text index over an exported claude.ai chat history and project docs.
Standard-library SQLite FTS5, plus an MCP server so Claude can search past
conversations.

- `claude_kb.py` - the index. Build, incremental update, search, MCP server.
- `kb_ingest.py` - unattended runner. Merges export parts, calls `update`,
  archives what it consumed, appends one line to `ingest.log`.
- `kb_open.py` - the only part that touches the network, and it does so by
  driving a real browser rather than an HTTP client.
- `kb_config.py` - single resolution point for every environment-specific
  value: environment variable, then `config.json`, then a default.

## The export format changed once, and will again

The legacy export was ONE self-contained `data-*.zip`. It now ships a
`manifest-*.json` listing one-time `export_url`s for separate category zips:

    conversations-000.zip    indexed
    projects-000.zip         indexed
    memories-000.zip         downloaded, not indexed
    light_metadata-000.zip   never fetched - see below

Part counts grow with account size; never assume there are exactly four. Both
layouts are still handled: `kb_ingest.py` ingests a legacy `data-*.zip` as-is,
and merge-extracts a multi-part set into one tree first.

Merging matters. `claude_kb.py` requires `projects` to sit BESIDE
`conversations.json`, and the parts arrive in separate zips. `_resolve_export`
walks for `conversations.json`; `_find_projects` accepts either a `projects/`
directory (legacy) or a single `projects.json` (multi-part), searching next to
the conversations file, then at the tree root, then anywhere in the tree.

## light_metadata is deliberately never fetched

`claude_kb.py` indexes only conversations and project docs. `light_metadata`
contributes nothing, and every `export_url` is SINGLE-USE, so fetching it spends
a URL for no benefit. `kb_config`/`kb_open.SKIP_CATEGORIES` excludes it.

Its one use is as the `--yes` login canary, precisely because it is the only
token in the manifest already worth nothing.

## Two dead ends - do not rebuild either

### 1. Cookie-based HTTP fetching does not work

`https://claude.ai/export/<org-uuid>/download/<token>` NEVER serves a zip to an
HTTP client. With a fully valid `sessionKey` cookie it returns HTTP 200
`text/html` - the app shell - with no `Content-Disposition` and no redirect. The
page's JavaScript starts the transfer.

This was verified, not assumed: `/api/organizations` returned 200 JSON with the
correct org on that same cookie, so it was never an auth failure. A
DPAPI-encrypted-cookie fetcher was built, tested, and deleted.

Cloudflare is not the blocker either. Default curl gets a `Cf-Mitigated:
challenge` 403, but any normal browser User-Agent passes straight through. The
challenge is a red herring that will send you down the wrong path.

Cost of learning this: one burned `light_metadata` URL.

### 2. Playwright does not help

Its bundled Chromium is a fresh automated profile, so Cloudflare challenges it -
trading a solved problem for a fragile one. And since requesting the export is
manual anyway, automating the click buys nothing. Built and deleted.

The conclusion both times: drive a real, already-logged-in browser. That is what
`kb_open.py` does, and why it launches a browser by absolute path rather than
`webbrowser.open()` - the OS default may be a browser the user never logs into,
and opening a live one-time URL in a logged-out browser spends the token for
nothing.

## Two modes: interactive and unattended

Same script; the difference is only how it proves the browser is logged in
before spending anything valuable.

    kb_open.py           interactive. Opens /api/organizations and asks
                         "does that tab show JSON? [y/N]". A human looks.

    kb_open.py --yes     non-interactive. Opens the light_metadata URL FIRST
                         as a canary, with a 90s timeout. If the zip lands,
                         the browser is logged in; the file is deleted (never
                         indexed, would only add noise) and the run proceeds.
                         If it times out, exit 3 having touched nothing else.

**Why `--yes` uses a canary instead of just skipping the prompt.** The prompt is
not ceremony - it is the only thing standing between a logged-out browser and a
set of spent one-time tokens. Removing it to make the script automatable would
trade a two-second question for a destroyed export. So `--yes` replaces the human
check with a machine check that costs the one token already worth nothing. A
manifest with no `light_metadata` part proceeds without a canary and says so.

## The canary remembers its result

The canary URL is single-use like every other. A naive canary therefore cannot
survive its own second run: on attempt two the URL it already spent produces no
download, which looks identical to a logged-out browser.

**Nothing can tell those two apart from the outside.** A logged-out browser and
an already-spent URL both yield exactly one observable: no file appeared. So the
script does not guess a cause. It reports the observation and refuses to spend
the token again to re-learn the same non-answer.

State lives in `.canary-state.json` under the KB root, mapping manifest basename
to `passed` or `failed`. Entries are pruned whenever the state is read, dropping
any manifest gone from both `incoming/` and `processed/`. The `processed/` check
is a suffix match because archived manifests are stamped
(`<stamp>_manifest-....json`), so an entry survives its manifest being archived.
A missing or corrupt state file reads as empty rather than raising - losing the
state costs one worthless token, while crashing costs the run.

Three branches in `--yes`, evaluated BEFORE the canary URL is opened:

    passed    skip the canary, go straight to the parts
    failed    exit 3 immediately, open nothing at all
    absent    run the canary, record the outcome, then proceed

The failure message names both possibilities rather than asserting one:

    CANARY FAILED - light_metadata did not download. Either the browser is not
    logged into claude.ai, or this URL was already spent. No valuable URLs
    touched.

**A failed entry is sticky on purpose, and clearing it is deliberate.** Fixing
the login does not unstick it: the state still says `failed` and the run still
exits 3, because from the script's side the login was never the confirmed cause.
Clear it yourself once you know the login is good:

    kb_open.py --reset-canary          clear, open nothing, exit 0
    kb_open.py --reset-canary --yes    clear, then retry in the same run

`--reset-canary` alone clears the entry for the newest manifest in `incoming/`,
or every entry when no manifest is waiting, prints what it cleared, and opens
nothing. If the URLs really are spent, no reset helps - request a fresh export.

## Pipeline

    claude.ai settings -> export request -> manifest-*.json
        -> incoming/manifest-*.json        (dropped there by the user)
        -> kb_open.py                      opens URLs in a browser, waits for
                                           the zips in the downloads dir,
                                           validates, moves them to incoming/,
                                           triggers the ingest task
    incoming/*-000.zip
        -> kb_ingest.py                    merge-extracts ALL parts into one
                                           temp tree, then runs update
        -> claude_kb.py update             incremental upsert, never wipes
        -> processed/<stamp>_*             parts AND manifest archived, one stamp

The scheduled task is created by `install-task.ps1`, not by hand. It was a
hand-made artifact on one machine for months, which meant a fresh install got
all the way through downloading an export - spending its single-use URLs - and
then had nothing to trigger.

`confirm_ingest()` checks the trigger's exit status. It used to discard it, so a
task that does not exist produced a 120-second wait followed by "ingest
triggered but no new ingest.log line" - a sentence whose first two words were
false, pointing at the wrong problem, after the expensive part had already
happened. It now reports the failure immediately, says the parts are safe in
`incoming/`, and names both recoveries.

The scheduled task NEVER contacts an export URL. It cannot succeed from a
non-interactive context and every attempt burns a one-time token. If a manifest
is sitting in `incoming/` with no part zips, the task logs `manifest waiting -
run kb_open.py to download the parts` and does nothing else.

`kb_open.py` does not just fire the scheduled task and walk away. After
triggering it, it polls `ingest.log` for up to 120s for a genuinely new line and
prints it, so the caller sees the actual
`SUMMARY NEW=.. UPDATED=.. SKIPPED=.. ROWS=..` rather than a silent success.

## Not every writer may shrink a conversation

`update()` takes an `authoritative` flag, and `_should_replace()` is the whole
decision:

    authoritative      replace whenever the content hash differs or updated_at
                       is newer. The official export is a COMPLETE snapshot, so
                       it may legitimately shrink a conversation - you deleted
                       messages, and the export is the truth.

    not authoritative  same, but only when the incoming message count is >= the
                       count already indexed. Otherwise: PARTIAL, and the
                       indexed version is left alone.

The asymmetry exists because a hash comparison cannot distinguish "this is
newer" from "this is a partial view of the same thing" - both simply hash
differently. Any writer that captures a conversation while it is still being
added to will hold FEWER messages than a later export of that conversation.
Replacing on hash alone would delete the fuller version, reinstate the shorter
one, and report `UPDATED=1` while doing it. Silent loss, reported as success -
the exact failure shape this codebase keeps running into.

**The counts must come from the same normalizer.** `msg_count` is the length of
`_conv_messages()` output, not `len(chat_messages)`: messages that are neither
`human` nor `assistant`, and messages whose content flattens to nothing, are
dropped. Across one real export that filter removes about 0.6% of messages. A
count taken before that filter is not comparable to a stored one, and the guard
silently degrades into a coin flip. Any future writer must count through
`_conv_messages`.

An unknown count on either side returns PARTIAL rather than replacing: if the
guard cannot prove the incoming version is not a shrink, it refuses.

`SUMMARY` gained a `PARTIAL=` field. `kb_ingest.py` matches on the `SUMMARY`
prefix, so appending fields is safe; reordering or renaming the prefix is not.

## Web captures

`incoming/claude-web-*.json` is a single-conversation envelope
(`format=claude-kb-web-export`, `format_version=1`) wrapping the conversation
object verbatim. `kb_ingest.py` picks these up alongside the export zips;
`claude_kb.py update-web <file...>` ingests them, always with
`authoritative=False`.

The envelope wraps rather than reshapes because the web app's conversation
object is a SUPERSET of the export's: same `uuid`/`name`/`updated_at`, same
`chat_messages` carrying `sender`/`text`/`content`/`created_at`/
`parent_message_uuid`, plus extras the export omits. So the wrapped object goes
straight into `_conv_messages` untranslated - which is also what guarantees the
capture is counted by the same normalizer the export is, without which the
shrink guard above compares two numbers that do not mean the same thing.

The envelope:

    {
      "format": "claude-kb-web-export",
      "format_version": 1,
      "captured_at": "<iso8601>",
      "source": "web_export",
      "conversation_uuid": "<uuid from the page URL>",
      "current_leaf_message_uuid": "<metadata only - see below>",
      "conversation": { ...the API's conversation object, verbatim... }
    }

`conversation_uuid` is the identity the capture was TAKEN FOR - the uuid in the
page URL - and `conversation.uuid` is the identity the API answered with. Both
are required and must agree. A mismatch means the capture is filed under the
wrong identity, and since that identity is the primary key the official export
also uses, ingesting it would corrupt a real conversation with a different one's
messages. Nothing can determine which of the two is correct from the outside, so
the capture is refused rather than guessed at.

`current_leaf_message_uuid` is METADATA ONLY and is deliberately never read.
Pruning a capture to the active path would make every forked conversation
capture short - and 15% of conversations in one real export contain a fork, some
of them hundreds of messages long. Each would then be rejected as PARTIAL
forever, by a guard doing its job against a capture that was wrong by design.
Capture the whole tree; the export ships the whole tree too, which is what makes
the two counts comparable.

A capture is refused outright rather than half-ingested, because a capture that
cannot be trusted is worth less than no capture at all: wrong format, a
`format_version` this build does not read, no conversation object, no
`conversation.uuid`, a missing or mismatched envelope `conversation_uuid`, no
`chat_messages`, no indexable messages, or ANY message flagged `truncated`. A
truncated message means the source returned incomplete text - the count matches
but the content does not, so it would churn `UPDATED` forever while degrading
what is indexed.

**The reader never raises.** Anything unexpected becomes a REJECTED line. This
is not defensive habit: a capture that crashed the reader would abort the whole
batch it arrived in, taking the good captures beside it down, and `kb_ingest.py`
would then leave every file in `incoming/` to be retried at 06:00 - forever,
because the poison pill is retried along with them. `_conv_messages` skips
non-dict entries for the same reason; one malformed message must not cost a
batch.

Two captures of one conversation in a single run are resolved before the upsert,
keeping the longer and rejecting the other with a reason. Otherwise the second
would be compared against the first's freshly written count - a comparison
against this run rather than against the index.

Four dispositions, one line each, per file:

    INGESTED  reached the index (new or updated). ARCHIVED.
    PARTIAL   shorter than what is stored, so held back. ARCHIVED - a
              conversation only grows in the index, so a short capture stays
              short and retrying accomplishes nothing.
    SKIPPED   already indexed, unchanged. ARCHIVED, nothing left to contribute.
    REJECTED  failed validation, never ingested, LEFT in incoming/. It is a
              fault to look at, not a file to quietly file away - and so it is
              reported again on every later run until someone deals with it.

The first three are all "accepted"; only REJECTED is not. Keeping INGESTED
distinct from the other two matters more than it looks: a file that VALIDATED is
not a file that reached the index, and reporting the first as the second is
exactly how a held-back capture comes to look like a successful one in a UI. The
first cut of this printed INGESTED for everything that parsed, which meant a
PARTIAL - the very outcome the shrink guard exists to produce - was announced as
a success.

The reader emits one `REJECTED <file>: <reason>` line per refusal and
`kb_ingest.py` folds those reasons into its single log entry. A reason that is
computed and then dropped before reaching `ingest.log` leaves a 06:00 rejection
undiagnosable the morning after - the same silent-stall shape as the eleven-day
export stall, in a new place.

## The native messaging host

`native/host.py` speaks Chrome/Edge native messaging (uint32 LE length prefix +
UTF-8 JSON) so a browser extension can write captures into `incoming/` and
trigger an ingest - neither of which an extension can do by itself.

Every path comes from `kb_config`. The one thing the host cannot resolve on its
own is where the KB scripts live, because the browser launches it from its own
directory: `host.cmd` sets `CLAUDE_KB_SCRIPTS` and the installer writes
`host.cmd`. That is also why `host.py` inserts that directory on `sys.path`
before importing `kb_config` rather than assuming an import path.

### The write guard is the point

`safe_name()` is the reason this host is safe to install. It runs with the
user's privileges and writes where it is told, so a compromised or simply buggy
extension is the threat model, and three independent conditions are ALL
required:

    basename only        no directory component survives, ".." is stripped
    extension allowlist  .json or .md, nothing executable or loadable
    name prefix          claude-web-, a hard requirement

The prefix is not cosmetic. It is what keeps the host from being a general
file-writing primitive: even given the other two conditions, a caller cannot
land a file the ingest path does not already expect to find. Do not relax it for
convenience.

### It always answers

A native host that exits without replying gives the extension a bare
"disconnected", which is indistinguishable from not being installed - so a
configuration problem sends someone to the registry instead of to their
`config.json`. Every failure path therefore returns a framed message, including
a failure to import `kb_config` at all.

### It reports on everything pending, not just what it wrote

`save_and_ingest` writes files and then ingests the whole `incoming/` queue,
reporting each file's disposition. The archiving step runs the normal ingest
runner, which processes whatever is there regardless - so reporting only on the
files just written would mean the numbers shown to the user did not describe
what actually happened to the index.

### The installer

`install-host.ps1` takes an extension id and a scripts directory and nothing
else. It generates `host.cmd` from `host.cmd.example` and writes the host
manifest, then registers both under HKCU for Chrome and Edge. Both generated
files carry machine-specific absolute paths and are gitignored, the same way the
extension bundle is.

It validates before touching the registry: the extension id must look like one
(32 chars, a-p), and the scripts directory must actually contain the modules.
A wrong value caught here is a message; the same value caught later is a silent
"host disconnected".

The script is deliberately **pure ASCII**. Windows PowerShell 5.1 reads a
BOM-less UTF-8 file as Windows-1252, so a stray non-ASCII character becomes
mojibake and can break parsing outright.

## The capture extension

`extension/` is MV3. The split is not cosmetic:

    capture-core.js   pure. No fetch, no chrome.*, no DOM. Everything that
                      DECIDES anything - the normalizer, validation, the
                      envelope - so it can be tested outside a browser, which
                      is the only way any of it gets tested at all.
    content.js        the fetch, on claude.ai
    background.js     the native-messaging bridge
    popup.*           reporting

### Three scopes, and capture-core is loaded into each that needs it

The popup, the content script and the service worker are separate JavaScript
worlds. A file listed in `content_scripts` is injected into the PAGE and is not
visible to the popup; `popup.html` has to load it again, explicitly, before
`popup.js`.

This is not a subtlety that can be left to memory. `annotateRows is not defined`
shipped on the only path the feature has, because `popup.js` called a
capture-core function that `popup.html` never loaded. Every previous suite had
`require()`d capture-core directly and tested it in isolation - proving the pure
logic correct and proving nothing whatsoever about whether the popup could see
it. Thirty-one green cases, and the feature had never once been executed.

`test_wiring.js` closes that class of hole in two layers: a static check that
each bundle (popup / content / background) can resolve every capture-core
function it calls, and a smoke run of the REAL click handler in a VM against a
fake DOM and a stubbed `chrome.*`. The suite is itself verified against a
deliberately broken copy - a test that passes on both the broken and the fixed
code proves nothing.

## Two rules for tests here

Both were paid for. Neither is a style preference.

### Assert on rendered output, not on the absence of exceptions

**Wherever the code under test catches its own errors, a smoke test that only
asks "did it throw" is worthless.**

On the deliberately broken copy of the extension, the assertions
`the popup's scripts load without a ReferenceError` and `clicking it does not
raise` both **PASSED** - while the feature was completely non-functional. The
click handler wraps its body in try/catch and renders the failure, so the
exception never reaches the test. What caught it was asserting on what the user
would actually see: the rendered text must not contain "is not defined", and the
list must have rows in it.

This generalises past the extension. Anything with a try/catch, an error branch
that logs and continues, or a function that returns a failure value instead of
raising - `_read_web_export` returning `None`, `_reject`, the host answering
`{ok:false}` rather than dying - has the same property. Test the OUTPUT the
caller receives, not the absence of a crash. A component that is good at not
crashing is very good at failing quietly.

### Verify a new test against a deliberately broken copy

**A test that passes on both the broken and the fixed code proves nothing**, and
you cannot tell which kind you have written by reading it.

So when a test is added for a specific defect, break the code on purpose - a
copy in a scratch directory, never the working tree - and confirm the test fails
there, with the failure the user reported. `test_wiring.js` takes a repo root as
an argument for exactly this: it is run against the real tree and against a copy
whose `popup.html` is missing the capture-core script tag, and it must pass the
first and fail the second.

Applied to the four bugs the tests caught earlier - the poison-pill batch abort,
the PARTIAL-reported-as-INGESTED confusion, the org-order 403, the missing script
tag - each one is a case where the passing test was written after seeing the
failing one.

**The fetch lives in the content script, not the service worker.** These
requests must be same-origin to carry the session cookie. A content script on
claude.ai is same-origin by definition; a fetch from the extension origin is
not, and a Lax cookie would simply not be sent - which presents as being logged
out, sending you to debug the wrong thing entirely.

**`indexableMessages()` in capture-core.js must agree with `_conv_messages()` in
claude_kb.py.** That agreement is the whole basis of the shrink guard: it
compares the capture's count against the stored one, and if the two functions
disagree it is comparing things that do not mean the same thing. Change one,
change the other, and re-run the round-trip test - which drives a real
extension-built envelope through the host into the reader rather than asserting
the two agree.

**API-only, no DOM fallback, on purpose.** A scrape of a virtualised transcript
under-counts, and an under-count is precisely what the shrink guard rejects; a
scrape that looked complete would be held back forever. There is no version of
"degrade to scraping" that beats failing loudly.

**Request `tree=True`, never `render_all_tools`.** The first because the export
ships the whole tree and a pruned capture would look short. The second because
those blocks are dropped by `flatten_text` anyway, so requesting them only adds
shape divergence between what is captured and what is indexed.

### Listing and bulk capture

The list comes from `GET .../chat_conversations?limit&offset`, paged. Bulk
capture goes through the SAME `captureOne()` as the single-capture button - same
validation, same envelope, same refusals. There is deliberately no second
capture implementation for the bulk case: two paths would drift, and only one of
them would be the one whose validation was right.

**Paced on purpose.** A tight bulk loop against internal endpoints is the single
behaviour most likely to be throttled or noticed, and none of this is
latency-critical - a run is something you start and walk away from. So:
`LIST_PAGE_DELAY_MS`, `CAPTURE_DELAY_MS`, a per-run cap
(`CAPTURE_MAX_PER_RUN`), and a list cap (`LIST_MAX_ITEMS`) that also stops a
changed pagination contract from spinning forever.

**A run always accounts for every conversation selected.** Captured ones carry
the host's per-file disposition; ones that failed carry their reason; and after
a fatal `auth`/`shape` failure the loop stops and marks the remainder
`not_attempted` rather than hammering the endpoint for identical failures. A
bulk run that stops halfway and reports only "failed" leaves you guessing what
landed, which is the same class of problem as a rejection whose reason is
discarded.

Whatever was captured before a fatal stop is still sent. Files already in hand
should not be thrown away because a later one failed.

### Marking what is already indexed

`indexed` is a READ-ONLY host message returning `{uuid: {msg_count,
updated_at}}`, used to label rows new / indexed / grown. Without it, selecting
from an account that has already been exported is guesswork and most captures
come back SKIPPED or PARTIAL.

It is a distinct message type rather than a widening of anything that writes.
Every widening of a writing path is a widening of what a compromised extension
can do; this one opens the database `mode=ro` and cannot modify it even in
principle, and `safe_name` is untouched.

Note that a read-only connection to a WAL database still creates `-shm`/`-wal`
sidecars and cannot remove them on close - that is SQLite, not a write, and the
existing `kb_search` path does exactly the same on every search. The test
asserts the WAL is empty and the database bytes are unchanged, which is the
claim that actually matters.

**`grown` is inferred from `updated_at`**, because the list endpoint carries no
message count. It means "changed since it was indexed" - a hint to make
selection sensible, not a prediction. A `grown` row may still come back SKIPPED
if the change did not alter indexable text.

### Not in scope: Markdown export

The reference implementation this borrows from offers an optional Markdown
export alongside the JSON. It is deliberately absent here and should stay that
way unless something changes: the KB indexes conversations and project docs, and
nothing in the pipeline reads Markdown. Adding it would produce files that look
like part of the system but are never ingested - a reading feature wearing a
search feature's clothes. If you want a conversation as prose, the index already
has the text.

### Choosing the organisation

An account can hold more than one organisation. A Claude subscription plus API
console access is the ordinary case, and only the subscription org has the
`chat` capability - the API org answers `chat_conversations` with **403
permission_error**, by design, and always will.

`selectChatOrgs()` therefore picks **by capability**, never by position in the
array and never by a hardcoded uuid. Where several qualify they are returned
sorted by uuid, so the choice is deterministic rather than quietly dependent on
whatever order the API happened to return. Every path that needs an org - single
capture, list, bulk run - goes through the one resolver, so none of them can
select differently from the others.

Three distinct failures, deliberately not merged:

    no organisation has "chat"      no_chat_org  an API-only account looks
                                                 exactly like this
    no organisation has a
    capabilities array at all       shape        the field is gone
    the response is not an array    shape

This was found on the first real run. Iterating organisations in array order
reached the API org, and its 403 was reported as "not signed in" - which sent
someone to check a session that was fine. The single-capture path happened to
work, but only because of the order the array arrived in; that is not a property
worth relying on, so it was moved onto the same resolver.

### Failures are classified, never collapsed

`auth`, `forbidden`, `no_chat_org`, `shape`, `transport`, `notfound`,
`mismatch`, `truncated`, `empty`. The
endpoints are internal and unsupported - observed on one account, one browser,
one day - so the interesting question when this breaks is *which* thing broke.
"Could not export" answers nothing six months later.

The load-bearing distinction: **a missing `chat_messages` key is `shape`; an
empty array is `empty`.** Conflating them would let an API change present as a
conversation with no messages - and a capture of nothing, believed, is the
failure that could replace a real conversation with nothing.

The other one, learned the hard way: **`401` is `auth`; `403` is `forbidden`.**
A 403 means the request was understood and refused - authenticated, but not
permitted. Reporting it as "not signed in" is not a small imprecision, it is a
wrong answer that costs someone an afternoon checking a session that was fine.
`classifyResponse()` is pure and separately tested for exactly this reason: a
classifier that cannot be tested is one that gets to be wrong quietly.

## The MCP extension is generated, not checked in

The repo holds one `claude_kb.py`. `.mcpb` requires the entry point inside the
bundle, so `build_extension.py` stages `kb-extension/build/` at package time:
`manifest.json` rendered from `manifest.example.json` plus the resolved config,
alongside copies of `claude_kb.py` and `kb_config.py`. The build directory is
gitignored.

This is a direct response to a real failure. A second copy of `claude_kb.py`
previously lived in the extension directory and drifted six weeks behind the
root module, missing the entire multi-part export handling. It happened to be
harmless - the extension only ever invokes the `mcp` subcommand, and the missing
functions were on the indexing path - but running `build` from that copy would
have indexed ZERO project docs from a multi-part export, with no error. Two
copies of a file that must agree will not stay in agreement.

### An installed extension lagging the built bundle is not that problem

Building the bundle and installing it are separate acts: `build_extension.py`
regenerates `kb-extension/build/`, and Claude Desktop keeps serving whatever was
last installed until someone installs the new one. So after any change to
`claude_kb.py` the installed extension is a step behind on purpose, until you
choose to reinstall.

That is a different thing from the drift above, and worth keeping straight. The
drift was two source files that were both supposed to be current and silently
were not. This is one source file, plus a deployed artifact whose version is
known and chosen.

What decides whether it matters is which code path changed:

    the serving path      _ro_conn, kb_search, kb_get_conversation, make_match
                          -> reinstall, or Desktop keeps answering with the old
                             behaviour

    the ingest path       update, update-web, upsert_conversations, the reader
                          -> the extension never calls any of it. Reinstalling
                             changes nothing about what Desktop does today.

A run of related changes to the ingest path can therefore be batched behind a
single reinstall at the end. Keep the bundle rebuilt and hash-verified against
the repo and the working install each time regardless, so the only variable is
when the install happens - never whether the bytes agree.

## Gotchas

- Never edit or grep these files with Windows PowerShell 5.1. It reads BOM-less
  UTF-8 as Windows-1252 and silently mangles content while reporting success.
  Use Python.
- The browser must NOT be set to ask where to save each download, or `kb_open.py`
  will sit at its 300s timeout while a save dialog waits for a click.
- Failed parts are LEFT in `incoming/` on purpose so the next run retries them.
  A failed ingest never archives its zips.
- `kb_open.py` without `--yes` is interactive and must never be wired into the
  scheduled task.
- "Expired link" or "has been used" in the browser tab means that URL is spent.
  There is no recovery; request a fresh export.
- A stalled pipeline is SILENT by design: the task exits 0 and logs a reason.
  Check `ingest.log`, not the exit code. This is exactly how an export-format
  change went unnoticed for eleven days.
- `build` WIPES the database. `update` is the normal path and never does.
