#!/usr/bin/env python3
r"""kb_open - open export URLs in a real browser, collect the parts, ingest.

Why not an HTTP client: the claude.ai /export/<uuid>/download/<token> URLs never
serve a zip to one. They serve an HTML page whose JavaScript starts the transfer.

Why not Playwright: its bundled Chromium is a fresh automated profile, so
Cloudflare challenges it. The export request is manual anyway, so there is
nothing to gain by automating the browser.

Why a named browser and not webbrowser.open(): that follows the OS default, which
may be a browser you never log into. Opening a live one-time URL in a logged-out
browser spends the token for nothing. Edge is tried first, then Chrome; set
CLAUDE_KB_BROWSER to pin whichever one is logged into claude.ai.

    python kb_open.py            interactive; asks you to confirm the login tab
    python kb_open.py --yes      non-interactive; proves the login with a canary

--yes does NOT simply skip the pre-flight. It spends the light_metadata URL
first, as a canary, because that part is never indexed and so is the only
worthless token in the manifest. If the canary downloads, the browser is
logged in and the valuable URLs are safe to open. If not, nothing is touched.

The canary URL is single-use like every other, so its result is remembered per
manifest in .canary-state.json. Without that, a second run could not tell a
logged-out browser from a URL this script already spent - both produce no download
- and would wrongly blame the login. Entries are pruned when their manifest is
gone from both incoming\ and processed\.

A remembered failure is sticky on purpose: re-running must not spend the token
again just to re-learn the same thing. Clear it deliberately once the login is
fixed:

    python kb_open.py --reset-canary          clear, open nothing, exit
    python kb_open.py --reset-canary --yes    clear, then retry in one run

Exit codes:
    0   parts downloaded and ingest confirmed in ingest.log
    1   nothing to do, or ingest not confirmed
    2   environment problem (browser not found, no manifest, missing setting)
    3   canary failed, browser not logged into claude.ai

Requires the browser set to NOT ask where to save each file.
"""
import argparse, glob, json, os, shutil, subprocess, sys, time, zipfile

import kb_config

try:
    _P = kb_config.paths()
except kb_config.MissingSetting as e:
    sys.stderr.write("ERROR: %s\n" % e)
    sys.exit(2)

ROOT      = _P["root"]
INCOMING  = _P["incoming"]
PROCESSED = _P["processed"]
LOG       = _P["log"]
STATE     = _P["state"]
DOWNLOADS = kb_config.get("downloads")
TASK_NAME = kb_config.get("task")

# light_metadata is never indexed by claude_kb.py. It is never collected, but in
# --yes mode its URL is spent deliberately as a login canary.
SKIP_CATEGORIES = {"light_metadata"}
CANARY_CATEGORY = "light_metadata"
ORDER = {"conversations": 0, "projects": 1, "memories": 2}
PER_FILE_TIMEOUT = 300
CANARY_TIMEOUT = 90
INGEST_TIMEOUT = 120
SETTLE = 2.0

QUIET = False


def say(msg=""):
    if not QUIET:
        print(msg)


def find_browser():
    """Configured browser, else the first of the known candidates that exists."""
    return kb_config.find_browser()


def open_url(browser, url):
    subprocess.Popen([browser, url], close_fds=True)


def newest_manifest():
    m = glob.glob(os.path.join(INCOMING, "manifest-*.json"))
    return max(m, key=os.path.getmtime) if m else None


def wait_for(name, timeout=PER_FILE_TIMEOUT):
    """Wait for <downloads>\\<name> to appear and stop growing."""
    target = os.path.join(DOWNLOADS, name)
    deadline = time.time() + timeout
    last, stable = -1, 0.0
    while time.time() < deadline:
        if os.path.exists(target):
            size = os.path.getsize(target)
            if size == last and size > 0:
                stable += 0.5
                if stable >= SETTLE:
                    return target
            else:
                last, stable = size, 0.0
        time.sleep(0.5)
    return None


def validate(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise zipfile.BadZipFile("member CRC failed")
        return len(z.namelist())


def log_lines():
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def manifest_known(base):
    """True if this manifest is still around, in incoming\\ or processed\\.

    processed\\ names are stamped (<stamp>_<base>), hence the suffix match.
    """
    if os.path.exists(os.path.join(INCOMING, base)):
        return True
    try:
        return any(f.endswith(base) for f in os.listdir(PROCESSED))
    except OSError:
        return False


def load_state():
    """Read .canary-state.json, pruned of manifests that no longer exist.

    Never raises: a missing or corrupt state file is an empty state, because
    losing this only costs one worthless token, while crashing costs the run.
    """
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and manifest_known(k)}


def save_state(state):
    try:
        tmp = STATE + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE)
    except OSError as e:
        say("warning: could not write canary state (%s)" % e)


def reset_canary():
    """Clear remembered canary results so a fresh canary can run.

    Targets the newest manifest in incoming\\, or every entry when there is no
    manifest waiting. Opens nothing. Returns the list of cleared entries.
    """
    state = load_state()
    if not state:
        print("canary state is already empty - nothing to reset.")
        return []

    man = newest_manifest()
    if man:
        base = os.path.basename(man)
        if base in state:
            del state[base]
            save_state(state)
            print("cleared canary state for %s" % base)
            return [base]
        print("no canary entry for %s - nothing to reset." % base)
        return []

    cleared = sorted(state)
    save_state({})
    print("no manifest in incoming\\; cleared all %d canary entries: %s"
          % (len(cleared), ", ".join(cleared)))
    return cleared


def run_canary(browser, data, manifest_base):
    """Spend the worthless light_metadata URL to prove the browser is logged in.

    The result is remembered per manifest, because the canary URL is single-use
    like every other. Without that memory a second run cannot tell a logged-out
    browser from a URL this script already spent - both produce no download - and
    would blame the login either way.

    Returns True to proceed, False to abort. A manifest with no light_metadata
    entry proceeds without a canary.
    """
    state = load_state()
    prior = state.get(manifest_base)
    if prior == "passed":
        say("canary already passed for this manifest, proceeding")
        return True
    if prior == "failed":
        print("canary previously failed for this manifest. If you have since "
              "logged into the browser, re-run with --reset-canary to try again. If "
              "the URLs are spent, request a fresh export instead.")
        return False

    entry = next((d for d in data.get("data_files", [])
                  if d.get("category") == CANARY_CATEGORY), None)
    if not entry:
        say("No %s part in this manifest - no canary available, proceeding."
            % CANARY_CATEGORY)
        return True

    name = entry.get("filename") or "%s-000.zip" % CANARY_CATEGORY
    say("Canary: opening %s to prove the browser is logged in." % name)
    open_url(browser, entry["export_url"])
    got = wait_for(name, timeout=CANARY_TIMEOUT)
    if not got:
        # State the observation, not a guess at the cause: a missing download
        # means either a logged-out browser or an already-spent URL.
        state[manifest_base] = "failed"
        save_state(state)
        print("CANARY FAILED - light_metadata did not download. Either the browser "
              "is not logged into claude.ai, or this URL was already spent. "
              "No valuable URLs touched.")
        return False

    # Never indexed, so keep it out of incoming\ and out of the archive.
    try:
        os.remove(got)
    except OSError:
        pass
    state[manifest_base] = "passed"
    save_state(state)
    say("Canary ok - the browser is logged in.\n")
    return True


def confirm_ingest(pre_count):
    """Fire the ingest task, then wait for a genuinely new ingest.log line.

    The trigger's exit status is checked. It used to be discarded, which meant a
    task that does not exist - the state of every fresh install until someone
    creates it - produced a 120-second wait and then "ingest triggered but no
    new ingest.log line", a sentence that is false in its first two words. The
    parts had downloaded, the one-time URLs were spent, and the message pointed
    at the wrong problem.
    """
    say("Running ingest...")
    r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                       capture_output=True, text=True)
    if r.returncode != 0:
        detail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        detail = detail[-1].strip() if detail else "no output"
        print("COULD NOT TRIGGER THE INGEST TASK %r: %s" % (TASK_NAME, detail))
        print("")
        print("  The downloaded parts are SAFE in %s" % INCOMING)
        print("  and nothing has been lost - only the ingest did not start.")
        print("")
        print("  If the task has never been created (the usual cause on a new")
        print("  install), create it once:")
        print("      powershell -NoProfile -ExecutionPolicy Bypass \\")
        print("          -File install-task.ps1 -ScriptsDir \"<this directory>\"")
        print("")
        print("  Or ingest right now without it:")
        print("      %s kb_ingest.py" % (kb_config.get("python") or "python"))
        return 1

    deadline = time.time() + INGEST_TIMEOUT
    while time.time() < deadline:
        cur = log_lines()
        if len(cur) > pre_count:
            print(cur[-1])
            return 0
        time.sleep(2)
    print("the task was triggered but wrote no new ingest.log line after %ds - "
          "check %s" % (INGEST_TIMEOUT, LOG))
    return 1


def main():
    global QUIET
    ap = argparse.ArgumentParser(
        description="Open Claude export URLs in a browser, collect the parts, ingest.")
    ap.add_argument("--yes", action="store_true",
                    help="non-interactive; prove login with a light_metadata "
                         "canary instead of the confirmation prompt")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress progress output; errors and results still print")
    ap.add_argument("--reset-canary", action="store_true", dest="reset_canary",
                    help="forget the remembered canary result for the newest "
                         "manifest (or all of them if none is waiting). Alone it "
                         "exits without opening anything; with --yes it clears "
                         "first, then runs a fresh canary")
    a = ap.parse_args()
    QUIET = a.quiet

    if a.reset_canary:
        reset_canary()
        if not a.yes:
            return 0

    browser = find_browser()
    if not browser:
        print("ERROR: no browser found. Set CLAUDE_KB_BROWSER (or \"browser\"\n"
              "       in config.json) to the full path of a browser that is\n"
              "       logged into claude.ai.")
        return 2

    manifest = newest_manifest()
    if not manifest:
        print("No manifest-*.json in incoming\\. Request an export first.")
        return 2

    with open(manifest, "r", encoding="utf-8") as f:
        data = json.load(f)
    files = [d for d in data.get("data_files", [])
             if d.get("category") not in SKIP_CATEGORIES]
    files.sort(key=lambda d: ORDER.get(d.get("category"), 99))
    if not files:
        print("Manifest has no fetchable parts.")
        return 1

    say("Manifest: %s" % os.path.basename(manifest))

    # Pre-flight: never spend a one-time token in a logged-out browser.
    if a.yes:
        if not run_canary(browser, data, os.path.basename(manifest)):
            return 3
    else:
        say("\nOpening claude.ai in your browser to confirm you are logged in.")
        open_url(browser, "https://claude.ai/api/organizations")
        ans = input("Does that tab show JSON (not a login page)? [y/N]: ").strip()
        if ans.lower() not in ("y", "yes"):
            print("Aborted. No URLs spent. Log into claude.ai in the browser, "
                  "then re-run.")
            return 1

    say("\nOpening %d export URLs.\n" % len(files))
    ok = failed = 0
    for d in files:
        name = d["filename"]
        dest = os.path.join(INCOMING, name)
        if os.path.exists(dest):
            say("skip (already in incoming): %s" % name)
            continue

        say("opening %-22s ..." % name)
        open_url(browser, d["export_url"])

        got = wait_for(name)
        if not got:
            print("FAILED %s: TIMEOUT" % name)
            print("   No %s in %s after %ds." % (name, DOWNLOADS, PER_FILE_TIMEOUT))
            print("   Check the tab: 'Expired link' means the URL was already")
            print("   used and you need a fresh export.")
            failed += 1
            continue

        try:
            members = validate(got)
        except Exception as e:
            print("FAILED %s: BAD ZIP (%s)" % (name, e))
            failed += 1
            continue

        shutil.move(got, dest)
        say("ok: %s (%d members)" % (name, members))
        ok += 1

    print("\n%d ok, %d failed" % (ok, failed))
    if ok == 0:
        print("Nothing new. Not running ingest.")
        return 1

    return confirm_ingest(len(log_lines()))


if __name__ == "__main__":
    sys.exit(main())
