#!/usr/bin/env python3
"""kb_ingest - 6 AM auto-ingest runner for the Claude KB.

Handles BOTH export layouts:

  legacy    incoming/data-*.zip                  -> one self-contained export zip
  multipart incoming/conversations-000.zip       -> the export is split by category;
            incoming/projects-000.zip               all parts must be merged into one
            incoming/memories-000.zip               tree before claude_kb.py can read
            incoming/light_metadata-000.zip         it (projects/ must sit beside
                                                    conversations.json)

For each export: runs `claude_kb.py update` (incremental upsert, never a wipe),
moves the consumed zip(s) to processed/ with a date stamp, and appends a line to
ingest.log. A manifest-*.json alone is NOT an export - it only lists download
URLs - so it is reported as actionable and left in place.
"""
import subprocess, sys, os, glob, shutil, datetime, tempfile, zipfile

import kb_config

try:
    _P = kb_config.paths()
except kb_config.MissingSetting as e:
    # This runs unattended from a scheduled task, so a config problem must be a
    # legible one-liner on stderr, not a traceback nobody will read.
    sys.stderr.write("ERROR: %s\n" % e)
    sys.exit(2)

ROOT      = _P["root"]
PY        = kb_config.get("python")
SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_kb.py")
INCOMING  = _P["incoming"]
PROCESSED = _P["processed"]
LOG       = _P["log"]

# category zips that make up one multi-part export; conversations is required
PART_CATEGORIES = ("conversations", "projects", "memories", "light_metadata")

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")
    print(msg)


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def run_kb(args):
    """Run claude_kb.py <args>. Returns (ok, summary_line, combined_output)."""
    r = subprocess.run([PY, SCRIPT] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    summary = next((ln.strip() for ln in out.splitlines() if ln.startswith("SUMMARY")), None)
    return (r.returncode == 0 and summary is not None), summary, out


def run_update(target):
    """Run claude_kb.py update <target>. Returns (ok, summary_line, combined_output)."""
    return run_kb(["update", target])


def fail(label, out, rc_note):
    log(f"INGEST FAILED for {label} ({rc_note}) - left in incoming for retry")
    tail = " | ".join(out.strip().splitlines()[-3:])
    log(f"  detail: {tail[:300]}")


def archive(paths, st):
    """Move each consumed file to processed/ with a shared date stamp."""
    moved = []
    for p in paths:
        dst = os.path.join(PROCESSED, f"{st}_{os.path.basename(p)}")
        shutil.move(p, dst)
        moved.append(os.path.basename(dst))
    return moved


def ingest_legacy(zips):
    for z in zips:
        base = os.path.basename(z)
        ok, summary, out = run_update(z)
        if not ok:
            fail(base, out, "legacy zip")
            continue
        moved = archive([z], stamp())
        log(f"ingested {base} -> processed/{moved[0]} | {summary}")


def find_web():
    """Web captures waiting in incoming/. Named by the extension that writes them."""
    return sorted(glob.glob(os.path.join(INCOMING, "claude-web-*.json")))


def ingest_web(paths):
    """Ingest web captures, archiving only the files claude_kb.py accepted.

    A capture is never authoritative, so it can come back PARTIAL - fewer
    messages than are already indexed. That is a correct outcome, not a
    failure: the capture is archived rather than left to retry, because a
    conversation only grows in the index and a partial one will stay partial
    forever.

    A REJECTED file (bad format, truncated messages) is left in incoming/ so it
    is visible rather than quietly filed away.
    """
    names = ", ".join(os.path.basename(p) for p in paths)
    ok, summary, out = run_kb(["update-web"] + paths)
    if not ok:
        fail(names, out, "web capture")
        return

    def tagged(tag):
        return [ln[len(tag) + 1:].strip()
                for ln in out.splitlines() if ln.startswith(tag + " ")]

    # INGESTED reached the index; PARTIAL and SKIPPED did not but were accepted,
    # and are archived all the same - a capture shorter than what is stored
    # stays shorter, and an unchanged one has nothing left to contribute, so
    # leaving either to retry accomplishes nothing.
    ingested = tagged("INGESTED")
    partial = tagged("PARTIAL")
    skipped = tagged("SKIPPED")
    accepted = ingested + partial + skipped

    # Carry the child's reasons into the log. A rejection whose cause is
    # computed and then dropped is undiagnosable the morning after.
    rejected = [ln[len("REJECTED "):].strip()
                for ln in out.splitlines() if ln.startswith("REJECTED ")]
    why = ("; ".join(rejected))[:400] if rejected else ""

    if not accepted:
        log(f"web captures [{names}] - none accepted, left in incoming"
            f"{' (' + why + ')' if why else ''} | {summary}")
        return

    st = stamp()
    archive(accepted, st)
    bits = []
    if ingested:
        bits.append("ingested " + ", ".join(os.path.basename(p) for p in ingested))
    if partial:
        bits.append("PARTIAL (not replaced) "
                    + ", ".join(os.path.basename(p) for p in partial))
    if skipped:
        bits.append("unchanged " + ", ".join(os.path.basename(p) for p in skipped))
    tail = f" ({len(rejected)} rejected, left in incoming: {why})" if rejected else ""
    log(f"web captures: {'; '.join(bits)} -> processed/{st}_*{tail} | {summary}")


def find_parts():
    """Return {category: [zip paths]} for multi-part export zips in incoming/."""
    parts = {}
    for cat in PART_CATEGORIES:
        hits = sorted(glob.glob(os.path.join(INCOMING, f"{cat}-*.zip")))
        if hits:
            parts[cat] = hits
    return parts


def ingest_multipart(parts, manifests):
    """Merge-extract every part zip into one tree, then ingest that tree once."""
    members = [p for cat in parts for p in parts[cat]]
    names = ", ".join(sorted(os.path.basename(p) for p in members))
    if "conversations" not in parts:
        log(f"multi-part export incomplete: have [{names}] but no conversations-*.zip "
            f"- cannot ingest, left in incoming")
        return
    with tempfile.TemporaryDirectory(prefix="kb_merge_") as merged:
        for p in members:
            try:
                with zipfile.ZipFile(p) as z:
                    z.extractall(merged)
            except zipfile.BadZipFile:
                log(f"INGEST FAILED: {os.path.basename(p)} is not a valid zip "
                    f"(truncated download?) - left in incoming for retry")
                return
        ok, summary, out = run_update(merged)
    if not ok:
        fail(names, out, "multi-part set")
        return
    st = stamp()
    moved = archive(members + manifests, st)
    log(f"ingested multi-part [{names}] -> processed/{st}_* "
        f"({len(moved)} files archived) | {summary}")


def main():
    os.makedirs(INCOMING, exist_ok=True)
    os.makedirs(PROCESSED, exist_ok=True)

    legacy    = sorted(glob.glob(os.path.join(INCOMING, "data-*.zip")))
    parts     = find_parts()
    web       = find_web()
    manifests = sorted(glob.glob(os.path.join(INCOMING, "manifest-*.json")))

    if legacy:
        ingest_legacy(legacy)
    if parts:
        ingest_multipart(parts, manifests)
    if web:
        ingest_web(web)
    if not legacy and not parts and not web:
        if manifests:
            # NEVER contact an export URL from here: it cannot succeed from a
            # scheduled context and each attempt burns a one-time token.
            log("manifest waiting - run kb_open.py to download the parts")
        else:
            log("no new export")


if __name__ == "__main__":
    main()
