#!/usr/bin/env python3
"""kb_config - one place where every environment-specific value is resolved.

Resolution order for every key, highest priority first:

    1. environment variable        CLAUDE_KB_ROOT=...
    2. config file                 config.json  (see config.example.json)
    3. built-in default            where a safe one exists

The config file is found at $CLAUDE_KB_CONFIG, else config.json beside these
scripts. A missing config file is not an error - environment variables and
defaults may well cover everything.

`root` deliberately has NO default. There is no location this tool can guess
that would be right for two different people, and quietly indexing into the
wrong directory is worse than refusing to start, so require() raises
MissingSetting naming the key, the environment variable, and the config file.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CLAUDE_KB_CONFIG") or os.path.join(HERE, "config.json")

# Browser candidates, in search order. Edge first because the export URLs are
# opened in whichever browser is logged into claude.ai and Edge is the common
# Windows case, but Chrome is searched too so this is not an Edge-only tool.
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.path.expanduser("~"),
                 r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
]

# key -> (environment variable, default). A default of _REQUIRED means the
# setting must be supplied; _AUTO means it is derived below.
_REQUIRED = object()
_AUTO = object()

_SPEC = {
    "root":       ("CLAUDE_KB_ROOT",       _REQUIRED),
    "downloads":  ("CLAUDE_KB_DOWNLOADS",  _AUTO),
    "task":       ("CLAUDE_KB_TASK",       "ClaudeKB-Ingest"),
    "python":     ("CLAUDE_KB_PYTHON",     _AUTO),
    "browser":    ("CLAUDE_KB_BROWSER",    _AUTO),
    "export_dir": ("CLAUDE_KB_EXPORT_DIR", _AUTO),
    "http_port":  ("CLAUDE_KB_HTTP_PORT",  8760),
    "author":     ("CLAUDE_KB_AUTHOR",     "Unknown"),
}


class MissingSetting(Exception):
    """A required setting was not supplied by env, config file, or default."""


def _load_file():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        # A malformed config is worth shouting about: unlike a missing one, it
        # means someone tried to configure this and got it wrong.
        raise MissingSetting(
            "config file %s could not be read (%s). Fix the JSON or delete the "
            "file to fall back to environment variables and defaults."
            % (CONFIG_PATH, e))


_FILE = None


def _file_cfg():
    global _FILE
    if _FILE is None:
        _FILE = _load_file()
    return _FILE


def get(key, default=None):
    """Resolve one setting. Returns `default` if it is unset and has no default."""
    if key not in _SPEC:
        raise KeyError("unknown setting %r" % key)
    env_var, built_in = _SPEC[key]

    val = os.environ.get(env_var)
    if val is None:
        val = _file_cfg().get(key)
    if val is not None:
        return _coerce(key, val)

    if built_in is _REQUIRED:
        return default
    if built_in is _AUTO:
        return _derive(key)
    return built_in


def _coerce(key, val):
    if key == "http_port":
        return int(val)
    if key == "browser" and isinstance(val, str):
        return [val]
    return val


def _derive(key):
    if key == "downloads":
        return os.path.join(os.path.expanduser("~"), "Downloads")
    if key == "python":
        return sys.executable
    if key == "browser":
        return list(BROWSER_CANDIDATES)
    if key == "export_dir":
        return None          # discovered under root at call time
    return None


def require(key):
    """Resolve a setting, or raise MissingSetting naming exactly what to set."""
    val = get(key)
    if val in (None, ""):
        env_var = _SPEC[key][0]
        raise MissingSetting(
            "required setting '%s' is not configured.\n"
            "  Set the environment variable %s, or add \"%s\" to %s.\n"
            "  See config.example.json for every supported key."
            % (key, env_var, key, CONFIG_PATH))
    return val


def root():
    """The KB working directory. Required; never guessed."""
    return require("root")


def paths():
    """Every derived path, resolved from root in one place."""
    r = root()
    return {
        "root":      r,
        "db":        os.path.join(r, "claude_kb.db"),
        "incoming":  os.path.join(r, "incoming"),
        "processed": os.path.join(r, "processed"),
        "log":       os.path.join(r, "ingest.log"),
        "state":     os.path.join(r, ".canary-state.json"),
    }


def export_dir():
    """The unpacked export directory used by `build`.

    Configured explicitly, else the newest data-* directory under root. Returns
    None when there is none, so callers can report that rather than crash on a
    path built from someone else's export id.
    """
    import glob
    cfg = get("export_dir")
    if cfg:
        return cfg
    hits = [d for d in glob.glob(os.path.join(root(), "data-*")) if os.path.isdir(d)]
    return max(hits, key=os.path.getmtime) if hits else None


def find_browser():
    """First browser executable that exists, or None.

    An explicitly configured browser that does not exist is an error rather
    than a reason to fall through to a guess: falling back would open a live
    one-time URL in a browser the user never chose and may not be logged into.
    """
    configured = os.environ.get("CLAUDE_KB_BROWSER") or _file_cfg().get("browser")
    if configured:
        cands = [configured] if isinstance(configured, str) else list(configured)
        for p in cands:
            if os.path.exists(p):
                return p
        raise MissingSetting(
            "configured browser not found: %s\n"
            "  Set CLAUDE_KB_BROWSER (or \"browser\" in %s) to the full path of "
            "a browser that is logged into claude.ai."
            % (", ".join(cands), CONFIG_PATH))

    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None
