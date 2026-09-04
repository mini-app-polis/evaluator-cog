"""Container and platform-descriptor rule checks (CD-017, CD-021..CD-024).

These five rules all ask the same underlying question from different
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

*CD-021 is a `gap`, not a requirement.* It is checkable so that the
fleet-wide count of services without an explicit runtime image can be
measured, and it emits a finding when one is absent because that finding
*is* the measurement. Its severity is WARN and its wording records an
observed gap rather than asserting a failure.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)

_DIMENSION = "cd_readiness"

# Keys that state a memory ceiling, after normalisation (lowercased with
# separators removed). CD-024 accepts any spelling that states a ceiling.
_MEMORY_KEY_TOKENS = ("memory", "ram")
_CPU_KEY_TOKENS = ("cpu", "vcpu")


def _rel(path: Path, repo_path: Path) -> str:
    """Best-effort repo-relative display path.

    Monorepo service roots are not always below ``repo_path``, so
    ``relative_to`` can legitimately raise. Findings are prose read by
    humans, not machine paths, so falling back to the full string is
    fine and never worth an exception.
    """
    try:
        return str(path.relative_to(repo_path))
    except ValueError:
        return str(path)


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


def check_cd_017(
    repo_path: Path,
    monorepo_path: Path | None = None,
) -> list[Finding]:
    """CD-017: the Railway restart policy is version-controlled and correct.

    A restart policy that lives only in the dashboard is invisible to
    review and lost on service recreation, so condition (1) treats a
    missing or malformed ``railway.json`` as terminal — there is nothing
    to inspect and the remaining conditions would each restate the same
    fact, so the check returns after the first finding.

    Conditions (2) and (3) encode the policy itself: ``ON_FAILURE`` with
    at least ten retries. ``NEVER`` and ``ALWAYS`` are both wrong in
    different directions — one gives up on a transient fault, the other
    hides a crash loop — so both are flagged with the value named.

    Condition (4) is the subtle one. A ``railway.json`` that omits
    ``startCommand`` leaves the effective start command dashboard-owned,
    which defeats the purpose of committing the file in the first place:
    the deploy still is not reproducible from the repository.

    Monorepo services keep their descriptor beside the service rather
    than at the repository root, so ``monorepo_path`` is consulted first
    and the root is the fallback.

    ``railway.toml`` counts. Railway accepts either spelling and CD-024
    already reads both; this check looked only for ``railway.json`` and
    so reported deejaytools-com, whose descriptor is a railway.toml, for
    having no restart policy at all. Which file the policy lives in was
    never the rule's question.
    """
    CHECK_ID = "CD-017"
    findings: list[Finding] = []

    search_roots: list[Path] = []
    if monorepo_path is not None:
        search_roots.append(monorepo_path)
    search_roots.append(repo_path)

    target: Path | None = None
    data: dict[str, Any] | None = None
    error: str | None = None
    found_root: Path = repo_path
    for root in search_roots:
        candidate, candidate_data, candidate_error = _load_platform_descriptor(root)
        if candidate is not None:
            target, data, error = candidate, candidate_data, candidate_error
            found_root = root
            break

    if target is None:
        looked_in = ", ".join(_rel(r / "railway.json", repo_path) for r in search_roots)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"No railway.json or railway.toml found (looked at: "
                f"{looked_in}) — the restart policy is therefore "
                f"dashboard-owned and not version-controlled.",
                "Commit a railway.json (or railway.toml) with a deploy block "
                'setting restartPolicyType to "ON_FAILURE", '
                "restartPolicyMaxRetries to at least 10, and an explicit "
                "startCommand.",
            )
        )
        return findings

    # Relative to the root it was actually found under: a monorepo
    # service is evaluated at apps/<name>, and its descriptor may sit at
    # the repo root above that, where _rel against the service directory
    # gives up and prints an absolute container path.
    rel = _rel(target, found_root)
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
    if not isinstance(start_command, str) or not start_command.strip():
        shown = "missing" if "startCommand" not in deploy else repr(start_command)
        findings.append(
            _finding(
                CHECK_ID,
                "ERROR",
                _DIMENSION,
                f"{rel}: deploy.startCommand is {shown} — the effective start "
                f"command is left dashboard-owned, which defeats the purpose of "
                f"committing railway.json.",
                "Add an explicit deploy.startCommand to railway.json so the "
                "process the service runs is defined in version control.",
            )
        )

    return findings


def check_cd_021(repo_path: Path, monorepo_path: Path | None = None) -> list[Finding]:
    """CD-021: deployable services define their runtime image explicitly.

    This rule is catalogued as a ``gap``, not a requirement, and the
    distinction changes what the check is for. It is not asserting that
    every service must have a Dockerfile today; it exists so the number
    of services still relying on platform build auto-detection can be
    counted. The finding it emits *is* that measurement, which is why
    the severity is WARN and the wording records an observed state
    rather than pronouncing a failure.

    Passing requires two things together. A ``Dockerfile`` must exist at
    the repository root, *and* the platform descriptor must actually
    select the dockerfile builder — either by setting ``build.builder``
    to ``DOCKERFILE`` or by naming a ``dockerfilePath``. A Dockerfile
    the platform never reads is not a runtime definition; the image that
    actually ships still comes from auto-detection, and the committed
    file is documentation at best.
    """
    CHECK_ID = "CD-021"
    findings: list[Finding] = []

    dockerfile = repo_path / "Dockerfile"
    has_dockerfile = dockerfile.is_file()
    if not has_dockerfile and monorepo_path is not None:
        has_dockerfile = (monorepo_path / "Dockerfile").is_file()

    descriptor, data, _error = _load_platform_descriptor(repo_path)
    if descriptor is None and monorepo_path is not None:
        # Same reason as CD-024: the image definition governing a
        # monorepo service can live at the repo root, and "no descriptor"
        # is not true of the service just because it is not beside it.
        descriptor, data, _error = _load_platform_descriptor(monorepo_path)
    builder_selected = False
    if data is not None:
        build = data.get("build")
        if isinstance(build, dict):
            builder = build.get("builder")
            if isinstance(builder, str) and builder.strip().upper() == "DOCKERFILE":
                builder_selected = True
            dockerfile_path = build.get("dockerfilePath")
            if isinstance(dockerfile_path, str) and dockerfile_path.strip():
                builder_selected = True

    if has_dockerfile and builder_selected:
        return findings

    if not has_dockerfile and descriptor is None:
        observed = (
            "no Dockerfile at the repository root and no railway.json / "
            "railway.toml descriptor"
        )
    elif not has_dockerfile:
        observed = (
            f"no Dockerfile at the repository root (descriptor "
            f"{_rel(descriptor, repo_path)} present)"
            if descriptor is not None
            else "no Dockerfile at the repository root"
        )
    elif descriptor is None:
        observed = (
            "a Dockerfile is present but there is no railway.json / "
            "railway.toml to select the dockerfile builder"
        )
    else:
        observed = (
            f"a Dockerfile is present but {_rel(descriptor, repo_path)} does not "
            f"select the dockerfile builder, so the platform still "
            f"auto-detects the build"
        )

    findings.append(
        _finding(
            CHECK_ID,
            "WARN",
            _DIMENSION,
            f"Runtime image gap recorded: {observed}. The image this service "
            f"actually runs is therefore not defined in version control.",
            "Add a Dockerfile at the repository root and set build.builder to "
            '"DOCKERFILE" (or name a build.dockerfilePath) in railway.json so '
            "the platform builds from the committed image definition.",
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
    make the CD-021 measurement harder to read rather than easier.

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


def check_cd_024(repo_path: Path, monorepo_path: Path | None = None) -> list[Finding]:
    """CD-024: deployable services declare memory and CPU limits.

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

    Unlike CD-022 and CD-023, a missing descriptor is a failure here
    rather than a silent skip: the catalog notes say so explicitly, and
    a service with no platform descriptor has by definition declared no
    limits.
    """
    CHECK_ID = "CD-024"
    findings: list[Finding] = []

    descriptor, data, error = _load_platform_descriptor(repo_path)
    if descriptor is None and monorepo_path is not None:
        # A monorepo service is evaluated at apps/<name>; its platform
        # descriptor may be the one at the repo root that governs the
        # whole deploy. Reporting "no descriptor" for a service whose
        # descriptor is one directory up says nothing true.
        descriptor, data, error = _load_platform_descriptor(monorepo_path)

    if descriptor is None:
        findings.append(
            _finding(
                CHECK_ID,
                "WARN",
                _DIMENSION,
                "No railway.json or railway.toml at the repository root, so "
                "this service declares no memory or CPU ceiling.",
                "Add a railway.json (or railway.toml) at the repository root "
                "declaring explicit memory and CPU limits for the service.",
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
