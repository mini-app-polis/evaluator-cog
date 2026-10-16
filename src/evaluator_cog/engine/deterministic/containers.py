"""Container and platform-descriptor rule checks (CD-017, CD-021..CD-024).

These rules all ask the same underlying question from different
angles: *is this service's runtime defined in version control, or is it
defined by whatever the deployment dashboard happens to hold today?* A
``railway.json`` that omits ``startCommand``, a ``Dockerfile`` the
platform never selects, a base image pinned by a mutable tag — each of
these silently hands control of the running artefact to something
outside the repository.

Two shaping decisions are worth stating up front, because they are easy
to get wrong in the other direction:

*CD-022 and CD-023 are conditional on a Dockerfile existing.* Where
there is none, they return ``[]`` and emit nothing at all. The absence
of an image definition is CD-021's subject and CD-021's alone; reporting
it from three rules at once would triple the noise and obscure which
rule is actually open. A repo with no Dockerfile should show exactly one
container finding, not three.

*CD-021 asks only of Railway-hosted repositories.* Its subject is a
repository that carries a Railway descriptor — ``railway.json``,
``railway.toml`` or Infrastructure-as-Code under ``.railway/``. A pipeline
cog on Lambda deploys a zip onto a runtime its infra module pins, so it
has no image to define and the check stays silent there, as CD-022 and
CD-023 stay silent without a Dockerfile.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)
from evaluator_cog.engine.deterministic._terraform import (
    module_calls,
    of_type,
    terraform_resources,
)

_DIMENSION = "cd_readiness"

# Keys that state a memory ceiling, after normalisation (lowercased with
# separators removed). CD-024 accepts any spelling that states a ceiling.
_MEMORY_KEY_TOKENS = ("memory", "ram")
_CPU_KEY_TOKENS = ("cpu", "vcpu")


def _rel(path: Path, repo_path: Path) -> str:
    """Repo-relative display path that is stable across runs.

    ``relative_to`` raises when ``path`` is not below ``repo_path``.

    The fallback used to be the absolute path, which begins with the
    run's temporary download directory. That is not merely ugly: the
    directory is different on every run, so a finding carrying one can
    never be recognised as a repeat of itself. It defeats duplicate
    suppression on the way in and would write a fresh row on every run
    behind a server-side idempotency guard. The last two components say
    everything a reader needs and say the same thing every time.
    """
    try:
        return str(path.relative_to(repo_path))
    except ValueError:
        pass
    parent = path.parent.name
    return f"{parent}/{path.name}" if parent else path.name


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON descriptor. Returns ``(data, error)``; never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(raw, dict):
        return None, "top-level value is not a JSON object"
    return raw, None


def _load_toml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a TOML descriptor. Returns ``(data, error)``; never raises."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc)
    return raw, None


def _load_platform_descriptor(
    root: Path,
) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    """Load ``railway.json`` or ``railway.toml`` from ``root``.

    JSON is preferred when both exist — it is the form the platform
    itself writes back. Returns ``(path, data, error)``. ``path`` is
    None only when neither file is present; ``data`` is None when the
    file exists but could not be parsed, and ``error`` then says why.
    """
    json_path = root / "railway.json"
    if json_path.is_file():
        data, err = _load_json(json_path)
        return json_path, data, err
    toml_path = root / "railway.toml"
    if toml_path.is_file():
        data, err = _load_toml(toml_path)
        return toml_path, data, err
    return None, None, None


def _dockerfile_instructions(text: str) -> list[tuple[int, str, str]]:
    """Tokenise a Dockerfile into ``(line_no, INSTRUCTION, args)`` triples.

    Dockerfile syntax has three wrinkles that a naive ``startswith``
    scan gets wrong, and all three occur in real images:

    * **Line continuations.** ``FROM \\`` on one line and the image on
      the next is legal. The logical instruction is reported at the line
      where it *starts*, which is the line a reader will go fix.
    * **Comments.** ``#`` lines are skipped, including comment lines that
      appear in the middle of a continuation, which Docker also skips.
    * **Case.** ``from``, ``From`` and ``FROM`` are all valid, so the
      instruction keyword is upper-cased before comparison.

    Blank lines and lines that carry no keyword are dropped.
    """
    out: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    total = len(lines)
    i = 0
    while i < total:
        current = lines[i]
        if not current.strip() or current.lstrip().startswith("#"):
            i += 1
            continue
        start_line = i + 1
        buffer = ""
        while True:
            piece = lines[i].rstrip()
            if piece.endswith("\\"):
                buffer += piece[:-1] + " "
                i += 1
                while i < total and (
                    not lines[i].strip() or lines[i].lstrip().startswith("#")
                ):
                    i += 1
                if i >= total:
                    break
                continue
            buffer += piece
            i += 1
            break
        tokens = buffer.split(None, 1)
        if not tokens:
            continue
        args = tokens[1].strip() if len(tokens) > 1 else ""
        out.append((start_line, tokens[0].upper(), args))
    return out


def _from_image(args: str) -> str:
    """Extract the image reference from the argument text of a FROM line.

    ``FROM --platform=linux/amd64 python:3.11 AS builder`` must yield
    ``python:3.11``: leading ``--flag`` tokens are skipped and the
    trailing ``AS <name>`` alias is not part of the reference.
    """
    tokens = args.split()
    index = 0
    while index < len(tokens) and tokens[index].startswith("--"):
        index += 1
    return tokens[index] if index < len(tokens) else ""


def _read_dockerfile(repo_path: Path) -> tuple[Path | None, str | None]:
    """Return ``(path, text)`` for a root Dockerfile, or ``(None, None)``.

    ``(path, None)`` means the file exists but could not be decoded —
    callers treat that as "no readable subject" and stay silent, for the
    same reason they stay silent when the file is absent.
    """
    path = repo_path / "Dockerfile"
    if not path.is_file():
        return None, None
    try:
        return path, path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path, None


def _walk_keys(node: Any) -> list[tuple[str, Any]]:
    """Flatten a parsed descriptor into ``(key, value)`` pairs, recursively.

    CD-024 accepts "the platform's native limit keys or a documented
    equivalent", and the descriptor nests those keys differently
    depending on whether they sit under ``deploy``, ``resources`` or a
    service block. Walking every key means the check tests whether a
    ceiling is *stated*, not where the author chose to state it.
    """
    pairs: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            pairs.append((str(key), value))
            pairs.extend(_walk_keys(value))
    elif isinstance(node, list):
        for item in node:
            pairs.extend(_walk_keys(item))
    return pairs


def _states_limit(data: dict[str, Any], tokens: tuple[str, ...]) -> bool:
    """True if any key naming one of ``tokens`` carries a non-null value."""
    for key, value in _walk_keys(data):
        normalised = key.lower().replace("_", "").replace("-", "")
        if any(token in normalised for token in tokens) and value is not None:
            return True
    return False


#: Railway Infrastructure-as-Code authoring files, one of which a repository
#: keeps. Only TypeScript is generally available; Python and Go are beta.
_RAILWAY_IAC_FILES = ("railway.ts", "railway.py", "railway.go")

#: ``builder: "DOCKERFILE"`` in TypeScript or Go, ``builder="DOCKERFILE"`` in
#: Python. The file is code and is not evaluated here, so this reads the one
#: setting the rule asks about rather than the whole graph.
_IAC_DOCKERFILE_BUILDER = re.compile(
    r"""["']?builder["']?\s*[:=]\s*["']DOCKERFILE["']"""
)


def _railway_iac_file(repo_path: Path) -> Path | None:
    """The repository's ``.railway/railway.{ts,py,go}``, if it has one."""
    for name in _RAILWAY_IAC_FILES:
        candidate = repo_path / ".railway" / name
        if candidate.is_file():
            return candidate
    return None


#: Settings CD-017 and CD-024 read from an IaC file. TypeScript and Go write
#: ``key: value``, Python ``key=value``; Python may also spell a key in snake
#: case. Each pattern matches every occurrence, since one file can declare
#: several services.
_IAC_RESTART_POLICY = re.compile(
    r"""\brestart_?policy_?type["']?\s*[:=]\s*["']([A-Za-z_]+)["']""",
    re.IGNORECASE,
)
_IAC_MAX_RETRIES = re.compile(
    r"""\brestart_?policy_?max_?retries["']?\s*[:=]\s*([^\s,})\]]+)""",
    re.IGNORECASE,
)
_IAC_START_COMMAND = re.compile(
    r"""\b(?:startCommand|start_command|start|command)["']?\s*[:=]\s*(["'`])(.*?)\1""",
    re.DOTALL,
)


def _iac_limit(token: str) -> re.Pattern[str]:
    """A key naming ``token`` (``memoryBytes``, ``cpu``) with a non-null value."""
    return re.compile(
        r"\b[A-Za-z_]*" + token + r"""[A-Za-z_]*["']?\s*[:=]\s*"""
        r"(?!null\b|None\b|undefined\b)[^\s,})\]]",
        re.IGNORECASE,
    )


_IAC_MEMORY_LIMIT = _iac_limit("memory")
_IAC_CPU_LIMIT = _iac_limit("cpu")


def _read_iac(repo_path: Path) -> tuple[Path | None, str | None]:
    """``(path, code)`` for the repository's IaC file, comments removed.

    Whole-line comments are dropped (``//``, ``#``, and the ``*`` lines of a
    block comment) so a setting named in prose — "the restart policy is
    ON_FAILURE" in a header comment — is not read as declared. ``(path,
    None)`` means the file exists but could not be read.
    """
    path = _railway_iac_file(repo_path)
    if path is None:
        return None, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path, None
    code = "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith(("//", "#", "*", "/*"))
    )
    return path, code


def _dockerfile_sets_start(repo_path: Path) -> bool:
    """True if the root Dockerfile's final stage has a CMD or ENTRYPOINT."""
    _path, text = _read_dockerfile(repo_path)
    if text is None:
        return False
    instructions = _dockerfile_instructions(text)
    final_from = max(
        (i for i, (_n, ins, _a) in enumerate(instructions) if ins == "FROM"),
        default=None,
    )
    if final_from is None:
        return False
    return any(
        ins in ("CMD", "ENTRYPOINT") and args.strip()
        for _n, ins, args in instructions[final_from + 1 :]
    )


def check_cd_017(repo_path: Path) -> list[Finding]:
    """CD-017: the Railway restart policy is version-controlled and correct.

    A restart policy that lives only in the dashboard is invisible to
    review and lost on service recreation, so condition (1) treats a
    missing or malformed descriptor as terminal — there is nothing to
    inspect and the remaining conditions would each restate the same
    fact, so the check returns after the first finding.

    Conditions (2) and (3) encode the policy itself: ``ON_FAILURE`` with
    at least ten retries. ``NEVER`` and ``ALWAYS`` are both wrong in
    different directions — one gives up on a transient fault, the other
    hides a crash loop — so both are flagged with the value named.

    Condition (4) is the subtle one. A descriptor that omits the start
    command leaves the effective start command dashboard-owned, which
    defeats the purpose of committing the file in the first place: the
    deploy still is not reproducible from the repository. Where the
    descriptor selects the root Dockerfile, a CMD or ENTRYPOINT in its
    final stage is the start command, also in version control, and
    satisfies the condition — declaring it in both places is how two
    of them drift.

    The descriptor is any of ``railway.json``, ``railway.toml`` or
    Infrastructure as Code in ``.railway/``. Railway stops reading the
    first two on 2026-12-01, and a repository that migrated used to fail
    here as having no restart policy at all; which file the policy lives
    in was never the rule's question. A repository holding both is part
    way through that migration, and ``.railway/`` is what it is moving
    to, so that is what is read.
    """
    iac, code = _read_iac(repo_path)
    if iac is not None:
        return _check_cd_017_iac(repo_path, iac, code)

    CHECK_ID = "CD-017"
    findings: list[Finding] = []

    target, data, error = _load_platform_descriptor(repo_path)

    if target is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                "No railway.json or railway.toml found, and no "
                ".railway/railway.{ts,py,go} — the restart policy is "
                "therefore dashboard-owned and not version-controlled.",
                "Declare the service in .railway/railway.ts (Railway stops "
                "reading railway.json on 2026-12-01) with restartPolicyType "
                '"ON_FAILURE", restartPolicyMaxRetries of at least 10, and an '
                "explicit start command.",
            )
        )
        return findings

    rel = _rel(target, repo_path)
    if data is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel} does not parse ({error}) — the restart policy cannot "
                f"be verified and the platform will ignore the file.",
                f"Fix the syntax in {rel} so the deploy block is parseable, "
                f"then re-verify the restart policy settings.",
            )
        )
        return findings

    deploy = data.get("deploy")
    if not isinstance(deploy, dict):
        deploy = {}

    policy = deploy.get("restartPolicyType")
    if policy != "ON_FAILURE":
        shown = "missing" if "restartPolicyType" not in deploy else repr(policy)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f'{rel}: deploy.restartPolicyType is {shown}, expected "ON_FAILURE".',
                'Set deploy.restartPolicyType to "ON_FAILURE" in railway.json '
                "so transient faults restart but a crash loop stays visible.",
            )
        )

    retries = deploy.get("restartPolicyMaxRetries")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 10:
        shown = "missing" if "restartPolicyMaxRetries" not in deploy else repr(retries)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}: deploy.restartPolicyMaxRetries is {shown}, expected an "
                f"integer of at least 10.",
                "Set deploy.restartPolicyMaxRetries to 10 or more in "
                "railway.json so a restarting service survives a short "
                "dependency outage.",
            )
        )

    start_command = deploy.get("startCommand")
    has_start = isinstance(start_command, str) and bool(start_command.strip())
    build = data.get("build")
    builder = build.get("builder") if isinstance(build, dict) else None
    selects_dockerfile = isinstance(builder, str) and builder.upper() == "DOCKERFILE"
    if not has_start and not (selects_dockerfile and _dockerfile_sets_start(repo_path)):
        shown = "missing" if "startCommand" not in deploy else repr(start_command)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}: deploy.startCommand is {shown} — the effective start "
                f"command is left dashboard-owned, which defeats the purpose of "
                f"committing railway.json.",
                "Add an explicit deploy.startCommand to railway.json, or a CMD "
                "to the root Dockerfile it selects, so the process the service "
                "runs is defined in version control.",
            )
        )

    return findings


def _check_cd_017_iac(repo_path: Path, iac: Path, code: str | None) -> list[Finding]:
    """CD-017 read from ``.railway/railway.{ts,py,go}``.

    The file is code and is not evaluated: the settings are matched as
    text, as CD-021 matches the builder. One file may declare several
    services, so every restart policy it states must be ``ON_FAILURE``
    and every retry count at least 10 — a single compliant service does
    not cover a sibling left on ``NEVER``. A retry count that is not a
    literal integer (a constant, an expression) cannot be read and is
    reported as such rather than guessed at.
    """
    CHECK_ID = "CD-017"
    findings: list[Finding] = []
    rel = _rel(iac, repo_path)

    if code is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel} could not be read — the restart policy cannot be verified.",
                f"Make {rel} readable UTF-8 text so its deploy settings can be "
                f"checked.",
            )
        )
        return findings

    policies = _IAC_RESTART_POLICY.findall(code)
    wrong = sorted({p for p in policies if p != "ON_FAILURE"})
    if not policies or wrong:
        shown = "not set" if not policies else ", ".join(repr(p) for p in wrong)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f'{rel}: restartPolicyType is {shown}, expected "ON_FAILURE".',
                f'Set restartPolicyType: "ON_FAILURE" in the deploy block of '
                f"every service in {rel} so transient faults restart but a "
                f"crash loop stays visible.",
            )
        )

    retries = _IAC_MAX_RETRIES.findall(code)
    low = sorted({r for r in retries if not r.isdigit() or int(r) < 10})
    if not retries or low:
        shown = "not set" if not retries else ", ".join(low)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}: restartPolicyMaxRetries is {shown}, expected a literal "
                f"integer of at least 10.",
                f"Set restartPolicyMaxRetries to 10 or more, as a number, in "
                f"the deploy block of every service in {rel}.",
            )
        )

    has_start = any(cmd.strip() for _q, cmd in _IAC_START_COMMAND.findall(code))
    selects_dockerfile = bool(_IAC_DOCKERFILE_BUILDER.search(code))
    if not has_start and not (selects_dockerfile and _dockerfile_sets_start(repo_path)):
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel} declares no start command, and no CMD in a root "
                f"Dockerfile it selects — the effective start command is left "
                f"dashboard-owned.",
                f"Declare startCommand in {rel}, or select the root Dockerfile "
                f'(builder: "DOCKERFILE") and give its final stage a CMD — '
                f"one of the two, not both.",
            )
        )

    return findings


def check_cd_021(repo_path: Path) -> list[Finding]:
    """CD-021: a Railway-hosted service defines its runtime image.

    The subject is a repository Railway builds: one carrying a Railway
    descriptor — ``railway.json`` or ``railway.toml``, or Infrastructure as
    Code in ``.railway/``, which replaces them (config as code stops being
    read on 2026-12-01). Anything else — a Lambda cog, a library, a static
    site — has no Railway build to define, and the check stays silent.

    Two conditions, each its own finding:

    *A Dockerfile at the repository root.* Without one the builder infers
    the base image, interpreter patch and system packages at build time,
    and none of them is recorded anywhere a reader can see.

    *The descriptor selects it.* A Dockerfile the platform does not use is
    not a runtime definition. ``railway.json`` / ``railway.toml`` must set
    ``build.builder`` to ``DOCKERFILE``; an IaC file must say
    ``builder: "DOCKERFILE"``. Railway would pick up a root Dockerfile on
    its own, but then the choice lives in the platform's default rather
    than in the repository, which is the thing this rule exists to move.

    The second condition is only asked once the first holds: without a
    Dockerfile, naming the builder too would report one gap twice.
    """
    CHECK_ID = "CD-021"
    findings: list[Finding] = []

    descriptor, data, _error = _load_platform_descriptor(repo_path)
    iac = _railway_iac_file(repo_path)
    source = iac or descriptor
    if source is None:
        return findings
    rel = _rel(source, repo_path)

    dockerfile, _text = _read_dockerfile(repo_path)
    if dockerfile is None:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"This Railway-hosted service ({rel}) has no Dockerfile at the "
                f"repository root, so its base image, interpreter patch and "
                f"system packages are inferred by the builder at build time "
                f"and recorded nowhere.",
                "Add a root Dockerfile with digest-pinned base images (CD-022) "
                "and a non-root final stage (CD-023), and select it in the "
                "Railway descriptor.",
            )
        )
        return findings

    if iac is not None:
        try:
            selects = bool(
                _IAC_DOCKERFILE_BUILDER.search(
                    iac.read_text(encoding="utf-8", errors="replace")
                )
            )
        except OSError:
            selects = False
        where = f'{rel} does not set builder: "DOCKERFILE"'
    else:
        build = (data or {}).get("build")
        builder = build.get("builder") if isinstance(build, dict) else None
        selects = isinstance(builder, str) and builder.upper() == "DOCKERFILE"
        shown = "missing" if builder is None else repr(builder)
        where = f'{rel}: build.builder is {shown}, not "DOCKERFILE"'

    if not selects:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{where} — the Dockerfile is there, but which builder runs is "
                f"left to the platform's default rather than declared.",
                f"Select the Dockerfile explicitly in {rel}: build.builder "
                f'"DOCKERFILE" in railway.json, or builder: "DOCKERFILE" in '
                f".railway/railway.ts.",
            )
        )
    return findings


def check_cd_022(repo_path: Path) -> list[Finding]:
    """CD-022: base images are pinned by digest, not by tag.

    A tag is a mutable pointer. ``python:3.11-slim`` today and
    ``python:3.11-slim`` next month are different bytes, so a build that
    passed review is not the build that ships, and a rollback does not
    restore what was running. Only ``@sha256:`` names an immutable
    image.

    Two shaping decisions follow from the catalog notes:

    *Absent Dockerfile means no subject.* Condition (1) returns ``[]``
    with no finding at all. A repository without an image definition has
    a CD-021 gap, not a CD-022 violation, and emitting one here would
    report that gap under the wrong rule.

    *Every unpinned FROM is reported with its line number.* The common
    real case is a multi-stage build that pins the builder stage and
    leaves the runtime stage on a floating tag, or vice versa. Naming
    only the file would leave the reader to find which of four ``FROM``
    lines is at fault, so each offending stage is reported separately
    with the line it sits on.
    """
    CHECK_ID = "CD-022"
    findings: list[Finding] = []

    dockerfile, text = _read_dockerfile(repo_path)
    if dockerfile is None or text is None:
        return findings

    rel = _rel(dockerfile, repo_path)
    for line_no, instruction, args in _dockerfile_instructions(text):
        if instruction != "FROM":
            continue
        image = _from_image(args)
        if "@sha256:" in image:
            continue
        shown = image or "(unparseable image reference)"
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}:{line_no}: FROM {shown} is not pinned by digest — a tag "
                f"is a mutable pointer, so this stage does not build "
                f"reproducibly.",
                f"Replace the reference on {rel} line {line_no} with a digest "
                f"pin of the form image@sha256:<64-hex-digest>, resolved with "
                f"`docker buildx imagetools inspect {shown}`.",
            )
        )
    return findings


def check_cd_023(repo_path: Path) -> list[Finding]:
    """CD-023: the final image stage runs as a non-root user.

    The trap this check exists to avoid is that ``USER`` does not carry
    across stages. A Dockerfile that drops privileges in its builder
    stage and then starts a fresh ``FROM`` for the runtime stage ships a
    container running as root, even though the word ``USER`` appears in
    the file. A naive whole-file substring scan passes that Dockerfile;
    this check fails it.

    So the final stage is resolved first — the last ``FROM`` in the
    file, with ``AS <name>`` aliases parsed off so a stage name is never
    mistaken for part of the image reference — and only instructions
    after that point are considered. Within the final stage the *last*
    ``USER`` wins, since a later instruction overrides an earlier one,
    and ``root`` or ``0`` (with any ``:group`` suffix stripped) counts as
    no drop at all.

    As with CD-022, a repository with no Dockerfile has no subject here
    and the check stays silent; that absence belongs to CD-021.
    """
    CHECK_ID = "CD-023"
    findings: list[Finding] = []

    dockerfile, text = _read_dockerfile(repo_path)
    if dockerfile is None or text is None:
        return findings

    rel = _rel(dockerfile, repo_path)
    instructions = _dockerfile_instructions(text)

    final_from_index: int | None = None
    for index, (_line_no, instruction, _args) in enumerate(instructions):
        if instruction == "FROM":
            final_from_index = index
    if final_from_index is None:
        return findings

    final_from_line = instructions[final_from_index][0]
    effective: tuple[int, str] | None = None
    for line_no, instruction, args in instructions[final_from_index + 1 :]:
        if instruction == "USER" and args.strip():
            effective = (line_no, args.strip())

    if effective is None:
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}: the final build stage (FROM at line {final_from_line}) "
                f"contains no USER instruction, so the image runs as root. A "
                f"USER set in an earlier stage does not carry forward.",
                f"Add a non-root USER instruction to the final stage of {rel}, "
                f"after line {final_from_line} — e.g. create an unprivileged "
                f"account with `RUN useradd -r -u 10001 appuser` and then "
                f"`USER appuser`.",
            )
        )
        return findings

    user_line, user_arg = effective
    account = user_arg.split(":", 1)[0].strip()
    if account in ("root", "0"):
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}:{user_line}: the final build stage sets USER "
                f"{user_arg} — the image runs with root privileges.",
                f"Change USER on {rel} line {user_line} to an unprivileged "
                f"account created in the final stage, e.g. "
                f"`RUN useradd -r -u 10001 appuser` followed by `USER appuser`.",
            )
        )
    return findings


def _check_cd_024_infrastructure(repo_path: Path) -> list[Finding]:
    """CD-024 for the pipeline cogs, in mini-app-polis/infra where they are declared.

    A Lambda has no CPU knob — CPU is allocated in proportion to memory —
    and its ceiling on a runaway is the timeout, so those are the two
    limits a function states. Left unset they are 128 MB and 3 seconds,
    defaults nobody chose. Two places must agree: the functions declared
    (in the cog module) set both, and every call to that module gives both
    a value for its cog.
    """
    findings: list[Finding] = []
    resources = terraform_resources(repo_path, "infrastructure") or []
    for fn in of_type(resources, "aws_lambda_function"):
        missing = [k for k in ("memory_size", "timeout") if not fn.attr(k)]
        if missing:
            findings.append(
                _finding(
                    "CD-024",
                    "WARN",
                    _DIMENSION,
                    f"aws_lambda_function.{fn.name} in {fn.file} does not set "
                    f"{' or '.join(missing)}.",
                    "Set memory_size and timeout explicitly; the defaults "
                    "(128 MB, 3 s) were chosen by no one.",
                )
            )
    for call in module_calls(repo_path, "infrastructure"):
        if "cog-worker" not in (call.attr("source") or ""):
            continue
        missing = [k for k in ("memory_mb", "timeout_seconds") if not call.attr(k)]
        if missing:
            findings.append(
                _finding(
                    "CD-024",
                    "WARN",
                    _DIMENSION,
                    f'module "{call.name}" in {call.file} does not set '
                    f"{' or '.join(missing)}.",
                    "Give every cog its memory and timeout explicitly, sized "
                    "against its own slowest run.",
                )
            )
    return findings


def check_cd_024(
    repo_path: Path,
    repo_type: str = "",
) -> list[Finding]:
    """CD-024: deployable services declare memory and CPU limits.

    Pipeline cogs run on Lambda (ADR-009), and their limits are declared in
    mini-app-polis/infra (ADR-010); ``_check_cd_024_infrastructure`` reads
    them there. Everything else is read from its Railway descriptor, below.

    An unbounded service is a noisy neighbour: a leak or a runaway query
    consumes whatever the host has rather than being killed and
    restarted under a policy someone chose. Declaring a ceiling turns an
    unbounded degradation into a bounded, observable restart.

    The check is deliberately spelling-agnostic. Railway, its
    descriptors and the team's own documented equivalents all name these
    ceilings differently — ``memoryLimit``, ``memoryGB``, ``numCpus``,
    ``cpuLimit`` — and which spelling is used is not the point. Any key
    naming memory (or RAM) and any key naming CPU, carrying a non-null
    value anywhere in the descriptor, satisfies the rule; the check is
    that a ceiling is stated at all.

    The descriptor may be Infrastructure as Code in ``.railway/``, which
    replaces ``railway.json`` / ``railway.toml`` from 2026-12-01 and is
    preferred when both exist. It is code, so the limits are matched as
    text — any setting naming memory, and any naming CPU, with a value
    that is not null — rather than evaluated.

    Unlike CD-022 and CD-023, a missing descriptor is a failure here
    rather than a silent skip: the catalog notes say so explicitly, and
    a service with no platform descriptor has by definition declared no
    limits.
    """
    if repo_type == "infrastructure":
        return _check_cd_024_infrastructure(repo_path)
    if repo_type == "pipeline-cog":
        # Declared in mini-app-polis/infra and checked there (ADR-010).
        return []

    CHECK_ID = "CD-024"
    findings: list[Finding] = []

    iac, code = _read_iac(repo_path)
    if iac is not None:
        rel = _rel(iac, repo_path)
        stated = {
            "memory": code is not None and bool(_IAC_MEMORY_LIMIT.search(code)),
            "CPU": code is not None and bool(_IAC_CPU_LIMIT.search(code)),
        }
        for kind, ok in stated.items():
            if not ok:
                findings.append(
                    _finding(
                        CHECK_ID,
                        "WARN",
                        _DIMENSION,
                        f"{rel} declares no {kind} limit — no setting naming a "
                        f"{kind} ceiling is present, or its value is null.",
                        f"Declare the ceiling in {rel}: deploy.limitOverride."
                        f"containers.{'memoryBytes' if kind == 'memory' else 'cpu'}"
                        f" on each service.",
                    )
                )
        return findings

    descriptor, data, error = _load_platform_descriptor(repo_path)

    if descriptor is None:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No railway.json or railway.toml at the repository root, and "
                "no .railway/railway.{ts,py,go}, so this service declares no "
                "memory or CPU ceiling.",
                "Declare the service in .railway/railway.ts with "
                "deploy.limitOverride.containers.cpu and memoryBytes set.",
            )
        )
        return findings

    rel = _rel(descriptor, repo_path)
    if data is None:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{rel} does not parse ({error}), so declared memory and CPU "
                f"limits cannot be read.",
                f"Fix the syntax error in {rel} so the resource limit keys can "
                f"be parsed and verified.",
            )
        )
        return findings

    if not _states_limit(data, _MEMORY_KEY_TOKENS):
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{rel} declares no memory limit — no key naming a memory "
                f"ceiling is present, or its value is null.",
                f"Declare an explicit memory ceiling in {rel} (for example a "
                f"deploy.memoryLimit key) so a leak is bounded by a restart "
                f"rather than by the host.",
            )
        )

    if not _states_limit(data, _CPU_KEY_TOKENS):
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                f"{rel} declares no CPU limit — no key naming a CPU ceiling is "
                f"present, or its value is null.",
                f"Declare an explicit CPU ceiling in {rel} (for example a "
                f"deploy.cpuLimit or numCpus key) so a runaway process cannot "
                f"starve co-located services.",
            )
        )

    return findings
