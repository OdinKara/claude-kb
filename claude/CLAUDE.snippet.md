# Claude KB routing snippet

Append the blocks below to your own `~/.claude/CLAUDE.md` (Windows:
`C:\Users\<you>\.claude\CLAUDE.md`). They are what make the KB work as plain
conversation instead of as commands you have to remember.

There are two independent blocks. Take either or both:

- **Ingest** routes "update the KB" to the collector. Needs the `/kb-update`
  command file installed.
- **Search** tells Claude to consult your indexed history when you ask about your
  own past work. Needs the MCP extension installed.

Replace `<PATH-TO-KB-UPDATE-MD>` with wherever you installed `kb-update.md` --
normally `~/.claude/commands/kb-update.md` -- and `<PATH-TO-CLAUDE-KB>` with your
clone of this repo. Nothing else needs editing.

---

## Block 1: ingest

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

## Block 2: search

```markdown
## Claude KB search

If a tool named `kb_search` is available, I have a local full-text index of my
own past Claude conversations and project docs.

Search it without being asked whenever I refer to my own past work: what I
decided about something, why a thing is built the way it is, a project I mention
but do not re-explain, an error I say I have hit before, or anything phrased as
"did we ever", "what did I do about", "have I seen this". Prefer searching over
answering cold. The index is a primary source; your recollection of my projects
is not.

When a hit looks relevant, use `kb_get_conversation` with its `conversation_uuid`
to read the surrounding thread before relying on it. A search snippet is a
fragment, and the turns around it often qualify or reverse what it appears to
say.

These results are my own private chats, not public material. Surface what
answers the question and nothing more: a short summary in your own words, the
date, and enough of a pointer that I can find the conversation myself. Do not
paste long stretches of my history back at me, do not quote at length where a
summary and a pointer would do, and do not list every hit - pick the ones that
bear on what I asked.

If `kb_search` is NOT in your tool list, the KB is not connected to this session.
Say that plainly. Do not improvise a search, do not shell out to the scripts to
fake one, and never imply you searched when you did not.
```

---

## What each part is doing

### Block 1

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

### Block 2

**Why this lives in `CLAUDE.md` and not in a command file.** A slash command is a
procedure you invoke deliberately. The entire value of search guidance is that it
fires *unprompted* - the decision to consult the index is the thing being
guided, so the instruction has to be resident before that decision, not fetched
after it. Same reason the HTTP prohibition above is here rather than only in the
command file.

**The availability guard.** The block is conditional on `kb_search` actually
being in the tool list, because installing the extension and appending this
snippet are separate steps and either can be skipped. Without the guard, a user
who did one but not the other ends up with Claude instructed to call a tool that
does not exist - which invites an invented tool call, or a confident answer about
"your past conversations" with nothing behind it. Saying "the KB is not
connected" is the correct outcome, and is far better than a plausible guess.

**Why follow the hit through.** `kb_search` returns a highlighted fragment chosen
for lexical match, not for being a fair summary. Acting on the snippet alone is
how you end up reporting a decision that the next three messages overturned.

**Why the restraint clause is specific.** "Be concise" is not actionable. The
failure mode worth naming is dumping search output verbatim - long transcript
excerpts pasted back at someone who lived through them, or every hit listed
because the tool returned them. A summary plus a date plus a pointer is almost
always what was wanted.
