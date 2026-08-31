---
description: Download the pending Claude export parts via a browser and ingest them into the KB
---

Run the Claude KB export collector non-interactively:

```
<PYTHON> "<CLAUDE_KB_SCRIPTS>/kb_open.py" --yes
```

Replace `<PYTHON>` with the full path to your interpreter and
`<CLAUDE_KB_SCRIPTS>` with the directory holding `kb_open.py`, then delete this
paragraph. Pin the interpreter by absolute path rather than using bare `python`:
it resolves differently depending on how the session was launched, and a wrong
or missing Python produces an exit code that looks like a KB failure rather than
an environment one. If Python is ever relocated, update this file.

Everything else is resolved by `kb_config.py` (environment variable, then
`config.json`, then a default), so no other path is written down here. To see
where a run will actually read and write:

```
<PYTHON> -c "import sys; sys.path.insert(0, r'<CLAUDE_KB_SCRIPTS>'); import kb_config; print(kb_config.paths())"
```

Run it from wherever the session already is. Do not cd first, do not ask which
directory, do not ask the user to confirm the path.

## What this does

It opens the export URLs from the waiting manifest in a real browser, waits for
each zip to land in the configured downloads directory, moves them into
`<root>/incoming/`, then triggers the ingest task and waits for it to log a
result.

Browser tabs will open while it runs. That is expected and unavoidable - these
URLs only serve a zip to a real browser. Do not treat it as a malfunction.

Then report, in this order:

1. The exit code and what it means (table below).
2. The per-file result lines - each `ok: <file> (N members)` or
   `FAILED <file>: ...` line from the output.
3. The final `<root>/ingest.log` line the script printed, which carries the
   `SUMMARY NEW=.. UPDATED=.. SKIPPED=.. ROWS=..` counts.

## Exit codes

| code | meaning | what to tell the user |
|------|---------|-----------------------|
| 0 | parts downloaded and ingest confirmed in `<root>/ingest.log` | Done. Quote the SUMMARY line and the new ROWS total. |
| 1 | nothing to do, or ingest not confirmed | Say which. "Nothing new" means the parts were already in `<root>/incoming/`. "no new ingest.log line after 120s" means the task fired but did not log - check `<root>/ingest.log` manually. |
| 2 | environment problem | Either no browser was found, there is no `manifest-*.json` in `<root>/incoming/`, or a required setting is missing. The script names which. The manifest case is the common one: no new export manifest has been dropped yet. |
| 3 | canary failed - `light_metadata` did not download | The cause is genuinely ambiguous: EITHER the browser is not logged into claude.ai, OR that URL was already spent. These are indistinguishable from outside - report both, do not assert one. No valuable URLs were touched. See the recovery path below. |

## Recovering from exit 3

The canary result is remembered per manifest in `.canary-state.json`, so a plain
re-run will NOT retry - it exits 3 again without opening anything. That is
deliberate: re-running must not spend the token again just to re-learn the same
thing.

Tell the user to confirm the browser is logged into claude.ai, then retry with
the reset:

```
<PYTHON> "<CLAUDE_KB_SCRIPTS>/kb_open.py" --reset-canary --yes
```

That clears the remembered failure and runs a fresh canary in the same pass. Do
NOT run it repeatedly - each fresh canary spends a real (if worthless) token, and
if the URLs are actually spent no reset will help. After one failed retry, the
answer is a fresh export, not another reset.

To clear the state without running anything, use `--reset-canary` alone; it
opens nothing and exits 0.

## Preconditions - this command does NOT create exports

The user must have already:

1. Requested a data export in claude.ai settings.
2. Dropped the resulting `manifest-*.json` into `<root>/incoming/`.

If `<root>/incoming/` has no manifest the script exits 2. Do not try to create an
export, and do not try to fetch the URLs with curl, WebFetch, or any HTTP
client - those URLs only serve a zip to a real browser. See `DEV.md` in the
claude-kb repo.

## Notes

- Every `export_url` is SINGLE-USE. Do not re-run the command hoping a failed
  part will succeed on a second try; a spent URL needs a fresh export.
- NEVER write a new fetcher that downloads these URLs over HTTP - not with curl,
  WebFetch, requests, urllib, Playwright, or a headless browser. Both a
  cookie-authenticated HTTP client and a headless browser were built, tested, and
  deleted. The URLs return the app shell to an HTTP client even with a valid
  session, and every failed attempt burns a one-time token. The evidence is in
  `DEV.md`. `kb_open.py` is the only supported path.
- `--yes` deliberately spends the `light_metadata` URL as a login canary. That
  part is never indexed, so it is the only token in the manifest worth nothing.
- The script can take several minutes: it waits up to 300s per part for the
  download to land, then up to 120s for the ingest to log.
