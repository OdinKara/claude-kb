---
description: Set up this clone of Claude KB - index, search, ingest task, and optionally browser capture
---

Walk the user through setting up this repo, following `README.md`. This command
is project-scoped and only ever runs from inside the clone.

**Read the README section for each phase before doing it.** Do not work from
what you remember of this file - it routes, the README instructs. If the two ever
disagree, the README is right.

Work through the phases in order. Stop at every point marked HAND OVER, ask, and
wait - do not guess a value, and do not report a step as done that you could not
perform.

---

## Phase 0 - prerequisites, reported all at once

Check and report the whole picture before starting, so the user is not told about
a missing dependency three steps in:

- `python --version` - 3.10+ required.
- `python -c "import mcp"` - needed only to serve the index to Claude. Absent is
  fine for now; note it.
- `node --version` - needed only to pack a `.mcpb`. **Absent is not a blocker**:
  README's *Route B* registers the MCP server directly in
  `claude_desktop_config.json`. Say which route you will take.
- Ask which browser they use, and whether it permits unpacked extensions.
  Managed or policy-locked browsers often do not - that rules out browser
  capture only, and nothing else.
- Confirm the platform. The native host, both installers and the scheduled task
  are **Windows only**. On macOS or Linux, do phases 1-3 and say plainly that
  4-6 are unavailable.

Ask what the user actually wants before building anything:

- **the index and CLI search** - phases 1-2
- **plus search from Claude Desktop** - add phase 3
- **plus browser capture** - add phases 4-6

## Phase 1 - configuration

Follow README **Setup**. Copy `config.example.json` to `config.json` and set
`root`.

HAND OVER: ask where the KB working directory should be. Do not pick one. It
holds the database and the export archive, it should not be inside the repo, and
`root` has no default precisely because no guess is right for two people.

Verify: `python claude_kb.py` with no arguments prints usage rather than a
missing-setting error.

## Phase 2 - build the index

The user needs a Claude data export.

**Do not request or download one without explicit permission in this session.**
Export URLs are single-use; a wasted attempt costs them a whole new export
request. Ask whether they already have one.

- If they have an export: `python claude_kb.py update <path-to-export-or-zip>`.
- If they do not, and they ask you to fetch one: only then follow README
  **Collecting an export**, and only after they have dropped a `manifest-*.json`
  into `<root>/incoming/` themselves. `/kb-update` is the supported route.
- **Never** attempt to download an export URL with an HTTP client. See the hard
  rules in `CLAUDE.md`.

Verify: `python claude_kb.py search "<a word they expect to appear>"` returns
hits. Report the row count.

## Phase 3 - the ingest task, and search from Desktop

Follow README **Scheduled ingest**. Run `install-task.ps1`. This is required, not
optional: `kb_open.py` triggers this task by name, so without it a later fetch
downloads the parts, spends their single-use URLs, and has nothing to hand them
to.

Verify: trigger it once and confirm `ingest.log` ends with `no new export` on an
empty `incoming/`. That is a clean result, not a failure.

Then, if they want Desktop search, follow README **MCP extension**:
`python build_extension.py`, then Route A (pack a `.mcpb`) or Route B (no Node).

HAND OVER: **installing the `.mcpb` in Claude Desktop is theirs to do.** You can
build and pack it. Ask them to install it and restart Desktop, then to confirm
`kb_search` appears in a fresh session.

## Phase 4 - load the extension

Follow README **From clone to capturing**.

Before anything else, tell them: **the clone must live where it will stay.** An
unpacked extension's ID is derived from its path, so moving the folder later
issues a new ID and silently breaks the host registration.

HAND OVER: ask them to open their browser's extensions page, enable developer
mode, *Load unpacked*, select the `extension/` directory, and paste back the
32-character ID from its card. You cannot read it.

## Phase 5 - register the native host

With that ID, run `native/install-host.ps1` with `-ExtensionId` and
`-ScriptsDir`. Report which browsers it registered and which it skipped as not
installed.

If it reports Firefox: it is genuinely unsupported - different registry path and
a different manifest schema - not something to work around.

## Phase 6 - verify the capture path

HAND OVER: ask them to reload the extension and open its popup.

- It should say **Host ready** and name the `incoming/` directory.
- If it says the host is unavailable, the message names the likely cause. Check
  the ID matches what was registered, and run `native/host.cmd` in a terminal -
  it should sit waiting for input rather than exiting.

Then ask them to open a conversation on claude.ai and try **Capture + Ingest**,
and to report what the popup said. Interpret it against README **What the popup
is telling you** - in particular:

- **Unchanged** and **PARTIAL** are correct, successful outcomes, not failures.
  PARTIAL means the capture had fewer messages than what is already indexed, so
  it was held back; nothing was lost.
- **Not permitted - and you ARE signed in** is the two-organization case, not a
  session problem.

---

## Report at the end

- what was set up, and what was skipped because they did not want it or the
  platform does not support it
- the index row count and a search that returned hits
- whether the ingest task exists and logged a clean run
- whether `kb_search` answers in Desktop, or that it is still pending their
  install
- whether the popup says Host ready, or what it said instead

Do not report a HAND OVER step as complete on their behalf. If they have not
confirmed it, say it is pending.
