#!/usr/bin/env python3
"""Native messaging host for the Claude KB capture extension.

Speaks Chrome/Edge native messaging (uint32 LE length prefix + UTF-8 JSON) on
stdin/stdout. A browser extension cannot write to an arbitrary folder or start a
process; this host does both on its behalf, which is exactly why its write guard
is not negotiable - see safe_name.

Every path comes from kb_config (environment variable, then config.json, then a
default), so nothing here is machine-specific. The one thing the host must be
told is where the KB scripts live, because the browser launches it from its own
directory: host.cmd sets CLAUDE_KB_SCRIPTS, and the installer writes host.cmd.

Message types:
    ping             is the host alive, and where would it write
    indexed          READ-ONLY: which conversations are already indexed
    save             write captures into incoming/
    ingest           ingest what is in incoming/ and report per-file outcomes
    save_and_ingest  both, reporting the ingest outcome for what was written

NEVER exits without answering. A host that dies quietly gives the extension a
bare "disconnected", which is indistinguishable from not being installed - so a
configuration problem would look like a missing host and send someone chasing
the registry instead of their config.json.
"""
import json
import os
import struct
import subprocess
import sys

MAX_MESSAGE = 50_000_000          # a long conversation is large; a GB is a bug
ALLOWED_EXTENSIONS = (".json", ".md")
REQUIRED_PREFIX = "claude-web-"

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- bootstrap
# The browser launches this from its own directory, so kb_config is not
# importable until we say where it lives. Failure is recorded, not raised: the
# protocol loop below still has to answer.
BOOT_ERROR = None
kb_config = None


def _scripts_dir():
    d = os.environ.get("CLAUDE_KB_SCRIPTS")
    if d:
        return d
    # Fall back to the repo layout (native/ sits beside the modules).
    return os.path.dirname(HERE)


def _bootstrap():
    global kb_config, BOOT_ERROR
    d = _scripts_dir()
    if not os.path.isfile(os.path.join(d, "kb_config.py")):
        BOOT_ERROR = ("kb_config.py not found in %r. Set CLAUDE_KB_SCRIPTS to the "
                      "directory holding claude_kb.py, or re-run install-host.ps1."
                      % d)
        return
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import kb_config as _kc
        kb_config = _kc
    except Exception as e:                        # noqa: BLE001
        BOOT_ERROR = "could not import kb_config from %r (%s: %s)" % (
            d, type(e).__name__, e)


_bootstrap()


# ---------------------------------------------------------------- protocol
def read_msg():
    raw = sys.stdin.buffer.read(4)
    if not raw or len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    if n <= 0 or n > MAX_MESSAGE:
        return None
    body = sys.stdin.buffer.read(n)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"type": "__malformed__"}


def write_msg(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------- write guard
def safe_name(name):
    """The only filename a caller may write. Returns a basename, or None.

    Deliberately as strict as it looks. This host runs with the user's
    privileges and writes where it is told, so a compromised or simply buggy
    extension is the threat model. Three independent conditions, all required:

        basename only        no directory component survives, and ".." is
                             stripped, so nothing escapes incoming/
        extension allowlist  .json or .md, nothing executable or loadable
        name prefix          claude-web-, a HARD requirement

    The prefix is not cosmetic. It is what keeps this host from being a general
    file-writing primitive: even granted the other two, a caller cannot land a
    file that the ingest path does not already expect to find. Do not relax it
    for convenience.
    """
    base = os.path.basename(name or "").replace("..", "")
    if not base:
        return None
    if not base.lower().endswith(ALLOWED_EXTENSIONS):
        return None
    if not base.startswith(REQUIRED_PREFIX):
        return None
    return base


# ---------------------------------------------------------------- operations
def _paths():
    p = kb_config.paths()
    return p["root"], p["incoming"], p["processed"]


def handle_save(msg):
    _root, incoming, _processed = _paths()
    os.makedirs(incoming, exist_ok=True)
    written, refused = [], []
    for item in (msg.get("files") or []):
        raw_name = (item or {}).get("name")
        name = safe_name(raw_name)
        content = (item or {}).get("content")
        if not name:
            refused.append({"name": str(raw_name)[:120],
                            "reason": "rejected by write guard (must be a bare "
                                      "%s*%s filename)"
                                      % (REQUIRED_PREFIX, "|".join(ALLOWED_EXTENSIONS))})
            continue
        if not isinstance(content, str):
            refused.append({"name": name, "reason": "content is not a string"})
            continue
        path = os.path.join(incoming, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        written.append({"name": name, "path": path})
    return {"written": written, "refused": refused, "incoming": incoming}


MAX_INDEXED_ROWS = 20000       # a native message may not exceed ~1MB


def handle_indexed():
    """Return what is already indexed: {uuid: {msg_count, updated_at}}.

    READ-ONLY, and deliberately its own message type. It could have been folded
    into an existing handler, but every widening of something that writes is a
    widening of what a compromised extension can do. This opens the database
    with mode=ro and cannot modify anything even in principle; safe_name and the
    write path are untouched by it.

    The extension uses this to mark rows as new / indexed / grown, so bulk
    selection is not guesswork - without it most captures of an already-exported
    account come back SKIPPED or PARTIAL.
    """
    import sqlite3

    db = kb_config.paths()["db"]
    if not os.path.isfile(db):
        return {"ok": True, "indexed": {}, "count": 0, "truncated": False,
                "message": "No index yet at %s" % db}

    uri = "file:%s?mode=ro" % db.replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT conversation_uuid, msg_count, updated_at FROM indexed_convs"
            " LIMIT ?", (MAX_INDEXED_ROWS + 1,)).fetchall()
    finally:
        conn.close()

    truncated = len(rows) > MAX_INDEXED_ROWS
    out = {}
    for uuid, count, updated in rows[:MAX_INDEXED_ROWS]:
        if uuid:
            out[str(uuid).lower()] = {"msg_count": count or 0,
                                      "updated_at": updated or ""}
    return {"ok": True, "indexed": out, "count": len(out), "truncated": truncated}


def _run(args):
    py = kb_config.get("python") or sys.executable
    scripts = _scripts_dir()
    r = subprocess.run([py, os.path.join(scripts, args[0])] + list(args[1:]),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=scripts)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _parse_summary(out):
    """SUMMARY NEW=.. UPDATED=.. SKIPPED=.. PARTIAL=.. REJECTED=.. ROWS=.. -> dict."""
    line = next((l.strip() for l in out.splitlines() if l.startswith("SUMMARY")), None)
    counts = {}
    if line:
        for tok in line.split()[1:]:
            if "=" in tok:
                k, _, v = tok.partition("=")
                try:
                    counts[k.lower()] = int(v)
                except ValueError:
                    counts[k.lower()] = v
    return line, counts


def handle_ingest():
    """Ingest every pending capture and report what became of each one.

    Deliberately processes ALL pending captures, not only the ones just written.
    The archiving step runs the normal ingest runner, which processes whatever
    is in incoming/ regardless - so reporting on a subset would mean the numbers
    shown to the user did not describe what actually happened to the index.

    Reads the machine-readable per-file lines rather than prose. INGESTED,
    PARTIAL, SKIPPED and REJECTED are kept distinct all the way to the caller,
    so a capture that was held back or refused can never be presented as a
    successful ingest.
    """
    _root, incoming, _processed = _paths()
    os.makedirs(incoming, exist_ok=True)

    import glob
    targets = sorted(glob.glob(os.path.join(incoming, REQUIRED_PREFIX + "*.json")))
    if not targets:
        return {"ok": True, "status": "none", "message": "No captures to ingest.",
                "ingested": [], "partial": [], "skipped": [], "rejected": [],
                "counts": {}, "summary": None}

    rc, out = _run(["claude_kb.py", "update-web"] + targets)
    summary, counts = _parse_summary(out)

    def tagged(tag):
        return [os.path.basename(l[len(tag) + 1:].strip())
                for l in out.splitlines() if l.startswith(tag + " ")]

    ingested, partial, skipped = tagged("INGESTED"), tagged("PARTIAL"), tagged("SKIPPED")
    rejected = []
    for l in out.splitlines():
        if l.startswith("REJECTED "):
            name, _, reason = l[len("REJECTED "):].partition(":")
            rejected.append({"name": name.strip(), "reason": reason.strip()})

    if rc != 0 and not (ingested or partial or skipped):
        return {"ok": False, "status": "error",
                "message": "Ingest failed. " + " | ".join(out.strip().splitlines()[-3:])[:300],
                "ingested": [], "partial": [], "skipped": [], "rejected": rejected,
                "counts": counts, "summary": summary}

    # Archive what was accepted, and log it, by handing over to the normal
    # runner. Rejected files are left in incoming/ by design.
    _run(["kb_ingest.py"])

    held_back = bool(partial or rejected)
    if ingested and held_back:
        status = "mixed"
    elif ingested:
        status = "ingested"
    elif rejected and not partial:
        status = "rejected"
    elif partial:
        status = "partial"
    elif skipped:
        status = "unchanged"
    else:
        status = "none"

    bits = []
    if counts.get("new"):
        bits.append("%d new" % counts["new"])
    if counts.get("updated"):
        bits.append("%d updated" % counts["updated"])
    if skipped:
        bits.append("%d unchanged" % len(skipped))
    if partial:
        bits.append("%d PARTIAL (shorter than what is indexed - not replaced): %s"
                    % (len(partial), ", ".join(partial)))
    if rejected:
        bits.append("%d REJECTED (%s)"
                    % (len(rejected),
                       "; ".join("%s: %s" % (r["name"], r["reason"]) for r in rejected)[:200]))
    message = ", ".join(bits) if bits else "Nothing to do."

    return {"ok": True, "status": status, "message": message,
            "ingested": ingested, "partial": partial, "skipped": skipped,
            "rejected": rejected, "counts": counts, "summary": summary}


# ---------------------------------------------------------------- dispatch
def handle(msg):
    kind = (msg.get("type") or "").strip()

    if BOOT_ERROR:
        return {"ok": False, "status": "error",
                "message": "Claude KB host is installed but not configured: " + BOOT_ERROR}

    if kind == "ping":
        try:
            root, incoming, _p = _paths()
        except Exception as e:                    # noqa: BLE001
            return {"ok": False, "status": "error",
                    "message": "config error: %s: %s" % (type(e).__name__, e)}
        return {"ok": True, "status": "ready", "root": root, "incoming": incoming,
                "accepts": REQUIRED_PREFIX + "*" + "|".join(ALLOWED_EXTENSIONS)}

    if kind == "save":
        saved = handle_save(msg)
        ok = bool(saved["written"])
        return {"ok": ok,
                "status": "saved" if ok else "rejected",
                "message": ("Wrote %d file(s) to incoming." % len(saved["written"]))
                           if ok else "Nothing written; every file failed the write guard.",
                **saved}

    if kind == "indexed":
        return handle_indexed()

    if kind == "ingest":
        return handle_ingest()

    if kind == "save_and_ingest":
        saved = handle_save(msg)
        if not saved["written"]:
            return {"ok": False, "status": "rejected",
                    "message": "Nothing written; every file failed the write guard.",
                    **saved}
        result = handle_ingest()
        result["written"] = saved["written"]
        result["refused"] = saved["refused"]
        result["incoming"] = saved["incoming"]
        return result

    return {"ok": False, "status": "error", "message": "unknown type: %r" % kind}


def main():
    while True:
        msg = read_msg()
        if msg is None:
            return
        try:
            write_msg(handle(msg))
        except Exception as e:                    # noqa: BLE001
            # Always answer. See the module docstring.
            write_msg({"ok": False, "status": "error",
                       "message": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    main()
