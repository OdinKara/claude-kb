# Claude KB

A local, full-text search index over your own exported Claude chat history and
project docs. Python standard library only (`sqlite3` + `json`) for the index;
an MCP server on top so Claude itself can search your past conversations.

Ask "what did I decide about X six months ago" and get the actual message back,
with the conversation id to read the rest of the thread.

---

## WARNING: The database contains everything you have ever said to Claude

`claude_kb.db` holds the **full text of every message in every conversation you
exported**, in plain, greppable form. So do the export zips in `incoming/` and
`processed/`.

**Never commit it. Never share it. Never put it in a bug report.**

The shipped `.gitignore` excludes `*.db`, `*.db.gz`, `*.zip`, `incoming/`,
`processed/`, `staging/`, `data-*/`, `manifest-*.json`, and `*.log` for exactly
this reason. Keep your working directory (`root`) **outside** the repo - that is
the default arrangement here, and it is the reason a stray `git add .` cannot
publish your history.

---

## Status: what has and has not been tested

Honest accounting, because the one untested path is the one that spends
single-use tokens.

**Verified working:**

- config resolution from environment variables, `config.json`, and defaults,
  including the deliberate failure when `root` is unset
- `claude_kb.py update` and `search` against a real index
- `kb_ingest.py` under a real scheduled task, including the empty-input no-op
- the generated MCP extension: bundle builds, manifest passes schema
  validation, and the bundled module answers `kb_search` and starts its stdio
  server cleanly

**Not yet tested against a live export:** the `kb_open.py` download path.

The parameterized browser/canary/download code has not run against a real
manifest since it was parameterized. Every `export_url` is single-use, so this
path cannot be rehearsed - it gets its first real test on the next export, and
until then treat it as unproven. The logic is unchanged from the version that
did work end to end before parameterization, but "unchanged logic" is not the
same as "tested".

## How it works

Anthropic no longer ships one self-contained export zip. A data-export request
now yields a `manifest-*.json` listing one-time `export_url`s for separate
category zips:

```
conversations-000.zip    indexed
projects-000.zip         indexed
memories-000.zip         downloaded, not indexed
light_metadata-000.zip   never fetched (see DEV.md)
```

Part counts grow with account size; never assume there are exactly four.

```
claude.ai settings -> export request -> manifest-*.json
    -> incoming/manifest-*.json      (you drop it there)
    -> kb_open.py                    opens each URL in a real browser, waits for
                                     the zips, validates them, moves them to
                                     incoming/, triggers the ingest task
incoming/*-000.zip
    -> kb_ingest.py                  merge-extracts ALL parts into one tree
    -> claude_kb.py update           incremental upsert, never wipes
    -> processed/<stamp>_*           parts and manifest archived together
```

The pipeline is **semi-automatic by design**. Requesting the export and clicking
through the download is manual; merging, indexing, and archiving are not. See
`DEV.md` for why full automation is not possible - two different approaches were
built, tested, and deleted, and the evidence is written down so nobody has to
repeat them.

---

## Setup

Requires Python 3.10+. No third-party packages for indexing and search. The MCP
server modes (`mcp`, `http`) additionally need `mcp` installed.

```bash
git clone https://github.com/<you>/claude-kb
cd claude-kb
cp config.example.json config.json
# edit config.json: set "root" to your KB working directory
```

Create the working directory and let it fill itself:

```bash
mkdir -p "/path/to/Claude KB/incoming"
python claude_kb.py update /path/to/some-export.zip
python claude_kb.py search "gradle jvm target"
```

`root` is the only required setting. **It has no default on purpose** - there is
no location this tool could guess that would be right for two different people,
and quietly indexing into the wrong directory is worse than refusing to start.
Every script exits with a message naming the missing key, the environment
variable, and the config file it looked in.

---

## Configuration

Resolution order for every key, highest priority first:

1. environment variable - `CLAUDE_KB_ROOT=...`
2. `config.json` - found at `$CLAUDE_KB_CONFIG`, else beside the scripts
3. built-in default, where a safe one exists

| key | env var | default | what it is |
|---|---|---|---|
| `root` | `CLAUDE_KB_ROOT` | **required** | working dir: `claude_kb.db`, `incoming/`, `processed/`, `ingest.log` |
| `downloads` | `CLAUDE_KB_DOWNLOADS` | `~/Downloads` | where `kb_open.py` watches for the browser's downloads |
| `task` | `CLAUDE_KB_TASK` | `ClaudeKB-Ingest` | name of the scheduled task to trigger |
| `python` | `CLAUDE_KB_PYTHON` | `sys.executable` | interpreter used to invoke `claude_kb.py` |
| `browser` | `CLAUDE_KB_BROWSER` | search Edge, then Chrome | full path to a browser **logged into claude.ai** |
| `export_dir` | `CLAUDE_KB_EXPORT_DIR` | newest `data-*/` under root | unpacked export, for `build` only |
| `http_port` | `CLAUDE_KB_HTTP_PORT` | `8760` | localhost port for `claude_kb.py http` |
| `author` | `CLAUDE_KB_AUTHOR` | `Unknown` | name written into the generated extension manifest |

`config.json` is gitignored. `config.example.json` documents every key inline.

**On the browser setting:** a configured browser that does not exist is a hard
error, never a fall-through to the next candidate. Falling back would open a
live one-time URL in a browser you never chose and may not be logged into, which
destroys that part of the export. For the same reason the tool never uses
`webbrowser.open()` - that follows the OS default, which may be a browser you
never sign into.

---

## Usage

```bash
python claude_kb.py update <export-dir|zip>   # incremental upsert (never wipes)
python claude_kb.py search "TERMS"
python claude_kb.py build                     # full rebuild - WIPES; rarely needed
python claude_kb.py mcp                       # stdio MCP server
python claude_kb.py http                      # localhost streamable-http MCP

python kb_open.py                             # fetch + ingest (interactive)
python kb_open.py --yes                       # same, non-interactive (canary)
python kb_open.py --reset-canary              # clear a stuck canary result
python kb_ingest.py                           # ingest whatever is in incoming/
```

### Collecting an export

1. claude.ai -> Settings -> Privacy -> export data. Anthropic emails a link that
   yields a `manifest-*.json`.
2. Drop that manifest in `<root>/incoming/`.
3. Make sure your browser is logged into claude.ai **and set to not ask where to
   save each file** - otherwise `kb_open.py` sits at its timeout while a save
   dialog waits for a click.
4. `python kb_open.py` (or `--yes` to run unattended).

`--yes` does not simply skip the confirmation prompt. It spends the
`light_metadata` URL first as a **login canary**, because that part is never
indexed and so is the only worthless token in the manifest. If it downloads, the
browser is logged in and the valuable URLs are safe to open. If it does not,
nothing else is touched.

The canary result is remembered per manifest in `.canary-state.json`, because
the canary URL is single-use like every other: on a second run the already-spent
URL produces no download, which looks *identical* to a logged-out browser.
Nothing can tell those two apart from outside, so the script reports the
observation rather than guessing a cause, and refuses to spend the token again
to re-learn the same non-answer. Clear it deliberately with `--reset-canary`
once you know the login is good.

Exit codes:

| code | meaning |
|---|---|
| 0 | parts downloaded and ingest confirmed in `ingest.log` |
| 1 | nothing to do, or ingest not confirmed |
| 2 | environment problem (browser not found, no manifest, missing setting) |
| 3 | canary failed - browser not logged in, or that URL already spent |

**Every `export_url` is single use.** Do not re-run hoping a failed part
succeeds on a second try; a spent URL needs a fresh export. Do not try to fetch
them with curl, `requests`, or a headless browser - `DEV.md` documents why that
cannot work and what it costs to find out again.

---

## Scheduled ingest

`kb_ingest.py` is meant to run unattended on a timer. On Windows:

```powershell
schtasks /create /tn "ClaudeKB-Ingest" /tr "\"C:\path\to\python.exe\" \"C:\path\to\kb_ingest.py\"" /sc daily /st 06:00
schtasks /run /tn "ClaudeKB-Ingest"
```

On macOS/Linux, a cron entry calling `kb_ingest.py` does the same job; set
`task` to a command your platform can trigger, or run `kb_ingest.py` directly.

**The scheduled task never contacts an export URL.** It cannot succeed from a
non-interactive context, and every attempt burns a one-time token. If a manifest
is sitting in `incoming/` with no part zips, it logs
`manifest waiting - run kb_open.py to download the parts` and does nothing else.

**A stalled pipeline is silent by design** - the task exits 0 and logs a reason.
Check `ingest.log`, not the exit code. An export-format change once went
unnoticed for eleven days precisely this way.

---

## MCP extension

The repo holds **one** `claude_kb.py`, at the root. `.mcpb` requires the entry
point to live inside the bundle, so the extension directory is *generated*
rather than checked in:

```bash
python build_extension.py            # writes kb-extension/build/
python build_extension.py --print    # show resolved values, write nothing
```

That produces `kb-extension/build/` containing `manifest.json` - rendered from
`manifest.example.json` with your resolved config substituted in - plus copies
of `claude_kb.py` and `kb_config.py`. Load that directory in Claude Desktop, or
pack it with `mcpb pack kb-extension/build`.

Generating the manifest rather than shipping placeholders is deliberate: a
second checked-in copy of `claude_kb.py` is exactly what drifted before, sitting
six weeks behind the root module, and hand-edited placeholders are a step people
get wrong.

**Manual fallback.** If you would rather not run the build step, copy
`kb-extension/manifest.example.json` to `kb-extension/build/manifest.json`,
copy `claude_kb.py` and `kb_config.py` in beside it, and replace the four
placeholders by hand:

| placeholder | value |
|---|---|
| `{{ROOT}}` | your KB working directory |
| `{{DB_PATH}}` | `<root>/claude_kb.db` |
| `{{PYTHON}}` | full path to your Python interpreter |
| `{{AUTHOR}}` | your name |

Backslashes must be JSON-escaped (`C:\\Users\\...`); the build step does this
for you and validates the result before writing it.

The extension exposes two tools:

- `kb_search(query, limit)` - ranked hits with title, date, sender, source,
  `conversation_uuid`, and a highlighted snippet.
- `kb_get_conversation(conversation_uuid, max_messages)` - the full chat, in
  order, after you have found it.

Both open the index **read-only**; the server cannot modify your database.

---

## Claude Code integration

**Optional.** Everything above works standalone from a terminal. This section
only adds ergonomics: a `/kb-update` slash command, and natural-language
triggers so "update the KB" routes to the right workflow instead of Code
improvising one.

Two template files live in `claude/`. Both need placeholders filled in; neither
is used by the scripts themselves.

### 1. The slash command

Copy `claude/kb-update.md` to your **user-scoped** commands directory:

| OS | destination |
|---|---|
| Windows | `C:\Users\<you>\.claude\commands\kb-update.md` |
| macOS / Linux | `~/.claude/commands/kb-update.md` |

Then edit the two placeholders at the top: `<PYTHON>` (full path to your
interpreter) and `<CLAUDE_KB_SCRIPTS>` (the directory holding `kb_open.py`).
Every other path is resolved through `kb_config.py` at run time, so there is
nothing else to keep in sync.

**User-scoped, not project-scoped.** A command placed in a project's
`.claude/commands/` only resolves when the session's working directory is inside
that project. Updating the KB is something you do from wherever you happen to
be, so it belongs in `~/.claude/commands/`, where it resolves from any
directory.

### 2. The natural-language routing

The slash command alone gives you `/kb-update`. To make plain phrasings work,
append the block in `claude/CLAUDE.snippet.md` to your own `~/.claude/CLAUDE.md`
and fill in its two paths. That block carries the trigger phrases, the
instruction to read the command file rather than act on a remembered summary,
the run-from-the-current-directory rule, and the prohibition on writing an HTTP
downloader for export URLs.

That last one has to live in `CLAUDE.md` rather than only in the command file,
because it needs to be visible *before* Code opens the command file. A session
that has already decided to improvise a downloader is not going to read the
instructions first.

Without the snippet, the slash command still works; you just have to type it.

---

## Notes

- The index is incremental. `update` upserts and never wipes, so re-ingesting an
  overlapping export is safe and cheap.
- FTS5 is used when SQLite has it, with an automatic fall-back to FTS4.
- `build` is a clean-slate rebuild that **deletes and recreates** the database.
  You almost never want it; `update` is the normal path.

See `DEV.md` for architecture, and for two automation dead ends documented with
their evidence so they do not get rebuilt.

## License

MIT - see `LICENSE`.
