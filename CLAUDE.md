# Claude KB - instructions for Claude Code

A local SQLite FTS index over the user's own exported Claude conversations and
project docs, plus an MCP server so Claude can search them and an optional
browser extension that captures conversations without waiting for an export.

**`README.md` is authoritative for setup.** Follow its ordered sequences step by
step rather than improvising one, and read the relevant section before acting.
Nothing here restates those steps on purpose - a second copy would drift, and
the one that drifts is always the one someone follows.

- setting up the index and search: **From clone to searchable in Claude**
- setting up browser capture: **From clone to capturing**
- the ingest task, which is required, not optional: **Scheduled ingest**
- `/kb-setup` walks all of it and stops where a human is needed

`DEV.md` explains why things are built the way they are. Read it before changing
behaviour, and especially before "simplifying" anything that looks redundant -
most of it is load-bearing and the reasons are written down.

## Hard rules

**Never request or download a Claude data export without explicit permission
from the user in this session.** Every `export_url` in a manifest is
SINGLE-USE. A wasted attempt does not fail politely and retry - it destroys that
part of the export and the user has to request an entirely new one and wait for
it. Running `kb_open.py` spends real tokens. Ask first, every time.

**Never write an HTTP client to fetch export URLs.** Not with `curl`, `requests`,
`urllib`, WebFetch, Playwright, or a headless browser. It cannot work: those URLs
return an HTML page whose JavaScript starts the transfer, even with a fully valid
session cookie. This has been built, tested and deleted twice; `DEV.md` records
the evidence under "Two dead ends". `kb_open.py` drives a real browser and is the
only supported path. Every attempt costs a one-time token to relearn the same
thing.

**Two organizations is normal, and a 403 is not a logout.** An account with both
a Claude subscription and API console access has two organizations, and only the
one whose capabilities include `chat` serves conversations - the API one answers
with `403 permission_error` by design, forever. Select by capability, never by
array order. A 403 means authenticated but not permitted; reporting it as "not
signed in" sends someone to check a session that is fine.

**Windows only** for the native messaging host, both installers, and the
scheduled task. Indexing, search and the MCP server are cross-platform. Say so
rather than letting a macOS or Linux user get halfway.

**Never commit the database or an export.** `claude_kb.db` holds the full text of
every message the user has ever sent Claude. `.gitignore` covers it and the
export artifacts; do not add exceptions.

## What you cannot do, and must hand over

Stop and ask rather than claiming these are done or pretending to do them:

- **Loading the extension unpacked and reading its ID.** This needs the browser's
  extensions page. Ask the user to load `extension/`, turn on developer mode, and
  paste back the 32-character ID from its card.
- **Installing the `.mcpb` in Claude Desktop.** You can build and pack it; the
  install is a Desktop action.
- Anything else needing a browser UI or a Desktop restart.

## Check before starting, and report what you find

- **Python 3.10+** - required for everything.
- **`pip install -r requirements.txt`** - only to serve the index to Claude,
  not to index or capture. It pins `mcp<2` on purpose: 2.x renamed the API
  this project builds on, and an unpinned install fails with an ImportError
  that reads like a bug here.
- **Node** - only to pack a `.mcpb`. If it is missing or npm is blocked by
  policy, do not treat setup as blocked: README's **Route B** registers the
  server directly in `claude_desktop_config.json` and needs no Node at all.
- **Whether the browser permits unpacked extensions.** Managed or
  policy-locked browsers often forbid them, which makes browser capture
  unavailable - that does not affect the index, search, or the MCP server.

Report the whole picture before starting rather than discovering it a step at a
time.

## Testing

Two rules, both in `DEV.md` under "Two rules for tests here", both paid for:
assert on rendered output rather than on the absence of exceptions wherever code
catches its own errors, and verify a new test against a deliberately broken copy,
since a test that passes on both proves nothing.

Never run tests against the user's live `claude_kb.db`. Copy it to a scratch
directory and point an isolated root at the copy, the way the existing suites do.
