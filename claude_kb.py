#!/usr/bin/env python3
"""Claude KB — local chat-history + project-doc search index.

Python standard library only (sqlite3 + json). Reads the Claude data export
(READ-ONLY) and builds an FTS index for searching all past chats and project docs.

Usage:
    python claude_kb.py build                     # full clean-slate rebuild
    python claude_kb.py update <export-dir|.zip>  # incremental upsert (never wipes)
    python claude_kb.py update-web <file.json...> # upsert web captures (never shrinks)
    python claude_kb.py search "TERMS"
    python claude_kb.py mcp                        # stdio MCP server
    python claude_kb.py http                       # localhost streamable-http MCP
"""
import sqlite3, json, os, sys, glob, gzip, shutil, hashlib, zipfile, tempfile

import kb_config

# UTF-8 throughout: the export is UTF-8 and chats contain emoji/box-drawing;
# force UTF-8 stdout so a cp1252 Windows console can't crash on encode.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Resolved from the environment / config.json / defaults - see kb_config.py.
# Deferred rather than resolved at import so a missing setting is reported as a
# one-line message by main(), not as an import traceback.
ROOT = EXPORT = CONV = PROJDIR = DCHATS = DB = None
CONFIG_ERROR = None


def _resolve_config():
    """Bind the module-level paths. Returns an error string, or None on success."""
    global ROOT, EXPORT, CONV, PROJDIR, DCHATS, DB, CONFIG_ERROR
    try:
        p = kb_config.paths()
    except kb_config.MissingSetting as e:
        CONFIG_ERROR = str(e)
        return CONFIG_ERROR
    ROOT = p["root"]
    DB = p["db"]
    EXPORT = kb_config.export_dir()
    # `build` needs an unpacked export; `update`, `search`, and the MCP servers
    # do not, so an absent one is not an error until build() actually asks.
    if EXPORT:
        CONV = os.path.join(EXPORT, "conversations.json")
        PROJDIR = os.path.join(EXPORT, "projects")
        DCHATS = os.path.join(EXPORT, "design_chats")
    CONFIG_ERROR = None
    return None


ROOT_PARENT = "00000000-0000-4000-8000-000000000000"


# ── FTS availability ─────────────────────────────────────────────────────────
def fts_flavor(conn):
    for flavor in ("fts5", "fts4"):
        try:
            conn.execute(f"CREATE VIRTUAL TABLE _probe USING {flavor}(x)")
            conn.execute("DROP TABLE _probe")
            return flavor
        except sqlite3.OperationalError:
            continue
    return None


# ── text flattening ──────────────────────────────────────────────────────────
def flatten_text(msg):
    """Prefer content-block text (type==text); fall back to the `text` field."""
    parts = []
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if t:
                    parts.append(t)
    if parts:
        return "\n".join(parts).strip()
    return (msg.get("text") or "").strip()


def conv_title(c):
    for k in ("name", "summary"):
        v = (c.get(k) or "").strip()
        if v:
            return v
    return "(untitled)"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── shared parsing + upsert helpers (used by build AND update) ───────────────
def _conv_messages(c):
    """Return [(text, role, created_at)] for indexable messages, in order.

    THE definition of what counts as an indexable message, and therefore of
    msg_count. Every writer must count through this - see _should_replace.

    Non-dict entries are skipped rather than crashing: this runs over data from
    outside the official export, where one malformed entry must not take down
    the batch it arrived in.
    """
    out = []
    for m in (c.get("chat_messages") or c.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("sender") or m.get("role") or ""
        if role not in ("human", "assistant"):
            continue
        text = flatten_text(m)
        if not text:
            continue
        out.append((text, role, m.get("created_at") or ""))
    return out


def _hash_texts(texts):
    """Stable content hash over an ordered list of message/doc texts."""
    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _iter_conversations(conv_json, dchats_dir):
    """Yield conversation dicts from conversations.json + design_chats/*.json."""
    for c in load_json(conv_json):
        yield c
    if dchats_dir and os.path.isdir(dchats_dir):
        for f in sorted(glob.glob(os.path.join(dchats_dir, "*.json"))):
            obj = load_json(f)
            if isinstance(obj, list):
                for c in obj:
                    yield c
            elif isinstance(obj, dict):
                yield obj


def _iter_projects(projects_path):
    """Yield (project_dict, label) from a projects DIR of *.json or a single projects.json.

    The legacy export shipped projects/ as a directory of one JSON per project.
    The multi-part export (projects-000.zip) ships a single projects.json holding
    a list. Accept either, plus a lone dict, so both layouts index identically.
    """
    if not projects_path:
        return
    if os.path.isdir(projects_path):
        for pf in sorted(glob.glob(os.path.join(projects_path, "*.json"))):
            pj = load_json(pf)
            label = os.path.basename(pf)
            if isinstance(pj, list):
                for p in pj:
                    if isinstance(p, dict):
                        yield p, label
            elif isinstance(pj, dict):
                yield pj, label
    elif os.path.isfile(projects_path):
        pj = load_json(projects_path)
        label = os.path.basename(projects_path)
        if isinstance(pj, list):
            for p in pj:
                if isinstance(p, dict):
                    yield p, label
        elif isinstance(pj, dict):
            yield pj, label


def _iter_project_docs(projdir):
    """Yield (project_uuid, project_name, filename, content, created_at)."""
    for pj, label in _iter_projects(projdir):
        puuid = pj.get("uuid") or label
        pname = (pj.get("name") or "").strip() or f"(untitled project {label[:8]})"
        for d in (pj.get("docs") or []):
            content = (d.get("content") or "").strip()
            if not content:
                continue
            yield puuid, pname, (d.get("filename") or "(doc)"), content, (d.get("created_at") or "")


def _resolve_export(path):
    """Resolve a dir OR .zip to (conv_json, projdir, dchats_dir, tempdir).

    tempdir is a TemporaryDirectory (clean up after) when a zip was extracted, else None.
    """
    tmp = None
    root = path
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        tmp = tempfile.TemporaryDirectory(prefix="kb_ingest_")
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp.name)
        root = tmp.name
    if not os.path.exists(root):
        raise FileNotFoundError(f"export path not found: {path}")
    conv = os.path.join(root, "conversations.json")
    if not os.path.isfile(conv):
        conv = None
        for dp, _dn, fn in os.walk(root):
            if "conversations.json" in fn:
                conv = os.path.join(dp, "conversations.json")
                break
    if not conv:
        if tmp:
            tmp.cleanup()
        raise FileNotFoundError(f"conversations.json not found under {path}")
    base = os.path.dirname(conv)
    return conv, _find_projects(base, root), os.path.join(base, "design_chats"), tmp


def _find_projects(base, root):
    """Locate the projects payload: projects/ dir or projects.json, near conv then anywhere.

    In a merged multi-part export the parts need not land in the same subdirectory,
    so fall back to a walk of the whole tree before giving up.
    """
    for d in (base, root):
        cand = os.path.join(d, "projects")
        if os.path.isdir(cand):
            return cand
        cand = os.path.join(d, "projects.json")
        if os.path.isfile(cand):
            return cand
    for dp, dn, fn in os.walk(root):
        if "projects" in dn:
            return os.path.join(dp, "projects")
        if "projects.json" in fn:
            return os.path.join(dp, "projects.json")
    return os.path.join(base, "projects")  # absent; _iter_projects yields nothing


def _ensure_schema(conn):
    """Create docs (FTS) + tracking tables if absent. Returns the FTS flavor used."""
    have_docs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='docs'").fetchone()
    flavor = "fts5"
    if not have_docs:
        flavor = fts_flavor(conn)
        if flavor is None:
            print("ERROR: neither FTS5 nor FTS4 available in this sqlite3 build.")
            sys.exit(1)
        conn.execute(f"""
            CREATE VIRTUAL TABLE docs USING {flavor}(
                content, source UNINDEXED, project_name UNINDEXED, title UNINDEXED,
                conversation_uuid UNINDEXED, created_at UNINDEXED, role UNINDEXED)""")
    # tracking tables — how we know what's already indexed and how fresh
    conn.execute("""CREATE TABLE IF NOT EXISTS indexed_convs(
        conversation_uuid TEXT PRIMARY KEY, updated_at TEXT,
        msg_count INTEGER, content_hash TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS indexed_projdocs(
        project_uuid TEXT, filename TEXT, content_hash TEXT,
        PRIMARY KEY(project_uuid, filename))""")
    return flavor


def _insert_conv(conn, c):
    """Insert a conversation's docs rows + tracking. Returns msg_count (0 if none)."""
    msgs = _conv_messages(c)
    if not msgs:
        return 0
    uuid = c.get("uuid") or ""
    title = conv_title(c)  # chats are NOT linked to a project in the export
    conn.executemany(
        "INSERT INTO docs(content,source,project_name,title,conversation_uuid,created_at,role)"
        " VALUES (?,?,?,?,?,?,?)",
        [(text, "chat", "(no project)", title, uuid, created, role)
         for (text, role, created) in msgs])
    conn.execute(
        "INSERT OR REPLACE INTO indexed_convs(conversation_uuid,updated_at,msg_count,content_hash)"
        " VALUES (?,?,?,?)",
        (uuid, c.get("updated_at") or "", len(msgs), _hash_texts([t for t, _, _ in msgs])))
    return len(msgs)


def _should_replace(stored, incoming, authoritative):
    """Decide what to do with a conversation that is already indexed.

    Returns "replace", "skip", or "partial".

    `stored` and `incoming` are both (updated_at, content_hash, msg_count).

    The official export is authoritative: it is a complete snapshot, so it may
    legitimately shrink a conversation (you deleted messages) and is always
    allowed to replace.

    Any other writer - a live browser capture, say - is not. A capture of an
    ongoing chat holds FEWER messages than a later export of the same chat, and
    the plain hash comparison cannot tell "this is newer" from "this is a
    partial view": both simply hash differently. Replacing on hash alone would
    delete the fuller version and reinstate the shorter one, silently, while
    reporting success. So a non-authoritative writer may only replace when it
    brings at least as many messages as are already indexed.

    msg_count must be computed by the SAME normalizer on both sides
    (_conv_messages), not from a raw array length. The export drops messages
    that are neither human nor assistant, and messages whose content flattens
    to nothing; a count that skips that filter is not comparable to a stored
    one, and the guard degrades into a coin flip.
    """
    st_upd, st_hash, st_count = stored
    in_upd, in_hash, in_count = incoming

    unchanged = in_hash == st_hash and not (in_upd and st_upd and in_upd > st_upd)
    if unchanged:
        return "skip"
    if authoritative:
        return "replace"
    if in_count is None or st_count is None:
        return "partial"          # cannot prove it is not a shrink; refuse
    return "replace" if in_count >= st_count else "partial"


def _insert_projdoc(conn, puuid, pname, filename, content, created):
    """Insert a project doc's row + tracking."""
    conn.execute(
        "INSERT INTO docs(content,source,project_name,title,conversation_uuid,created_at,role)"
        " VALUES (?,?,?,?,?,?,?)",
        (content, "project_doc", pname, filename, puuid, created, ""))
    conn.execute(
        "INSERT OR REPLACE INTO indexed_projdocs(project_uuid,filename,content_hash) VALUES (?,?,?)",
        (puuid, filename, _hash_texts([content])))


def _backfill_tracking(conn):
    """Migration: if docs has rows but tracking is empty (pre-tracking DB),
    reconstruct tracking from existing docs so `update` won't duplicate them."""
    if not conn.execute("SELECT 1 FROM docs LIMIT 1").fetchone():
        return
    if conn.execute("SELECT count(*) FROM indexed_convs").fetchone()[0] == 0:
        agg = {}
        order = []
        for uuid, content in conn.execute(
                "SELECT conversation_uuid, content FROM docs WHERE source='chat' ORDER BY rowid"):
            if uuid not in agg:
                agg[uuid] = []
                order.append(uuid)
            agg[uuid].append(content)
        for uuid in order:
            texts = agg[uuid]
            conn.execute(
                "INSERT OR REPLACE INTO indexed_convs(conversation_uuid,updated_at,msg_count,content_hash)"
                " VALUES (?,?,?,?)", (uuid, "", len(texts), _hash_texts(texts)))
    if conn.execute("SELECT count(*) FROM indexed_projdocs").fetchone()[0] == 0:
        for puuid, filename, content in conn.execute(
                "SELECT conversation_uuid, title, content FROM docs WHERE source='project_doc'"):
            conn.execute(
                "INSERT OR REPLACE INTO indexed_projdocs(project_uuid,filename,content_hash)"
                " VALUES (?,?,?)", (puuid, filename, _hash_texts([content])))


# ── build (full, clean-slate rebuild) ────────────────────────────────────────
def build():
    if not EXPORT:
        print("ERROR: no unpacked export found.\n"
              "  Set CLAUDE_KB_EXPORT_DIR (or \"export_dir\" in config.json) to the\n"
              "  unpacked export directory, or place one as <root>/data-*/.\n"
              "  To ingest export zips instead, use: claude_kb.py update <zip|dir>")
        sys.exit(2)
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    flavor = _ensure_schema(conn)
    print("FTS5: available" if flavor == "fts5"
          else f"NOTE: FTS5 unavailable — using {flavor.upper()}.")

    n_docs = n_convs = n_msgs = 0
    for puuid, pname, filename, content, created in _iter_project_docs(PROJDIR):
        _insert_projdoc(conn, puuid, pname, filename, content, created)
        n_docs += 1
    for c in _iter_conversations(CONV, DCHATS):
        added = _insert_conv(conn, c)
        if added:
            n_convs += 1
            n_msgs += added

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    total = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    conn.close()
    gz = write_gz()
    size = os.path.getsize(DB)
    n_projects = len(glob.glob(os.path.join(PROJDIR, "*.json")))
    print("\n=== BUILD COMPLETE ===")
    print(f"  projects scanned : {n_projects}")
    print(f"  conversations    : {n_convs}")
    print(f"  chat messages    : {n_msgs}")
    print(f"  project docs     : {n_docs}")
    print(f"  total rows        : {total}")
    print(f"  DB                : {DB}")
    print(f"  DB size          : {size:,} bytes ({size/1024/1024:.2f} MB)")
    print(f"  DB.gz            : {gz} ({os.path.getsize(gz):,} bytes, "
          f"{os.path.getsize(gz)/1024/1024:.2f} MB)")


# ── update (incremental upsert; never wipes) ─────────────────────────────────
def upsert_conversations(conn, conversations, authoritative):
    """Upsert conversations into an open connection.

    `authoritative` says whether this writer is allowed to shrink a
    conversation - see _should_replace. Returns (new, updated, skipped,
    partial).
    """
    tally = {"new": 0, "updated": 0, "skipped": 0, "partial": 0}
    for c in conversations:
        status = _upsert_one(conn, c, authoritative)
        if status:
            tally[status] += 1
    return tally["new"], tally["updated"], tally["skipped"], tally["partial"]


def _upsert_one(conn, c, authoritative):
    """Upsert one conversation. Returns "new"/"updated"/"skipped"/"partial", or
    None when there is nothing indexable.

    Per-conversation rather than per-batch so callers can report what happened
    to each INPUT. A file that validated is not the same as a file that reached
    the index, and reporting the first as the second is how a held-back capture
    comes to look like a successful one.
    """
    msgs = _conv_messages(c)
    if not msgs:
        return None
    uuid = c.get("uuid") or ""
    incoming = (c.get("updated_at") or "",
                _hash_texts([t for t, _, _ in msgs]),
                len(msgs))
    row = conn.execute(
        "SELECT updated_at, content_hash, msg_count FROM indexed_convs"
        " WHERE conversation_uuid=?", (uuid,)).fetchone()
    if row is None:
        _insert_conv(conn, c)
        return "new"
    action = _should_replace(row, incoming, authoritative)
    if action == "replace":
        conn.execute(
            "DELETE FROM docs WHERE conversation_uuid=? AND source='chat'", (uuid,))
        _insert_conv(conn, c)
        return "updated"
    if action == "partial":
        # Fewer messages than are already indexed, from a writer that is not a
        # complete snapshot. Leave the fuller version alone.
        print(f"  PARTIAL {uuid}: {incoming[2]} messages vs {row[2]} indexed"
              f" - keeping the indexed version")
        return "partial"
    return "skipped"


def update(export_path, authoritative=True):
    conv_json, projdir, dchats, tmp = _resolve_export(export_path)
    try:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        _backfill_tracking(conn)  # safe no-op once tracking exists

        c_new, c_upd, c_skip, c_part = upsert_conversations(
            conn, _iter_conversations(conv_json, dchats), authoritative)

        d_new = d_upd = d_skip = 0
        for puuid, pname, filename, content, created in _iter_project_docs(projdir):
            ex_hash = _hash_texts([content])
            row = conn.execute(
                "SELECT content_hash FROM indexed_projdocs WHERE project_uuid=? AND filename=?",
                (puuid, filename)).fetchone()
            if row is None:
                _insert_projdoc(conn, puuid, pname, filename, content, created)
                d_new += 1
            elif row[0] != ex_hash:
                conn.execute(
                    "DELETE FROM docs WHERE conversation_uuid=? AND title=? AND source='project_doc'",
                    (puuid, filename))
                _insert_projdoc(conn, puuid, pname, filename, content, created)
                d_upd += 1
            else:
                d_skip += 1

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        total = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
        conn.close()
        gz = write_gz()
        size = os.path.getsize(DB)
        print("\n=== UPDATE COMPLETE (upsert; nothing wiped) ===")
        print(f"  source           : {export_path}")
        print(f"  chats   NEW={c_new}  UPDATED={c_upd}  SKIPPED={c_skip}"
              f"  PARTIAL={c_part}")
        print(f"  docs    NEW={d_new}  UPDATED={d_upd}  SKIPPED={d_skip}")
        print(f"  total rows        : {total}")
        print(f"  DB size          : {size:,} bytes ({size/1024/1024:.2f} MB)")
        print(f"  DB.gz            : {os.path.getsize(gz):,} bytes")
        # machine-readable last line for the ingest runner. The SUMMARY prefix
        # is what kb_ingest.py matches on; fields may be appended safely.
        print(f"SUMMARY NEW={c_new + d_new} UPDATED={c_upd + d_upd} "
              f"SKIPPED={c_skip + d_skip} PARTIAL={c_part} ROWS={total}")
    finally:
        if tmp:
            tmp.cleanup()


WEB_FORMAT = "claude-kb-web-export"
WEB_FORMAT_VERSION = 1


def _reject(path, reason):
    """Report a refused capture in one parseable line, and return None.

    The caller logs this reason. A reason that is computed and then dropped
    before it reaches ingest.log leaves a 06:00 rejection undiagnosable after
    the fact, which is the silent-stall shape this codebase keeps hitting.
    """
    print(f"REJECTED {os.path.basename(path)}: {reason}")
    return None


def _read_web_export(path):
    """Parse one claude-kb-web-export envelope. Returns a conversation dict or None.

    The envelope wraps the conversation object verbatim, because the shape the
    web app returns is a SUPERSET of the shape the official export ships: same
    uuid/name/updated_at, same chat_messages carrying sender/text/content/
    created_at/parent_message_uuid. So the wrapped object feeds straight into
    _conv_messages with no translation, and - critically - it is counted by the
    same normalizer the export is counted by, which is what makes the shrink
    guard's comparison meaningful.

    Rejects rather than half-ingests. A capture that cannot be trusted is worth
    less than no capture: it would land as a PARTIAL at best, and at worst as a
    plausible-looking replacement.

    Never raises. Anything unexpected becomes a REJECTED line, because a capture
    that crashes the reader would abort the whole batch it arrived in - taking
    the good captures beside it down too - and kb_ingest would then leave every
    file in incoming/ to be retried, forever, at 06:00.
    """
    try:
        return _read_web_export_inner(path)
    except Exception as e:                       # noqa: BLE001 - deliberate
        return _reject(path, f"unreadable ({type(e).__name__}: {e})")


def _read_web_export_inner(path):
    try:
        obj = load_json(path)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return _reject(path, f"unreadable ({e})")
    if not isinstance(obj, dict):
        return _reject(path, "not a JSON object")

    fmt = (obj.get("format") or "").strip()
    if fmt != WEB_FORMAT:
        return _reject(path, f"unknown format {fmt!r}")
    try:
        ver = int(obj.get("format_version") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver != WEB_FORMAT_VERSION:
        # Refuse a future envelope rather than mis-parse it.
        return _reject(path, f"format_version {obj.get('format_version')!r}"
                             f" (this build reads {WEB_FORMAT_VERSION})")

    conv = obj.get("conversation")
    if not isinstance(conv, dict):
        return _reject(path, "no conversation object")
    conv_uuid = (conv.get("uuid") or "").strip()
    if not conv_uuid:
        return _reject(path, "no conversation.uuid")

    # The envelope carries the uuid the capture was TAKEN FOR - the one in the
    # page URL - and the conversation object carries the uuid the API actually
    # answered with. They must agree. A mismatch means the capture is filed
    # under the wrong identity, and since that identity is the primary key the
    # official export also uses, ingesting it would corrupt a real conversation
    # with another one's messages. There is no safe way to guess which of the
    # two is right, so refuse.
    env_uuid = (obj.get("conversation_uuid") or "").strip()
    if not env_uuid:
        return _reject(path, "no envelope conversation_uuid")
    if env_uuid != conv_uuid:
        return _reject(path, f"uuid mismatch: envelope {env_uuid}"
                             f" != conversation {conv_uuid}")

    # current_leaf_message_uuid may be present. It is METADATA ONLY and is
    # deliberately not read here: pruning a capture to the active path would
    # make every forked conversation capture short, and 15% of them are forked,
    # so each would be rejected as PARTIAL forever. Capture the whole tree.

    msgs = conv.get("chat_messages")
    if not isinstance(msgs, list) or not msgs:
        return _reject(path, "no chat_messages")

    # A truncated message means the source returned incomplete text: the count
    # matches but the content does not, so it would churn UPDATED forever while
    # degrading what is indexed. Refuse the whole capture.
    n_trunc = sum(1 for m in msgs if isinstance(m, dict) and m.get("truncated"))
    if n_trunc:
        return _reject(path, f"{n_trunc} truncated message(s)")

    if not _conv_messages(conv):
        return _reject(path, "no indexable messages")
    return conv


def update_web(paths):
    """Upsert web captures. NEVER authoritative - see _should_replace.

    Returns (ok, ingested_paths). A file that fails validation is not ingested
    and not reported as ingested, so the caller leaves it where it is.
    """
    conns = []
    good = []
    seen = {}
    for p in paths:
        conv = _read_web_export(p)
        if conv is None:
            continue
        uuid = conv.get("uuid")
        if uuid in seen:
            # Two captures of one conversation in a single run. Upserting both
            # would have the second compared against the first's freshly stored
            # count, which is a comparison against this run rather than against
            # the index. Keep the longer and say so.
            other, other_n = seen[uuid]
            this_n = len(_conv_messages(conv))
            if this_n <= other_n:
                _reject(p, f"duplicate of {os.path.basename(other)} in this run"
                           f" ({this_n} messages vs {other_n}) - keeping the longer")
                continue
            _reject(other, f"superseded by {os.path.basename(p)} in this run"
                           f" ({other_n} messages vs {this_n})")
            idx = good.index(other)
            good.pop(idx)
            conns.pop(idx)
        conns.append(conv)
        good.append(p)
        seen[uuid] = (p, len(_conv_messages(conv)))

    # Per-file disposition, so a caller can say what became of each capture
    # rather than only how the batch went in aggregate.
    outcomes = []
    conn = sqlite3.connect(DB)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        _backfill_tracking(conn)
        for path, conv in zip(good, conns):
            outcomes.append((path, _upsert_one(conn, conv, authoritative=False)))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        total = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    finally:
        conn.close()
    write_gz()

    tally = {"new": 0, "updated": 0, "skipped": 0, "partial": 0}
    for _p, st in outcomes:
        if st:
            tally[st] += 1
    rejected = len(paths) - len(good)

    print("\n=== WEB CAPTURE UPDATE (upsert; nothing wiped) ===")
    print(f"  files            : {len(paths)} ({rejected} rejected)")
    print(f"  chats   NEW={tally['new']}  UPDATED={tally['updated']}"
          f"  SKIPPED={tally['skipped']}  PARTIAL={tally['partial']}")
    print(f"  total rows        : {total}")
    print(f"SUMMARY NEW={tally['new']} UPDATED={tally['updated']} "
          f"SKIPPED={tally['skipped']} PARTIAL={tally['partial']} "
          f"REJECTED={rejected} ROWS={total}")
    return True, outcomes


def write_gz():
    """Write claude_kb.db.gz from the current DB (read-only source)."""
    dst = DB + ".gz"
    with open(DB, "rb") as f_in, gzip.open(dst, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return dst


# ── search ───────────────────────────────────────────────────────────────────
def make_match(terms):
    """Quote each whitespace token so punctuation can't break FTS syntax; AND them."""
    toks = [t for t in terms.split() if t]
    if not toks:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def search(terms, limit=15):
    if not os.path.exists(DB):
        print("No index yet. Run:  python claude_kb.py build")
        return
    conn = sqlite3.connect(DB)
    match = make_match(terms)
    if match is None:
        print("Empty query.")
        return
    try:
        has_snip = True
        cur = conn.execute(f"""
            SELECT project_name, title, created_at, role, source,
                   snippet(docs, 0, '»', '«', ' … ', 14) AS snip
            FROM docs
            WHERE docs MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (match, limit))
        rowset = cur.fetchall()
    except sqlite3.OperationalError:
        # FTS4 has no rank/snippet with same signature — degrade gracefully
        has_snip = False
        cur = conn.execute(
            "SELECT project_name,title,created_at,role,source,substr(content,1,200) "
            "FROM docs WHERE docs MATCH ? LIMIT ?", (match, limit))
        rowset = cur.fetchall()
    conn.close()

    if not rowset:
        print(f'No hits for: {terms}')
        return
    print(f'Top {len(rowset)} hits for: {terms}\n' + "=" * 70)
    for i, (pname, title, created, role, source, snip) in enumerate(rowset, 1):
        date = (created or "")[:10]
        snip = " ".join((snip or "").split())
        print(f"\n{i:2}. [{pname}] · {title} · {date} · {role or '-'} · {source}")
        print(f"    {snip}")


# ── mcp server (STDIO) ───────────────────────────────────────────────────────
def _ro_conn():
    """Open the index READ-ONLY (queries only; cannot modify the DB)."""
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def _make_server(host="127.0.0.1", port=8760, http=False):
    """Build the 'claude-kb' FastMCP server with both tools registered.

    Shared by the stdio (`mcp`) and streamable-http (`http`) modes.
    NOTE: never print to stdout — in stdio mode it carries the MCP protocol.
    """
    # This project targets the mcp 1.x API. mcp 2.x renamed/removed
    # mcp.server.fastmcp.FastMCP, and the resulting ImportError reads like a bug
    # in this project rather than what it is - a version mismatch - so name it.
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        try:
            import importlib.metadata as _md
            installed = _md.version("mcp")
        except Exception:
            installed = "unknown"
        raise ImportError(
            "could not import FastMCP from the 'mcp' package (installed: %s).\n"
            "  This project needs mcp 1.x: 2.x renamed that API.\n"
            "  Fix with:  pip install \"mcp<2\"   (or: pip install -r requirements.txt)\n"
            "  Original error: %s" % (installed, e)) from e

    if http:
        # stateless + JSON responses: each HTTP request is self-contained
        # (curl-friendly) and localhost-only, no session/auth needed.
        server = FastMCP("claude-kb", host=host, port=port,
                         streamable_http_path="/mcp",
                         stateless_http=True, json_response=True)
    else:
        server = FastMCP("claude-kb")

    @server.tool()
    def kb_search(query: str, limit: int = 15) -> list:
        """Full-text search across all of your past Claude chats + project docs.

        Returns a ranked list of hits, each with title, date, sender, source,
        conversation_uuid, and a highlighted snippet. Use kb_get_conversation with
        a returned conversation_uuid to read the full chat.
        """
        match = make_match(query)
        if match is None:
            return []
        conn = _ro_conn()
        try:
            cur = conn.execute(
                "SELECT title, created_at, role, source, conversation_uuid, "
                "snippet(docs, 0, '»', '«', ' … ', 14) "
                "FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
                (match, max(1, int(limit))))
            return [{
                "title": t,
                "date": (c or "")[:10],
                "sender": r,
                "source": s,
                "conversation_uuid": u,
                "snippet": " ".join((sn or "").split()),
            } for (t, c, r, s, u, sn) in cur.fetchall()]
        finally:
            conn.close()

    @server.tool()
    def kb_get_conversation(conversation_uuid: str, max_messages: int = 200) -> list:
        """Return all indexed messages for a conversation_uuid, in order.

        Each item is {sender, text}. Use after kb_search to read the FULL chat.
        """
        conn = _ro_conn()
        try:
            cur = conn.execute(
                "SELECT role, content FROM docs "
                "WHERE conversation_uuid = ? AND source = 'chat' "
                "ORDER BY rowid LIMIT ?",
                (conversation_uuid, max(1, int(max_messages))))
            return [{"sender": r, "text": t} for (r, t) in cur.fetchall()]
        finally:
            conn.close()

    return server


def run_mcp():
    """STDIO MCP server 'claude-kb' (for Claude Desktop's config-based MCP)."""
    _make_server(http=False).run()  # stdio transport by default


def run_http():
    """Streamable-HTTP MCP server on 127.0.0.1:<port> at /mcp (localhost only)."""
    port = kb_config.get("http_port")  # default 8760 (free in 8760-8799)
    _make_server(host="127.0.0.1", port=port, http=True).run(
        transport="streamable-http")


# ── cli ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    err = _resolve_config()
    if err:
        print("ERROR: %s" % err)
        sys.exit(2)
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "update":
        update(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "update-web":
        files = sys.argv[2:]
        if not files:
            print("usage: claude_kb.py update-web <capture.json> [...]")
            sys.exit(2)
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            for f in missing:
                print(f"ERROR: no such file: {f}")
            sys.exit(2)
        ok, outcomes = update_web(files)
        # One line per accepted file, naming what actually became of it.
        # INGESTED means it reached the index. PARTIAL and SKIPPED did not, and
        # must never be reported as if they had - a caller that conflates them
        # tells the user a held-back capture succeeded. All three are accepted
        # and archivable; only REJECTED (printed by the reader) is not.
        for p, st in outcomes:
            if st in ("new", "updated"):
                print(f"INGESTED {p}")
            elif st == "partial":
                print(f"PARTIAL {p}")
            elif st == "skipped":
                print(f"SKIPPED {p}")
        sys.exit(0 if ok else 1)
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        search(" ".join(sys.argv[2:]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "mcp":
        run_mcp()
    elif len(sys.argv) >= 2 and sys.argv[1] == "http":
        run_http()
    else:
        print(__doc__)
        sys.exit(1)
