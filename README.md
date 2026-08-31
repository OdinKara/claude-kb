# Claude KB

A local, full-text search index over your own exported Claude chat history and
project docs. Python standard library only (`sqlite3` + `json`) for the index;
an MCP server on top so Claude itself can search your past conversations.

Ask "what did I decide about X six months ago" and get the actual message back,
with the conversation id to read the rest of the thread.

---

## Quick start

```bash
git clone https://github.com/OdinKara/claude-kb
cd claude-kb
```

Now open Claude Code in that directory and say **"set this up"**, or run
**`/kb-setup`**.

It checks your prerequisites, asks what you actually want, builds the index and
wires up search, and stops to ask you for the two things it cannot do itself:
**loading the extension unpacked and reading its ID**, and **installing the
`.mcpb` in Claude Desktop**. It never requests a data export without asking -
those URLs are single-use.

*Not using Claude Code?* Everything below is the same setup done by hand, and
remains the source of truth.

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

## Status: verified end to end

The whole chain has been exercised against a real account, not just unit-tested
in pieces:

- **Configuration.** Resolution from environment variables, `config.json`, and
  built-in defaults, including the deliberate refusal to start when `root` is
  unset.
- **Collection.** `kb_open.py --yes` against a live manifest: the canary proved
  the login, the browser was resolved from configuration, and every part
  downloaded and passed zip validation before being moved into `incoming/`.
- **Ingest.** The multi-part merge, the incremental upsert, and the archive step,
  driven by the real scheduled task under its configured name - including the
  empty-input case, which logs its reason and exits cleanly rather than
  erroring.
- **Search.** `claude_kb.py search` against a populated index.
- **MCP extension.** The bundle builds from a single source module, the
  generated manifest passes schema validation, and the packed extension
  installed in Claude Desktop answers `kb_search` correctly in a fresh session.

The collection step is the one that cannot be rehearsed: every `export_url` is
single-use, so each test of that path costs a real export. It has been run, and
it works. If you change that code, you get one attempt per export to find out
whether you broke it - which is why the canary exists, and why it spends the one
token in the manifest that is already worth nothing.

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
                                     incoming/, then ingests
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

### Prerequisites

| for | you need |
|---|---|
| indexing and searching from the CLI | Python 3.10+, standard library only |
| the MCP server modes (`mcp`, `http`) | `pip install -r requirements.txt` (pins `mcp<2`) |
| packing a Desktop extension *(optional)* | Node.js, for the `mcpb` CLI |

Only the first row is mandatory. Nothing about indexing or searching needs a
third-party package; `mcp` is required solely to serve the index to Claude, and
Node solely to package that server as a Desktop extension. There is a route that
skips Node entirely - see [Installing it in Claude Desktop](#installing-it-in-claude-desktop).

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

## Scheduled ingest (optional)

**You do not need this.** The normal workflow is running `kb_open.py` when you
actually want an update, and finishing with `python kb_ingest.py`. This task
only removes that second command, and picks up anything left in `incoming/`
overnight.

`kb_open.py` tries to trigger it and, when there is no such task, simply tells
you the one command that finishes the job. That is a normal outcome, not an
error.

If you want it, on Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install-task.ps1
```

Run from the repo directory it takes no arguments: the task name comes from
`task` in `config.json`, the interpreter from `python`, both falling back to the
same defaults `kb_config` uses. `-ScriptsDir`, `-Python`, `-TaskName` and
`-Time` override; `-Uninstall` removes it. It builds the command with both paths
quoted (either can contain spaces - getting that wrong is the classic way this
task ends up created but broken), then **queries the task back and checks it
really runs `kb_ingest.py`** rather than trusting the exit code.

### What this task can and cannot do

`schtasks` creates it **without stored credentials**, which means Logon Mode
*Interactive only*: it runs only while its own account is logged on
interactively. That is worth knowing before you rely on it - a daily 06:00
ingest will not happen on a machine you log out of.

The installer checks this rather than leaving you to find out:

- registered as **the interactive user** - works, but it will **not** run at its
  scheduled time if you are logged off. The installer says so.
- registered as **some other account** - it will never fire. The installer fails
  loudly rather than reporting success, and names the fix.

To have it run regardless of who is logged on, give it stored credentials:

```powershell
schtasks /change /tn "ClaudeKB-Ingest" /ru <user> /rp <password>
```

Confirm it works:

```powershell
schtasks /run /tn "ClaudeKB-Ingest"
```

With an empty `incoming/` the last line of `ingest.log` should read
`no new export`. That is a clean result, not a failure.

The equivalent by hand, if you would rather:

```powershell
schtasks /create /tn "ClaudeKB-Ingest" /tr "\"C:\path\to\python.exe\" \"C:\path\to\kb_ingest.py\"" /sc daily /st 06:00
```

On macOS/Linux there is no installer yet; a cron entry or systemd timer calling
`kb_ingest.py` does the same job. Nothing in the pipeline cares what starts it -
but note that `kb_open.py` triggers the task with `schtasks`, so on those
platforms run `kb_ingest.py` yourself after a fetch.

**If the task is missing**, `kb_open.py` now says so immediately rather than
waiting out its timeout and reporting that the ingest was triggered when it was
not. The downloaded parts stay safe in `incoming/`; create the task and re-run,
or run `python kb_ingest.py` directly to ingest what is already there.

**The scheduled task never contacts an export URL.** It cannot succeed from a
non-interactive context, and every attempt burns a one-time token. If a manifest
is sitting in `incoming/` with no part zips, it logs
`manifest waiting - run kb_open.py to download the parts` and does nothing else.

**A stalled pipeline is silent by design** - the task exits 0 and logs a reason.
Check `ingest.log`, not the exit code. An export-format change once went
unnoticed for eleven days precisely this way.

---

## MCP extension

**No prebuilt `.mcpb` ships with this repo, and none can.** Do not go looking for
a release artifact - there isn't one, and its absence is not an oversight. An
MCP extension manifest has to carry *literal absolute paths*: your Python
interpreter, your KB root. A bundle is therefore specific to the machine that
built it, and a checked-in one would be wrong for every reader. You build your
own, and it is one command.

### From clone to searchable in Claude

The whole path, start to finish. Steps 1-4 are covered under
[Setup](#setup); 5-6 are below; 7 is optional ergonomics.

1. `git clone` this repo and `cd` into it.
2. `cp config.example.json config.json`, then set `root` to your KB working
   directory.
3. `pip install -r requirements.txt` - needed to serve the index, not to
   build it. It pins `mcp<2`; see [Prerequisites](#prerequisites).
4. Index an export: `python claude_kb.py update <export-dir|zip>`. Confirm with
   `python claude_kb.py search "some term"`.
5. `python build_extension.py` - generates `kb-extension/build/`.
6. Install that in Claude Desktop, by one of the two routes below.
7. *Optional:* install the `claude/` templates for ingest and search ergonomics -
   see [Claude Code integration](#claude-code-integration), and the
   [scheduled ingest task](#scheduled-ingest-optional) if you want ingests to
   happen without you.

After step 6, restart Desktop and ask it something that should hit your history.
If `kb_search` does not appear in a fresh session, the extension is not loaded.

### What the build step produces

```bash
python build_extension.py            # writes kb-extension/build/
python build_extension.py --print    # show resolved values, write nothing
```

`kb-extension/build/` contains `manifest.json` - rendered from
`manifest.example.json` with your resolved config substituted in - plus copies of
`claude_kb.py` and `kb_config.py`.

Generating the manifest rather than shipping placeholders is deliberate: a
second checked-in copy of `claude_kb.py` is exactly what drifted before, sitting
six weeks behind the root module, and hand-edited placeholders are a step people
get wrong.

### Installing it in Claude Desktop

Two routes. **Route A** produces a single installable file and needs Node.
**Route B** needs nothing beyond what you already have. Both end with the same
server running; pick on whether Node is available to you.

Desktop's interface changes between releases, so the shape of each flow is
described rather than a menu path that will go stale.

#### Route A: pack a `.mcpb` (needs Node)

The packer is `@anthropic-ai/mcpb`, distributed on npm, so Node.js is a
prerequisite for this route only:

```bash
npm install -g @anthropic-ai/mcpb
mcpb pack kb-extension/build claude-kb.mcpb
```

It validates the manifest against the schema before writing, so a malformed
manifest fails here rather than silently inside Desktop.

To install the resulting file: Desktop treats a `.mcpb` as an installable
artifact, so opening the file directly hands it to Desktop, which will ask you to
confirm. The same thing is reachable from Desktop's settings, in the area that
manages extensions, which offers installing one from a local file. Either way,
**restart Desktop afterward** and confirm `kb_search` exists in a fresh session.

If you are replacing an earlier build, remove the old extension first rather than
installing over it - two copies of the same server registering the same tool
names is ambiguous, and you will not be able to tell which one answered.

#### Route B: no Node, or npm blocked by policy

You do not need `mcpb` at all. The generated `manifest.json` already contains
exactly the launch configuration Desktop needs, so you can register the server
directly in `claude_desktop_config.json` instead:

```json
{
  "mcpServers": {
    "claude-kb": {
      "command": "<the command from your generated manifest.json>",
      "args": ["<path-to>/kb-extension/build/claude_kb.py", "mcp"],
      "env": { "CLAUDE_KB_ROOT": "<your KB root>" }
    }
  }
}
```

Copy the three values straight out of `kb-extension/build/manifest.json` -
`server.mcp_config.command`, `args`, and `env` - replacing the `${__dirname}`
placeholder in `args` with the real path to `kb-extension/build/`. Restart
Desktop.

**What a blocked npm looks like,** so you can recognise it as an environment
problem rather than a KB one:

- `npm install -g @anthropic-ai/mcpb` fails with `EACCES`, `403`, `ETIMEDOUT`,
  `ECONNREFUSED`, or `request to https://registry.npmjs.org/... failed` - a
  proxy, an offline machine, or a policy blocking the public registry.
- An internal mirror returns `404 Not Found` for the scoped package because it
  does not mirror `@anthropic-ai`.
- The install reports success but `mcpb: command not found` - the npm global bin
  directory is not on your `PATH`.

None of these are failures of the index. Indexing, CLI search, and the MCP server
itself are all unaffected; only the packaging convenience is unavailable. Take
Route B.

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
adds ergonomics in both directions: getting data *in* (a `/kb-update` slash
command and natural-language triggers, so "update the KB" routes to the right
workflow instead of Code improvising one) and getting it *out* (Claude
consulting your indexed history on its own when you ask about your past work).

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

`claude/CLAUDE.snippet.md` holds two independent blocks to append to your own
`~/.claude/CLAUDE.md`. Take either or both, and fill in the two paths.

**Block 1 - ingest.** The slash command alone gives you `/kb-update`; this makes
plain phrasings work too. It carries the trigger phrases, the instruction to read
the command file rather than act on a remembered summary, the
run-from-the-current-directory rule, and the prohibition on writing an HTTP
downloader for export URLs.

That last one has to live in `CLAUDE.md` rather than only in the command file,
because it needs to be visible *before* Code opens the command file. A session
that has already decided to improvise a downloader is not going to read the
instructions first.

Without this block, the slash command still works; you just have to type it.

**Block 2 - search.** This is the half that makes the index worth having in
conversation. It tells Claude to consult the KB *unprompted* when you refer to
your own past work - what you decided, why something is built the way it is, an
error you say you have hit before - rather than answering from a cold guess. It
also covers following a hit through with `kb_get_conversation`, because a search
snippet is a fragment and the turns around it often qualify it.

Search guidance belongs in `CLAUDE.md` for the same structural reason as the HTTP
prohibition: the decision to search is the thing being guided, so the instruction
has to be resident *before* that decision, not fetched after it. A file Claude
only opens once it has already decided to search is too late to be useful.

The block is explicitly conditional on `kb_search` being present in the tool
list. Installing the extension and appending this snippet are separate steps, and
either can be skipped - without the guard, doing one but not the other leaves
Claude instructed to call a tool that does not exist. When it is absent, the
correct behaviour is to say the KB is not connected, not to improvise a search or
imply one happened.

It also sets expectations about output: these are your own private chats, so the
block asks for a summary, a date, and a pointer rather than long verbatim
excerpts or an exhaustive list of every hit.

---

## Browser capture (optional)

The export pipeline above is the authoritative path, and it stays that way. This
adds a second, lighter one: a browser extension that captures the conversation
you are reading straight into `incoming/`, so a chat is searchable without
waiting for the next data export.

> **These endpoints are internal and undocumented.** They are what claude.ai's
> own web app calls, observed working on one account, in one browser, on one
> day. That is a snapshot, not a spec: no version header, no deprecation notice,
> no promise they exist next month. **This is the part of the project most
> likely to need fixing after a Claude web update** - the index, the ingest
> pipeline and the search are unaffected by any of it.
>
> The extension is built to *fail loudly and specifically* when they change -
> see [What the popup is telling you](#what-the-popup-is-telling-you) - and to
> write nothing when it cannot verify what it got.

> **Windows only.** The native host is a `.cmd` launcher and the installer is
> PowerShell writing to the registry. macOS and Linux use a different mechanism
> entirely (a JSON manifest in a per-browser directory), so neither is supported
> yet. Everything else in this repo - indexing, search, the MCP server - is
> cross-platform.
>
> **Chromium browsers only.** Chrome, Edge, Brave, Vivaldi and Opera share the
> native-messaging manifest format and are all registered by the installer.
> **Firefox is not supported**: it uses a different registry location *and* a
> different manifest schema (`allowed_extensions` with an add-on id, rather than
> `allowed_origins` with a `chrome-extension://` URL), and this extension
> declares no Firefox id. The installer detects Firefox and says so rather than
> writing something that cannot work. **Safari cannot work at all** - it uses a
> different extension model that has no native-messaging equivalent of this
> shape.

### Why there is no DOM fallback

There was one in the design, and it was cut deliberately. The API returns the
whole transcript in one response with an honest message count; a scrape of a
virtualised transcript silently under-counts. An under-count is exactly the
input the ingest side's shrink guard rejects, so a scrape that *looked* complete
would produce captures held back forever. Failing loudly beats degrading to a
lossy method that can quietly damage the index.

### From clone to capturing

The whole path. Steps 1-4 are the same ones as
[From clone to searchable](#from-clone-to-searchable-in-claude); 5-9 are the
capture path. The extension is useless without the native host, because a
browser extension cannot write to a folder or start a process.

1. `git clone` this repo and `cd` into it.
2. `cp config.example.json config.json`, then set `root` to your KB working
   directory.
3. `pip install -r requirements.txt` *(only needed to serve the index to
   Claude, not to capture)*.
4. Build the index from an export: `python claude_kb.py update <export-dir|zip>`.
   Confirm with `python claude_kb.py search "some term"`. **Do this first** -
   without an index the extension cannot label anything as already captured.
5. **Put the repo where it will stay.** See the warning below; moving it later
   breaks the registration.
6. **Load the extension.** Open your browser's extensions page, turn on
   developer mode, choose *Load unpacked*, and select the `extension/`
   directory.
7. **Copy the extension ID** from its card - 32 letters.
8. **Register the native host** with that ID:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File native\install-host.ps1 `
       -ExtensionId <id from the extension card> `
       -ScriptsDir  "C:\path\to\claude-kb"
   ```

   It generates `native\host.cmd` and the host manifest from your values, then
   registers them under HKCU **for each Chromium browser it finds installed**,
   and reports which it registered and which it skipped as absent. The
   interpreter comes from `config.json` if present; pass `-Python` to override.
   Both generated files are gitignored - they hold absolute paths specific to
   your machine.

9. **Reload the extension** and open its popup. It should say **Host ready** and
   name the `incoming/` directory it will write to.

If it does not, run `native\host.cmd` directly in a terminal: it should sit
waiting for input rather than exiting with an error.

To remove it: `install-host.ps1 -Uninstall -ExtensionId <id> -ScriptsDir <dir>`.

> ### WARNING: do not move the extension folder after registering
>
> The extension is not in any store, so it is loaded **unpacked** - and an
> unpacked extension's ID is **derived from the path it was loaded from**. Move
> or rename that folder, reload, and the browser assigns a **new ID**. The host
> registration still names the old one, so the host refuses to talk to it and
> the popup reports it as unavailable.
>
> That failure looks nothing like its cause, which is why it is worth knowing in
> advance. The popup's message names this specifically and prints the extension's
> current ID, so the fix is to re-run `install-host.ps1` with that ID.
>
> Pick a permanent home for the clone *before* step 6.

### Using it

Open a conversation on claude.ai and click the extension.

- **Capture + Ingest** - write the capture and index it immediately.
- **Capture only** - write it to `incoming/` and leave it for the scheduled run.

The capture is taken with `tree=True`, which returns the whole message tree
including branches you regenerated away from. That is deliberate: the official
export ships the whole tree too, and capturing only the active path would make
every branched conversation look short to the shrink guard.

### Capturing several at once

**Load chat list** pages through your conversations and shows them most recent
first, each labelled against what the KB already holds:

| Label | Meaning |
|---|---|
| **new** | not indexed at all |
| **grown** | indexed, but the conversation has changed since |
| **indexed** | indexed, and unchanged since |

`grown` is inferred from `updated_at` - the list endpoint carries no message
count - so it means "changed since it was indexed". It is a hint to make
selection sensible, not a prediction: a `grown` row can still come back
**unchanged** if the change did not alter indexable text.

*select new* ticks everything new or grown, which is usually what you want.
Then use the same two buttons as the single-conversation pair above:
**Capture + Ingest** writes the captures **and indexes them**, and **Capture
only** writes them to `incoming/` for the scheduled run to pick up. Both are
labelled with the number selected.

**Runs are paced on purpose.** Half a second between conversations, a quarter
second between list pages, and a cap of 25 conversations per run. These are
internal endpoints and a tight bulk loop is the behaviour most likely to be
throttled, so the run is slow by design - start it and come back.

**Selecting more than the cap is normal.** The button says what it will do -
*Capture next 25 of 156* - and when the batch finishes the remaining 131 are
**still selected**, the labels update so the ones just captured now read
*indexed*, and the button becomes *Capture next 25 of 131*. Press it again to
continue. There is no reloading or reselecting between batches.

If you close the popup mid-sequence, the pending conversations are remembered:
reopen it, click **Load chat list**, and they are reselected for you.

**Every selected conversation gets an outcome**, listed individually. If the run
stops early - an authentication failure, say - the ones after that point are
marked *not attempted* rather than silently dropped, and whatever was captured
before the stop is still ingested. The report survives closing the popup: reopen
it and the last run is still there.

The labels need the native host (it answers a read-only query for what is
indexed). If the host is unreachable the list still loads, just unlabelled.

**If your account has more than one organisation, this matters.** A Claude
subscription plus API console access gives you two, and **only the subscription
one serves conversations** - the API organisation answers the same endpoint with
`403 permission_error`, by design, forever. The extension selects by
*capability* rather than by whichever the API happens to list first, so this
needs nothing from you.

It is called out because it costs a debugging cycle when it goes wrong: an
implementation that iterates organisations in array order will hit the API one,
and a `403` reported as "not signed in" sends you to check a session that is
perfectly fine. If you see **Not permitted - and you ARE signed in**, that is
this, not your login. An API-only account with no subscription is reported as
**no chat organisation**, which is again a different thing from being signed
out.

### What the popup is telling you

The outcomes are kept distinct on purpose. A capture that was held back or
refused must never read as a success.

| Outcome | Meaning | Did anything change? |
|---|---|---|
| **Ingested** | Indexed as new or updated. | Yes |
| **Saved (not ingested)** | Written to `incoming/`, waiting for a run. From *Capture only*. | Not yet |
| **Already indexed, unchanged** | Identical to what is stored. | No, and nothing was lost |
| **Held back - PARTIAL** | Fewer messages than the copy already indexed, so it was not allowed to replace it. Normal when an export already covers this chat. | No, and nothing was lost |
| **Partly ingested** | Several captures, mixed outcomes. Refused files stay in `incoming/`. | Some |
| **Refused** | Failed validation. The file stays in `incoming/` so you can look at it. | No |

And when the capture never happened at all, the reason is classified rather than
collapsed into "could not export" - six months from now the distinction is what
saves the investigation:

| Reason | What it actually means |
|---|---|
| **Not signed in** | `401`, or an HTML login page. Sign in and retry. |
| **Not permitted - and you ARE signed in** | `403`. The request was understood and refused, which is *not* a session problem. Do not go looking at your login. |
| **No chat organisation on this account** | None of the account's organisations has the `chat` capability, so none holds conversations. An API-only account looks like this. |
| **The API shape has changed** | A `200` whose body is missing fields this expects. **This is the one that means the extension needs updating.** Nothing was written. |
| **Could not reach claude.ai** | Network-level: offline, DNS, timeout. Not auth, not an API change. |
| **Identity mismatch - refused** | The API returned a different conversation than the URL names. Capturing it would file it under the wrong identity. |
| **Transcript incomplete - refused** | At least one message came back `truncated`, so the text is incomplete. |
| **Nothing to capture** | The conversation genuinely has no indexable messages. |

A missing `chat_messages` field is reported as a **shape change**, never as an
empty conversation. Conflating those is the failure that could let a capture
replace a real conversation with nothing.

---

## Notes

- The index is incremental. `update` upserts and never wipes, so re-ingesting an
  overlapping export is safe and cheap.
- A conversation is only shortened by a writer entitled to shorten it. The
  official export is a complete snapshot and may; anything else must bring at
  least as many messages as are already indexed, or the update is reported as
  `PARTIAL` and the fuller version is kept. See `DEV.md`.
- FTS5 is used when SQLite has it, with an automatic fall-back to FTS4.
- `build` is a clean-slate rebuild that **deletes and recreates** the database.
  You almost never want it; `update` is the normal path.

See `DEV.md` for architecture, and for two automation dead ends documented with
their evidence so they do not get rebuilt.

## License

MIT - see `LICENSE`.
