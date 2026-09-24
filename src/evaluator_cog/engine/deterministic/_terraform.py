"""A reader for the few Terraform facts the runtime rules need.

Where the Terraform is depends on the repository. An ``infrastructure``
repository (mini-app-polis/infra) is a Terraform root: its ``.tf`` files
are at the repository root, with modules under ``modules/``. It declares
every pipeline cog's runtime — the queue, its dead-letter queue, the
function and the mapping between them — through one ``module`` block per
cog (ADR-010). Any other repository that carries Terraform does so in
``infra/``.

This is not an HCL parser, and deliberately so. The checks ask a handful
of questions — is this resource declared, does its block set this
attribute, does this block reference that one — and a brace-matching
scan over comment-stripped text answers them without a dependency the
evaluator would ship to Lambda for five rules. Strings containing braces
are the case it cannot see through; ``jsonencode({...})`` is balanced, so
the redrive policy reads correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_RESOURCE = re.compile(
    r'^\s*resource\s+"(?P<type>[\w-]+)"\s+"(?P<name>[\w-]+)"\s*\{', re.M
)


@dataclass(frozen=True)
class Resource:
    """One ``resource`` block: its type, its name, its body and its file."""

    type: str
    name: str
    body: str
    file: str

    def attr(self, key: str) -> str | None:
        """The right-hand side of a top-level-looking ``key = value`` line.

        The first match anywhere in the block, which is right for the
        scalar attributes asked about here (``timeout``, ``memory_size``)
        and for the ones that only appear nested (``maximum_concurrency``).
        """
        m = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", self.body)
        return m.group(1) if m else None

    def has_block(self, name: str) -> bool:
        """True when the body opens a nested ``name { ... }`` block."""
        return re.search(rf"(?m)^\s*{re.escape(name)}\s*\{{", self.body) is not None


def _strip_comments(text: str) -> str:
    """Drop ``#``, ``//`` and ``/* */`` comments, leaving strings alone.

    A regex cannot tell the ``//`` in ``"http://..."`` from a comment, and
    cutting a line there can take a closing brace with it.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _block_end(text: str, open_brace: int) -> int:
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def terraform_root(repo_path: Path, repo_type: str = "") -> Path:
    """Where a repository's Terraform lives: its root, or ``infra/``."""
    return repo_path if repo_type == "infrastructure" else repo_path / "infra"


def _tf_files(root: Path, *, recursive: bool) -> list[Path]:
    """The ``.tf`` files under ``root``, skipping ``.terraform/`` caches."""
    if not root.is_dir():
        return []
    found = root.rglob("*.tf") if recursive else root.glob("*.tf")
    return sorted(f for f in found if ".terraform" not in f.relative_to(root).parts)


def declares_terraform(repo_path: Path, repo_type: str = "") -> bool:
    """True when the repository's Terraform root holds any ``.tf`` file."""
    return bool(_tf_files(terraform_root(repo_path, repo_type), recursive=True))


def _blocks(
    repo_path: Path, root: Path, pattern: re.Pattern[str], *, recursive: bool
) -> list[Resource]:
    found: list[Resource] = []
    for tf in _tf_files(root, recursive=recursive):
        try:
            text = _strip_comments(tf.read_text(errors="replace"))
        except OSError:
            continue
        rel = tf.relative_to(repo_path).as_posix()
        for m in pattern.finditer(text):
            start = m.end() - 1
            end = _block_end(text, start)
            found.append(
                Resource(
                    type=m.groupdict().get("type") or "module",
                    name=m.group("name"),
                    body=text[start + 1 : end],
                    file=rel,
                )
            )
    return found


def terraform_resources(repo_path: Path, repo_type: str = "") -> list[Resource] | None:
    """Every resource the repository's Terraform declares; None when it has none.

    For an ``infrastructure`` repository that includes its modules, where
    the resources every cog shares are declared once. For anything else it
    is ``infra/*.tf``.

    None and an empty list are different answers: a repository with no
    Terraform root has not started owning infrastructure, one with an
    empty root has started and declared nothing.
    """
    root = terraform_root(repo_path, repo_type)
    recursive = repo_type == "infrastructure"
    if not root.is_dir() or (recursive and not _tf_files(root, recursive=True)):
        return None
    return _blocks(repo_path, root, _RESOURCE, recursive=recursive)


_MODULE = re.compile(r'^\s*module\s+"(?P<name>[\w-]+)"\s*\{', re.M)


def module_calls(repo_path: Path, repo_type: str = "") -> list[Resource]:
    """The ``module`` blocks in the Terraform root's own ``.tf`` files.

    Returned as ``Resource`` with ``type == "module"``, so ``attr`` reads
    their arguments — ``source``, and the inputs a caller sets. Modules'
    own nested calls are not included: the root is where each cog's
    settings are decided.
    """
    root = terraform_root(repo_path, repo_type)
    return _blocks(repo_path, root, _MODULE, recursive=False)


def of_type(resources: list[Resource], rtype: str) -> list[Resource]:
    """The resources of one type, in file order."""
    return [r for r in resources if r.type == rtype]


def dead_letter_queue_names(resources: list[Resource]) -> set[str]:
    """Names of the queues some redrive policy dead-letters into."""
    names: set[str] = set()
    for queue in of_type(resources, "aws_sqs_queue"):
        names.update(
            re.findall(
                r"deadLetterTargetArn\s*[=:]\s*aws_sqs_queue\.([\w-]+)\.arn", queue.body
            )
        )
    return names


_VARIABLE = re.compile(r'^\s*variable\s+"(?P<name>[\w-]+)"\s*\{', re.M)


def variable_defaults(repo_path: Path, repo_type: str = "") -> dict[str, str]:
    """The ``default = ...`` of every ``variable`` block in the Terraform root.

    A rule that compares two numbers in the stack reads them as they are
    written, and they are usually written as ``var.something``. A
    variable's default is the value the stack applies with unless
    ``terraform.tfvars`` overrides it, and tfvars is not in the
    repository, so the default is the only value a check can see.

    Returns the raw right-hand side, unparsed: the caller decides what
    counts as a number.
    """
    defaults: dict[str, str] = {}
    for tf in _tf_files(terraform_root(repo_path, repo_type), recursive=False):
        try:
            text = _strip_comments(tf.read_text(errors="replace"))
        except OSError:
            continue
        for m in _VARIABLE.finditer(text):
            start = m.end() - 1
            body = text[start + 1 : _block_end(text, start)]
            default = re.search(r"(?m)^\s*default\s*=\s*(.+?)\s*$", body)
            if default:
                defaults[m.group("name")] = default.group(1)
    return defaults
