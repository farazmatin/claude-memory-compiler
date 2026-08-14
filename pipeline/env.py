"""The `.env` file: loading it, checking it, and editing it without leaking it.

`docker compose` reads `.env` on its own, but nothing else did: every setting in
`pipeline/config.py` comes from `os.environ`, so a token written to `.env` reached
the containers and never reached the pipeline. Diarization then failed the way it
always fails - silently, with unowned action items - while the file that was
supposed to fix it sat on disk looking correct.

Loading happens once, at `pipeline.config` import. A real environment variable
always wins over the file, so a scheduled task or a shell export can still
override a value without editing anything.

**Secrets never pass through stdout.** Everything here reports whether a key is
configured, never what it is set to. That is what makes the setup scripts safe to
run with a terminal recorder going, and what keeps a pasted `doctor` report from
being a credential leak.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# Overridable so tests never read the developer's real .env, and so a service
# account can point at a file outside the repository.
ENV_FILE = Path(os.environ.get("MMC_ENV_FILE", ROOT_DIR / ".env"))
ENV_EXAMPLE_FILE = ROOT_DIR / ".env.example"

# Without these the stack does not start: compose refuses to boot LightRAG or
# Postgres when they are empty, by design.
REQUIRED_SECRETS = ("MMC_LIGHTRAG_API_KEY", "POSTGRES_PASSWORD")

# Generatable locally, with no account or licence attached.
GENERATED_SECRETS = REQUIRED_SECRETS

# Needs a person with a browser: an account token plus two accepted licences.
MANUAL_SECRETS = ("HF_TOKEN",)

CONFIGURED = "configured"
MISSING = "missing"

# Characters that survive an unquoted .env value in both compose's parser and the
# one below. Anything else gets quoted.
_SAFE_UNQUOTED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "_-./:@+=,~"
)

_loaded_from: Path | None = None


def parse(text: str) -> dict[str, str]:
    """Parse `.env` content into a mapping.

    Deliberately close to what `docker compose` accepts, because the same file
    configures both and a value that means two different things in the two
    readers is worse than one that fails in both.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            # Double quotes carry escapes, as they do for compose. Without this,
            # a name with a quote in it comes back with the backslash still in.
            value = _unescape(value[1:-1])
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            # Single quotes are literal, also as compose treats them.
            value = value[1:-1]
        else:
            # Unquoted values end at an inline comment, matching compose. The
            # leading space is required so a value like `pass#word` survives.
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        values[key] = value
    return values


def read_values(path: Path | None = None) -> dict[str, str]:
    """Values in the file, without touching the process environment."""
    target = Path(path) if path else ENV_FILE
    try:
        return parse(target.read_text(encoding="utf-8"))
    except OSError:
        return {}


def load(path: Path | None = None) -> bool:
    """Copy `.env` into `os.environ`. Returns False when there is no file.

    Existing environment variables are never overwritten: the file is the
    default, not the authority.
    """
    global _loaded_from
    target = Path(path) if path else ENV_FILE
    values = read_values(target)
    if not target.exists():
        return False
    for key, value in values.items():
        os.environ.setdefault(key, value)
    _loaded_from = target
    return True


def loaded_from() -> Path | None:
    """The file `load()` last read, for reporting where a value came from."""
    return _loaded_from


def generate_secret(nbytes: int = 32) -> str:
    """A secret suitable for MMC_LIGHTRAG_API_KEY or POSTGRES_PASSWORD.

    URL-safe rather than raw base64: this value ends up in a Postgres connection
    string, and a `/` or `+` there has to be escaped by whoever writes it next.
    """
    return secrets.token_urlsafe(nbytes)


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_UNESCAPES = {escaped[1]: raw for raw, escaped in _ESCAPES.items()}


def format_value(value: str) -> str:
    """Render a value for `.env`, quoting only when it would otherwise change."""
    if value and all(character in _SAFE_UNQUOTED for character in value):
        return value
    escaped = "".join(_ESCAPES.get(character, character) for character in value)
    return f'"{escaped}"'


def _unescape(value: str) -> str:
    """Reverse `format_value`'s escaping, in one left-to-right pass.

    One pass matters: replacing `\\\\` and then `\\"` separately would turn a
    literal backslash-quote into an unescaped quote.
    """
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            result.append(_UNESCAPES.get(following, "\\" + following))
            index += 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def set_value(key: str, value: str, path: Path | None = None) -> None:
    """Set one key in `.env`, preserving comments and the order of the rest.

    Rewrites the existing assignment in place when there is one - appending a
    second `KEY=` line would leave the file with two answers, and the two readers
    (compose and `parse` above) both take the last one, which is not where anyone
    looks.
    """
    target = Path(path) if path else ENV_FILE
    rendered = f"{key}={format_value(value)}"

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    replaced = False
    for position, line in enumerate(lines):
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[len("export "):].lstrip()
        if candidate.startswith(f"{key}=") or candidate == key:
            lines[position] = rendered
            replaced = True
            break
    if not replaced:
        lines.append(rendered)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _restrict_permissions(target)
    # The running process should agree with the file it just wrote.
    os.environ[key] = value


def create_from_example(path: Path | None = None, example: Path | None = None) -> bool:
    """Seed `.env` from `.env.example`. Returns False if one already exists.

    Never overwrites: the existing file holds a Postgres password that the
    running database still expects, and replacing it silently locks the corpus
    away behind credentials nobody has.
    """
    target = Path(path) if path else ENV_FILE
    if target.exists():
        return False
    source = Path(example) if example else ENV_EXAMPLE_FILE
    target.write_text(
        source.read_text(encoding="utf-8") if source.exists() else "",
        encoding="utf-8",
    )
    _restrict_permissions(target)
    return True


def fill_generated_secrets(path: Path | None = None) -> list[str]:
    """Generate any missing local secret. Returns the keys that were written.

    Only fills blanks. A key that already has a value is left exactly as it is,
    for the same reason `create_from_example` refuses to overwrite.
    """
    target = Path(path) if path else ENV_FILE
    values = read_values(target)
    written: list[str] = []
    for key in GENERATED_SECRETS:
        if values.get(key, "").strip():
            continue
        set_value(key, generate_secret(), target)
        written.append(key)
    return written


def is_configured(key: str, path: Path | None = None) -> bool:
    """True when the key has a non-empty value, in the environment or the file."""
    if os.environ.get(key, "").strip():
        return True
    return bool(read_values(path).get(key, "").strip())


def status(key: str, path: Path | None = None) -> str:
    """`configured` or `missing`. Never the value."""
    return CONFIGURED if is_configured(key, path) else MISSING


def _restrict_permissions(path: Path) -> None:
    """Owner-only, where the platform has a notion of it.

    Windows ignores the POSIX mode bits, so this is not the whole story there;
    what does the work on Windows is that `.env` stays in the user's own profile
    directory and out of git.
    """
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
