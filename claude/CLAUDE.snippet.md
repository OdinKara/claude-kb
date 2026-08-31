# Claude KB routing snippet

Append the block below to your own `~/.claude/CLAUDE.md` (Windows:
`C:\Users\<you>\.claude\CLAUDE.md`). It is what makes "update the KB" work as a
plain sentence instead of requiring the `/kb-update` slash command.

Replace `<PATH-TO-KB-UPDATE-MD>` with wherever you installed `kb-update.md` --
normally `~/.claude/commands/kb-update.md` -- and `<PATH-TO-CLAUDE-KB>` with your
clone of this repo. Nothing else needs editing.

Why the guardrail is in the snippet and not only in the command file: it has to
be visible BEFORE Code opens the command file. By the time a session is
improvising a downloader, it has already decided not to read the instructions.

---

```markdown
## Claude KB

When I say "KB update", "KB ingest", "update the KB", "ingest the KB", or
anything clearly meaning the same, read <PATH-TO-KB-UPDATE-MD> and follow it
exactly. That file is the source of truth for this workflow - do not act on a
summary of it, and do not rely on anything remembered about it from a previous
session.

Run from wherever the session already is. Do not cd first, do not ask which
directory, do not ask me to confirm the path.

Never write a new fetcher that downloads Claude export URLs over HTTP. That has
been tried and cannot work; <PATH-TO-CLAUDE-KB>/DEV.md has the evidence.
```

---

## What each part is doing

**The trigger list.** Several phrasings map to one workflow so you do not have to
remember the exact wording. Add your own; the point is that they all route to the
same file.

**"Read the command file and follow it exactly."** Without this, a session will
happily act on a half-remembered version of the workflow from earlier in the
conversation, or from its own summary. The exit-code handling and the
single-retry rule around `--reset-canary` only work if they are read fresh.

**"Run from wherever the session already is."** The scripts resolve every path
through `kb_config.py`, so the working directory is irrelevant. Without this
line, sessions tend to ask which directory to use, or cd somewhere first, which
wastes a turn and occasionally lands somewhere wrong.

**The HTTP-fetcher prohibition.** This is the expensive one. Claude export URLs
are single-use and only serve a zip to a real browser; an HTTP client gets the
app shell back even with a valid session cookie. A model that does not know this
will reasonably try `curl`, `requests`, or a headless browser, and each attempt
burns a token that cannot be recovered. Two such fetchers were built and deleted
before this line existed.
