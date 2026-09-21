"""A reader for the few Terraform facts the runtime rules need.

Pipeline cogs own their infrastructure in ``infra/*.tf`` (PIPE-016), so
the rules that describe that runtime — the queue, its dead-letter queue,
the function and the mapping between them — are read from there.

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


def infra_resources(repo_path: Path) -> list[Resource] | None:
    """Every resource declared in ``infra/*.tf``; None when there is no infra/.

    None and an empty list are different answers: a repository without
    ``infra/`` has not started owning its runtime, one with an empty
    ``infra/`` has started and declared nothing.
    """
    infra = repo_path / "infra"
    if not infra.is_dir():
        return None
    found: list[Resource] = []
    for tf in sorted(infra.glob("*.tf")):
        try:
            text = _strip_comments(tf.read_text(errors="replace"))
        except OSError:
            continue
        for m in _RESOURCE.finditer(text):
            start = m.end() - 1
            end = _block_end(text, start)
            found.append(
                Resource(
                    type=m.group("type"),
                    name=m.group("name"),
                    body=text[start + 1 : end],
                    file=f"infra/{tf.name}",
                )
            )
    return found


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
