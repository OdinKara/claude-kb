#!/usr/bin/env python3
"""build_extension - stage the MCP extension from the single source of truth.

The repo holds ONE claude_kb.py, at the root. The .mcpb format requires the
entry point to exist inside the bundle, so the extension cannot simply point at
it - but a checked-in second copy is what drifted last time (the extension copy
sat 46 lines behind for six weeks, silently missing the multi-part export
handling). So the copy is generated here at build time instead, and gitignored.

manifest.json is generated the same way, from manifest.example.json plus the
resolved config, because a placeholder a human edits by hand is a step people
get wrong.

    python build_extension.py              write kb-extension/build/
    python build_extension.py --print      show the resolved values, write nothing

Then point Claude Desktop at kb-extension/build/, or pack it with the mcpb CLI.
"""
import argparse, json, os, shutil, sys

import kb_config

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "kb-extension")
TEMPLATE = os.path.join(EXT, "manifest.example.json")
BUILD = os.path.join(EXT, "build")
SOURCE_MODULES = ["claude_kb.py", "kb_config.py"]


def resolve():
    """The substitutions applied to manifest.example.json."""
    root = kb_config.root()
    return {
        "{{ROOT}}": root,
        "{{DB_PATH}}": os.path.join(root, "claude_kb.db"),
        "{{PYTHON}}": kb_config.get("python"),
        "{{AUTHOR}}": kb_config.get("author"),
    }


def render(values):
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        text = f.read()
    for k, v in values.items():
        # json.dumps to escape backslashes: Windows paths land inside JSON
        # strings, and a raw C:\Users\... would produce an invalid document.
        text = text.replace(k, json.dumps(v)[1:-1])
    json.loads(text)  # fail here, not in Claude Desktop
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", action="store_true", dest="show",
                    help="print resolved values and exit without writing")
    a = ap.parse_args()

    try:
        values = resolve()
    except kb_config.MissingSetting as e:
        print("ERROR: %s" % e)
        return 2

    if a.show:
        for k, v in values.items():
            print("  %-14s %s" % (k.strip("{}"), v))
        return 0

    manifest = render(values)
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(manifest)
    for mod in SOURCE_MODULES:
        shutil.copy2(os.path.join(HERE, mod), os.path.join(BUILD, mod))

    print("built %s" % BUILD)
    print("  manifest.json   generated from manifest.example.json")
    for mod in SOURCE_MODULES:
        print("  %-15s copied from the repo root" % mod)
    print("\nLoad that directory in Claude Desktop, or pack it: mcpb pack %s" % BUILD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
