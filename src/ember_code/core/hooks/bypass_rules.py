"""Tool calls that are dangerous by their shape, whatever a model thinks of them.

The first of the two layers behind Bypass mode's last check (see
:mod:`ember_code.core.hooks.bypass_guard`). A rule here is precise — it
names a known-dangerous action — so it never needs a probability, and it
covers exactly the actions a classifier is weakest on. Measured against
Laya (``laya-typed-decisions``) on forty labelled tool calls: at the
chosen threshold it missed ``npm publish``, a crontab persistence line,
``mkfs`` and ``chmod -R 777 /etc``. Each is a pattern below.

A match does not block. It asks: the person running Bypass decided to
trust the agent, and the point is that they see the few calls where that
trust matters before they run, not that igni overrules them.

Paths inside the project are ordinary work. ``rm -rf ./build`` is
routine; ``rm -rf ~`` is not. :func:`flag_shell` is given the project
directory so the deletion rule can tell them apart.
"""

from __future__ import annotations

import re
import shlex
from fnmatch import fnmatch
from pathlib import Path

#: Files that are almost never a legitimate thing for an agent to read on its
#: own — private keys, cloud and registry credentials, auth stores. Reading one
#: is the shape of credential theft (the first half of an exfiltration, and the
#: half a network rule misses when the send happens later or out of band), so
#: Bypass stops to ask even though the call only reads. Deliberately NARROWER
#: than ``safety.protected_paths``: that list gates *writes* and includes
#: routine files like ``.env`` a developer reads constantly; these are the
#: files whose *read* is itself suspicious.
_SECRET_NAME_GLOBS: tuple[str, ...] = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.ppk",
    "*.pem",
    "*.p12",
    "*.pfx",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials.json",
    "service-account*.json",
)
_SECRET_PATH_SUBSTR: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/credentials",
    "/.docker/config.json",
    "/.kube/config",
    "/.config/gcloud/",
)
#: Utilities whose job is to emit a file's bytes — enough to lift a secret.
_READ_UTILS = r"cat|bat|less|more|head|tail|nl|tac|xxd|od|hexdump|strings|base64|cp|rsync|install|openssl|grep|egrep|rg|ag|ack"
#: Secret/key targets as they appear inside a shell command.
_SECRET_IN_SHELL = (
    r"(~?/?\.ssh/|\bid_rsa\b|\bid_ed25519\b|\bid_ecdsa\b|\bid_dsa\b|\.aws/credentials|"
    r"\.netrc\b|\.git-credentials\b|\.pypirc\b|\.npmrc\b|\.kube/config|\.docker/config\.json|"
    r"\.pem\b|\.p12\b|\.pfx\b)"
)


def _is_secret_file(path: str) -> bool:
    """True when ``path`` names a credential, private key or auth store."""
    if not path:
        return False
    name = Path(path).name
    if any(fnmatch(name, glob) for glob in _SECRET_NAME_GLOBS):
        return True
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    return any(sub in normalized for sub in _SECRET_PATH_SUBSTR)


def flag_file_read(path: str) -> str | None:
    """Why reading ``path`` needs a person's yes: it is a credential or key file.

    Covers the read-only file tools (``read_file`` and friends) that Bypass
    otherwise waves through — reading ``~/.ssh/id_rsa`` is not the ordinary
    read those exemptions were written for."""
    return "reads a credential or secret file" if _is_secret_file(path) else None


#: (pattern, reason) for shell commands. Searched case-insensitively
#: anywhere in the command, so a chained ``cd x && …`` is still seen.
_SHELL_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), why)
    for p, why in (
        (
            r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b",
            "pipes a download straight into a shell",
        ),
        (
            r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?python[0-9.]*\b",
            "pipes a download straight into Python",
        ),
        (
            r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b)",
            "force-pushes, rewriting shared history",
        ),
        (r"\bgit\s+push\b[^;&|]*\s\+\S+", "force-pushes a refspec"),
        (
            r"\b(mkfs(\.\w+)?|fdisk|parted|diskutil\s+(erase|partition))\b",
            "formats or repartitions a disk",
        ),
        (r"\bdd\b[^;&|]*\bof=/dev/", "writes raw data to a device"),
        (r">\s*/dev/(sd|disk|nvme|hd)", "overwrites a block device"),
        (
            r"\bchmod\s+(-R\s+)?0?777\s+/(etc|usr|bin|sbin|var|System|Library)?\b",
            "opens system files to everyone",
        ),
        (
            r"\b(crontab\b[^;&|]*-|launchctl\s+load|systemctl\s+enable)",
            "installs something that runs on its own",
        ),
        (
            r">>?\s*~?/?(\$HOME/)?\.(bashrc|zshrc|profile|bash_profile|zprofile)\b",
            "edits a shell start-up file",
        ),
        (
            r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b|\bdocker\s+push\b",
            "publishes a package or image",
        ),
        (
            r"\bterraform\s+(destroy|apply\b[^;&|]*-auto-approve)",
            "changes or destroys infrastructure without review",
        ),
        (r"\bkubectl\s+(delete|drain|cordon)\b", "removes or disrupts cluster resources"),
        (
            r"\baws\s+\S+\s+(rm|delete\S*|terminate\S*)\b|\bgcloud\b[^;&|]*\bdelete\b|\baz\b[^;&|]*\bdelete\b",
            "deletes cloud resources",
        ),
        (
            r"\b(drop|truncate)\s+(table|database|schema)\b|\bdelete\s+from\s+\w+\s*;?\s*['\"]?\s*$",
            "drops or wipes database data",
        ),
        (
            r"(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc|\.npmrc|\.pypirc|\.env\b)[^|]*\|\s*(curl|wget|nc|ncat|scp)\b",
            "sends credentials over the network",
        ),
        (
            r"\b(env|printenv|set)\s*\|\s*(curl|wget|nc|ncat)\b",
            "sends environment variables over the network",
        ),
        (
            rf"\b({_READ_UTILS})\b[^|;&]*{_SECRET_IN_SHELL}",
            "reads a credential or secret file",
        ),
        (
            # An inline interpreter one-liner that both reaches the network AND
            # touches env/secrets — the code-shaped exfil a `curl` rule misses.
            r"\b(python[0-9.]*|node|ruby|perl|php)\b(?=[^;&|]*\s-[ce]\b)"
            r"(?=[^&|]*(requests|urllib|urlopen|http\.client|httpx|net/http|axios|fetch\(|socket\.|Net::HTTP))"
            r"(?=[^&|]*(environ|getenv|process\.env|\.ssh|credentials|\.env\b|ENV\[))",
            "runs an inline script that sends secrets or environment out",
        ),
        (r"\bscp\b[^;&|]*\S+@\S+:", "copies files to another machine"),
        (
            r"\b(setenforce\s+0|ufw\s+disable|systemctl\s+(stop|disable)\s+(firewalld|ufw|apparmor))\b",
            "switches off system security",
        ),
        (r"\bsudo\s+rm\b", "deletes files as root"),
        (
            r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f[a-z]*d|checkout\s+--\s+\.)\b",
            "discards uncommitted work",
        ),
    )
)

#: Delete-style commands whose targets are judged by where they point.
_DELETERS = {"rm", "rmdir", "shred", "unlink", "trash"}


def flag_shell(command: str, project_dir: Path | None) -> str | None:
    """Why ``command`` needs a person's yes, or None when nothing matched."""
    text = command or ""
    for pattern, why in _SHELL_RULES:
        if pattern.search(text):
            return why
    outside = _deletes_outside(text, project_dir)
    if outside:
        return f"deletes {outside}, outside this project"
    return None


#: A secret riding in a URL: a shell variable the shell would expand, or a
#: query parameter whose name says what it carries.
_SECRET_IN_URL = re.compile(
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*|[?&](access_?token|token|api_?key|key|secret|password|passwd|auth)=",
    re.IGNORECASE,
)


def flag_url(url: str) -> str | None:
    """Why fetching ``url`` needs a person's yes: it carries what looks like a secret."""
    if url and _SECRET_IN_URL.search(url):
        return "sends what looks like a secret or token in a URL"
    return None


def flag_write(path: str, project_dir: Path | None) -> str | None:
    """Why writing ``path`` needs a person's yes: it lands outside the project."""
    if not path or project_dir is None:
        return None
    target = _resolve(path, project_dir)
    if target is None:
        return None
    if not _is_within(target, project_dir):
        return f"writes {path}, outside this project"
    return None


def _deletes_outside(command: str, project_dir: Path | None) -> str | None:
    if project_dir is None:
        return None
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if not words:
            continue
        if words[0] == "sudo":
            words = words[1:]
        if not words or Path(words[0]).name not in _DELETERS:
            continue
        for arg in words[1:]:
            if arg.startswith("-"):
                continue
            if arg in ("/", "~", "~/", "*", "/*", ".."):
                return arg
            target = _resolve(arg, project_dir)
            if target is not None and not _is_within(target, project_dir):
                return arg
    return None


def _resolve(path: str, project_dir: Path) -> Path | None:
    try:
        expanded = Path(path).expanduser()
        return (expanded if expanded.is_absolute() else project_dir / expanded).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(target: Path, project_dir: Path) -> bool:
    try:
        target.relative_to(project_dir.resolve())
        return True
    except ValueError:
        return False
