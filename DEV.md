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

The scheduled task NEVER contacts an export URL. It cannot succeed from a
non-interactive context and every attempt burns a one-time token. If a manifest
is sitting in `incoming/` with no part zips, the task logs `manifest waiting -
run kb_open.py to download the parts` and does nothing else.

`kb_open.py` does not just fire the scheduled task and walk away. After
triggering it, it polls `ingest.log` for up to 120s for a genuinely new line and
prints it, so the caller sees the actual
`SUMMARY NEW=.. UPDATED=.. SKIPPED=.. ROWS=..` rather than a silent success.

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
