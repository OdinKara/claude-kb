#!/usr/bin/env python3
"""deploy_extension - push the browser extension from this repo to the live install.

WHY THIS EXISTS
---------------
The browser extension exists twice: the source of truth in this repo, and the
directory the browser actually loads. Nothing kept them in sync, and they drifted
twice - most recently the live install was three commits behind and silently
missing the whole donate change while every check reported success.

build_extension.py already solved exactly this shape of problem for the MCP
extension ("a checked-in second copy is what drifted last time"). This is the
same idea for the browser extension.

THE DESTINATION PATH IS LOAD-BEARING
------------------------------------
An unpacked extension's ID is derived from its ABSOLUTE PATH, and that ID is
registered with the native messaging host in allowed_origins. Move the directory
and the ID changes, native messaging breaks, and the popup reports that the host
did not answer. Measured on this machine: the live path yields
nmohhionglkakcpmfkmdeihkekkahmdh, while loading the repo copy directly yields
mdnnaopdlondicnhnhfcckepmkmhbcha - same files, different path, different ID.

So this script:
  * reads the destination from config (extension_dir), never hardcodes it;
  * REFUSES to create it - a missing destination means something moved, and
    creating a directory at a new path is how the ID gets broken by accident;
  * copies file CONTENTS only. It never makes a symlink or junction, never
    relocates anything, and never touches the manifest.

Usage
-----
    python deploy_extension.py --check     compare only; non-zero if out of sync
    python deploy_extension.py             deploy repo -> live install
    python deploy_extension.py --dry-run   show what deploying would change

--check is wired into this repo's pre-push hook, so an out-of-date live install
fails loudly before a push instead of being noticed weeks later.

Exit codes:
    0  in sync (--check), or deployed successfully
    1  out of sync (--check), or unexplained edits in the destination
    2  cannot run: destination missing, not a directory, or git unavailable
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys

import kb_config

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "extension")

# Everything the browser actually loads. Explicit rather than a glob so a stray
# file in either tree is reported instead of silently deployed.
FILES = [
    "manifest.json",
    os.path.join("src", "popup.html"),
    os.path.join("src", "popup.css"),
    os.path.join("src", "popup.js"),
    os.path.join("src", "background.js"),
    os.path.join("src", "content.js"),
    os.path.join("src", "capture-core.js"),
]


class Fatal(Exception):
    """Cannot run. Never degrade to a warning - see the hard-fail note above."""


def _run(*args: str) -> str:
    try:
        p = subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise Fatal(f"could not run {args[0]}: {e}")
    return p.stdout if p.returncode == 0 else ""


def destination() -> str:
    """The live install directory, from config. Never created, never guessed."""
    dest = kb_config.get("extension_dir")
    if not dest:
        raise Fatal(
            "extension_dir is not configured.\n"
            f"  Set CLAUDE_KB_EXTENSION_DIR, or add \"extension_dir\" to "
            f"{kb_config.CONFIG_PATH}.")
    if not os.path.exists(dest):
        raise Fatal(
            f"destination does not exist:\n    {dest}\n\n"
            "  REFUSING TO CREATE IT. The browser derives the extension ID from\n"
            "  this exact path, and that ID is registered with the native\n"
            "  messaging host. Creating a directory at a new path would produce a\n"
            "  different ID and silently break native messaging.\n\n"
            "  If the live install genuinely moved, re-point extension_dir at it,\n"
            "  or re-run native/install-host.ps1 with the new -ExtensionId.")
    if not os.path.isdir(dest):
        raise Fatal(f"destination is not a directory:\n    {dest}")
    return dest


def _blob(path: str) -> str:
    """git's hash for a file's CONTENT, whatever tree it sits in."""
    return _run("git", "hash-object", path).strip()


def _known_blobs(rel: str) -> set:
    """Every content hash this repo has ever held for `rel`.

    Used to tell an OLD copy (fine - just stale) apart from an edit made
    directly in the live install that exists nowhere in this repo (not fine -
    it would be destroyed by a deploy, and it may be why the install works).
    """
    repo_rel = f"extension/{rel}".replace(os.sep, "/")
    seen = set()
    commits = _run("git", "rev-list", "--all", "--", repo_rel).split()
    for c in commits:
        h = _run("git", "rev-parse", f"{c}:{repo_rel}").strip()
        if h:
            seen.add(h)
    cur = _blob(os.path.join(SRC, rel))
    if cur:
        seen.add(cur)
    return seen


def survey(dest: str):
    """(differing, unexplained, missing_in_src, extra_in_dest)."""
    differing, unexplained, missing_src = [], [], []

    for rel in FILES:
        s = os.path.join(SRC, rel)
        d = os.path.join(dest, rel)
        if not os.path.exists(s):
            missing_src.append(rel)
            continue
        if not os.path.exists(d):
            differing.append((rel, "absent from the live install"))
            continue
        if filecmp.cmp(s, d, shallow=False):
            continue
        differing.append((rel, f"{os.path.getsize(d)} bytes live "
                               f"vs {os.path.getsize(s)} in repo"))
        if _blob(d) not in _known_blobs(rel):
            unexplained.append(rel)

    known = {os.path.normpath(f) for f in FILES}
    extra = []
    for root, _dirs, files in os.walk(dest):
        for f in files:
            rel = os.path.normpath(os.path.relpath(os.path.join(root, f), dest))
            if rel not in known:
                extra.append(rel)
    return differing, unexplained, missing_src, sorted(extra)


def report(dest, differing, unexplained, missing_src, extra) -> None:
    print(f"  repo: {SRC}")
    print(f"  live: {dest}")
    if missing_src:
        print("\n  MISSING FROM THE REPO (not deployed):")
        for r in missing_src:
            print(f"    {r}")
    if extra:
        print("\n  present in the live install but not tracked here "
              "(left alone):")
        for r in extra:
            print(f"    {r}")
    if differing:
        print(f"\n  OUT OF SYNC - {len(differing)} file(s):")
        for rel, why in differing:
            print(f"    {rel}  ({why})")
    else:
        print("\n  in sync: every tracked file is byte-identical")


def unexplained_error(unexplained) -> None:
    print("\n" + "=" * 72, file=sys.stderr)
    print("REFUSING TO DEPLOY: the live install has edits that exist nowhere in "
          "this repo.", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for rel in unexplained:
        print(f"    {rel}", file=sys.stderr)
    print("\n  These files match no committed version and no working-tree "
          "version.\n"
          "  Someone edited the live install directly. Deploying would destroy\n"
          "  that work -- and it may be exactly why the live install works.\n\n"
          "  Copy the changes back into this repo first, then deploy.",
          file=sys.stderr)


def deploy(dest, differing, dry_run: bool) -> int:
    if not differing:
        print("\n  nothing to do")
        return 0
    print(f"\n  {'would copy' if dry_run else 'copying'} "
          f"{len(differing)} file(s):")
    for rel, _why in differing:
        print(f"    {rel}")
        if not dry_run:
            d = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(os.path.join(SRC, rel), d)
    if dry_run:
        print("\n  --dry-run: nothing was written")
        return 0

    # Verify by comparing the artifacts, never by "the copy returned".
    bad = [rel for rel in FILES
           if os.path.exists(os.path.join(SRC, rel))
           and not filecmp.cmp(os.path.join(SRC, rel),
                               os.path.join(dest, rel), shallow=False)]
    if bad:
        print("\n  VERIFICATION FAILED - still differing after copy:",
              file=sys.stderr)
        for rel in bad:
            print(f"    {rel}", file=sys.stderr)
        return 1
    print("\n  verified: every tracked file is byte-identical")
    print("\n  Now click Reload on the extension's card. The path did not change,\n"
          "  so the extension ID and the native host registration are intact.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="compare only; non-zero if out of sync (used by pre-push)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be copied, write nothing")
    args = ap.parse_args()

    try:
        dest = destination()
    except Fatal as e:
        print(f"\ndeploy_extension: {e}\n", file=sys.stderr)
        return 2

    differing, unexplained, missing_src, extra = survey(dest)

    print("\nextension deploy" + (" [--check]" if args.check else ""))
    report(dest, differing, unexplained, missing_src, extra)

    # Drift the other way is fatal in BOTH modes: it must never be silently
    # overwritten, and it must never pass a check.
    if unexplained:
        unexplained_error(unexplained)
        return 1

    if args.check:
        if differing:
            print("\n  The live install is OUT OF DATE. Update it with:\n"
                  "      python deploy_extension.py\n"
                  "  then click Reload on the extension's card.\n",
                  file=sys.stderr)
            return 1
        print()
        return 0

    return deploy(dest, differing, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
