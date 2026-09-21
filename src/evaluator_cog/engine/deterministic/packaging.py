"""Lockfile-discipline rule check (CD-020).

CD-020 polices the seam between what a repository *declares* and what
actually ships: a ``uv.lock`` that records the project's own version goes
stale the moment a release bumps it, and every subsequent install resolves
against a graph nobody released. The file-sourced version scheme (PY-017)
keeps the version out of the lock, so a release never touches it.

CD-016 (serve() wrapped in serve_with_retry) lived here until Prefect was
retired (ecosystem-standards ADR-009); pipeline cogs have no startup
registration to protect.

The check never raises. Every file read, parse and subprocess call is
guarded, and an unreadable or unparseable input degrades to "cannot
confirm" rather than to a traceback, because these functions run inside a
batch conformance sweep where one bad repository must not stop the run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)

_DIMENSION = "cd_readiness"

# `uv lock --check` resolves against the configured indexes and can block
# on a slow or unreachable network. The conformance sweep is a batch job,
# so the call is bounded and a breach of the bound is treated as "could
# not determine", never as a violation.
_UV_LOCK_CHECK_TIMEOUT_S = 60.0


# --- shared local helpers ----------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_toml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    text = _read_text(path)
    if text is None:
        return None, "unreadable"
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as exc:
        return None, str(exc)


# --- CD-016: serve() registration wrapped in serve_with_retry ---------------


# --- CD-020: the lockfile is released with the version it locks -------------


def _requirement_name(requirement: str) -> str:
    """The distribution name at the head of a PEP 508 requirement string."""
    text = requirement.strip()
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    return match.group(1) if match else ""


def _canonical_name(name: str) -> str:
    """PEP 503 normalisation, so 'common_python_utils' == 'common-python-utils'."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement_specifiers(requirement: str) -> list[tuple[str, str]]:
    """(operator, version) pairs from a requirement's version specifier.

    Environment markers are cut off first: the version in
    ``pkg; python_version >= "3.11"`` belongs to the marker, not to the
    distribution.
    """
    text = requirement.split(";", 1)[0]
    name = _requirement_name(text)
    tail = text[len(name) :]
    tail = re.sub(r"^\s*\[[^\]]*\]", "", tail)  # drop extras
    tail = re.sub(r"@.*$", "", tail)  # drop direct-reference URLs
    return [
        (op, version.strip())
        for op, version in re.findall(r"(===|==|>=|<=|~=|!=|<|>)\s*([^,\s]+)", tail)
    ]


def _normalise_version(value: str) -> str:
    """Strip the tag's 'v' prefix and a specifier's trailing wildcard."""
    return value.strip().lstrip("vV").rstrip(".*").strip()


def _source_ref(source: dict[str, Any]) -> tuple[str, str] | None:
    """The (kind, value) ref a [tool.uv.sources] entry pins to, if any."""
    for key in ("tag", "rev", "branch"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None


def _project_requirements(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(group label, requirement string) for every declared dependency."""
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    pairs: list[tuple[str, str]] = []

    deps = project.get("dependencies")
    if isinstance(deps, list):
        pairs.extend(("project.dependencies", str(item)) for item in deps)

    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group, items in optional.items():
            if isinstance(items, list):
                pairs.extend(
                    (f"optional-dependencies.{group}", str(item)) for item in items
                )
    return pairs


def _locked_version(repo_path: Path, package_name: str) -> str:
    """The version ``uv.lock`` records for one package, or 'unknown'."""
    data, _ = _load_toml(repo_path / "uv.lock")
    if not isinstance(data, dict):
        return "unknown"
    packages = data.get("package")
    if not isinstance(packages, list):
        return "unknown"
    wanted = _canonical_name(package_name)
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        if _canonical_name(str(entry.get("name", ""))) == wanted:
            version = entry.get("version")
            if isinstance(version, str):
                return version
    return "unknown"


def _project_lock_entry(
    lock_data: dict[str, Any], project_name: str
) -> dict[str, Any] | None:
    """The ``[[package]]`` entry ``uv.lock`` holds for the project itself.

    Matched on the PEP 503 name *and* a source that is the project
    directory, so a dependency that happens to share the name is never
    mistaken for the root.
    """
    packages = lock_data.get("package")
    if not isinstance(packages, list):
        return None
    wanted = _canonical_name(project_name)
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        if _canonical_name(str(entry.get("name", ""))) != wanted:
            continue
        source = entry.get("source")
        if isinstance(source, dict) and "." in (
            source.get("editable"),
            source.get("virtual"),
        ):
            return entry
    return None


def _check_lock_omits_project_version(
    repo_path: Path, data: dict[str, Any] | None
) -> list[Finding]:
    """CD-020 (1): a release cannot stale the lock.

    Under the file-sourced version scheme (PY-017) ``[project]`` declares
    ``dynamic = ["version"]``, uv records no version for the project
    itself, and a release writes the version file without touching
    ``uv.lock``. A lock whose root entry *does* carry a version goes a
    version behind the moment a release succeeds.

    ``.releaserc.json`` is deliberately not read. This clause used to
    require the release to relock and commit ``uv.lock`` — the exact
    mechanism PY-017 removed — which failed every conforming repository.
    PY-017 owns the release config; this reads the lock.
    """
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return []
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        return []

    lock_data, _ = _load_toml(repo_path / "uv.lock")
    if not isinstance(lock_data, dict):
        return []
    entry = _project_lock_entry(lock_data, name)
    if entry is None:
        # No root entry is clause (2)'s concern, not this one's.
        return []
    locked = entry.get("version")
    if not isinstance(locked, str) or not locked.strip():
        return []

    static = project.get("version")
    dynamic = project.get("dynamic")
    if isinstance(static, str) and static.strip():
        declared = f'pyproject.toml declares a static version = "{static}"'
    elif isinstance(dynamic, list) and "version" in dynamic:
        declared = (
            'pyproject.toml declares dynamic = ["version"], so the entry '
            "predates the switch and the lock was never regenerated"
        )
    else:
        declared = "pyproject.toml declares no version source"

    return [
        _finding(
            "CD-020",
            "ERROR",
            _DIMENSION,
            (
                f"uv.lock records {name} {locked} for the project itself "
                f"({declared}). A release writes the new version without "
                f"touching uv.lock, so this lock goes a version behind the "
                f"moment each release succeeds."
            ),
            (
                'Source the version from a committed file (dynamic = ["version"] '
                "with [tool.hatch.version] path, per PY-017), remove any static "
                "[project] version, and run 'uv lock' so the project entry no "
                "longer records a version. Do not add uv.lock to the release "
                "commit."
            ),
        )
    ]


def _check_lock_is_current(repo_path: Path) -> list[Finding]:
    """CD-020 (2): ``uv lock --check`` against the checked-out tree.

    Guarded twice over. The ``uv`` binary is looked up first and the
    sub-check is skipped in silence when it is absent, because a tool
    missing from the evaluator's own environment is not the target
    repository's violation. Nor is a non-zero exit on its own: only the
    lockfile-needs-updating message is read as staleness, because uv
    exits non-zero for environment failures too. The subprocess then
    runs under an explicit timeout — ``uv lock --check`` reaches the configured indexes and would
    otherwise be able to hang the entire conformance sweep on a network
    stall — and a timeout, like any other execution failure, is reported
    as nothing rather than as a violation.
    """
    findings: list[Finding] = []
    uv_binary = shutil.which("uv")
    if uv_binary is None:
        return findings

    try:
        result = subprocess.run(
            [uv_binary, "lock", "--check"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_UV_LOCK_CHECK_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return findings

    if result.returncode == 0:
        return findings

    # Non-zero is not the same as stale. `uv lock --check` exits 1 with
    # "The lockfile at `uv.lock` needs to be updated" when the lock is
    # genuinely out of date, and exits with other codes when it could
    # not run at all — no usable interpreter, an unreadable .venv, no
    # network to resolve a git dependency. Treating every failure as
    # staleness reported identity's lockfile as out of date when
    # `uv lock --check` on a clean copy of that same tree exits 0; what
    # actually failed was reading a stale .venv.
    #
    # So the definitive message is required, not merely a bad exit code.
    # A false negative here costs little — the other three clauses still
    # catch the drift structurally — while a false positive tells you to
    # regenerate a lockfile that is correct.
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    if "needs to be updated" not in combined:
        return findings

    data, _ = _load_toml(repo_path / "pyproject.toml")
    project = data.get("project") if isinstance(data, dict) else None
    project = project if isinstance(project, dict) else {}
    name = str(project.get("name") or repo_path.name)
    declared = project.get("version")
    locked = _locked_version(repo_path, name)

    # Under a dynamic version neither file records the project's version,
    # so there is no pair to report and the stale graph is the whole gap.
    if isinstance(declared, str) and declared and locked not in (declared, "unknown"):
        gap = (
            f"pyproject.toml declares {name} {declared} while uv.lock records {locked}"
        )
    else:
        gap = "the resolved dependency graph no longer matches the declaration"
    findings.append(
        _finding(
            "CD-020",
            "ERROR",
            _DIMENSION,
            (
                f"'uv lock --check' exits {result.returncode} — uv.lock is out "
                f"of date with pyproject.toml: {gap}. Installs from this tree "
                f"resolve against a lockfile that no longer matches the "
                f"declared dependencies."
            ),
            (
                "Run 'uv lock' and commit the regenerated uv.lock with the "
                "change that altered the dependencies."
            ),
        )
    )
    return findings


def _check_source_specifier_agreement(
    data: dict[str, Any],
    sources: dict[str, Any],
) -> list[Finding]:
    """CD-020 (3): a dependency's version specifier must match its source ref.

    ``uv`` resolves a ``[tool.uv.sources]`` dependency from the git ref,
    not from the specifier in the requirement string, so a requirement
    reading ``common-python-utils>=3.0`` beside ``rev = "v4.0.0"`` is a
    statement that is simply false in the built image — and the next
    reader believes it. A bare name with no specifier is the correct
    spelling and is never flagged: it says nothing that the source entry
    can contradict.
    """
    findings: list[Finding] = []
    requirements = _project_requirements(data)

    for source_name, source in sources.items():
        if not isinstance(source, dict):
            continue
        ref = _source_ref(source)
        if ref is None:
            continue
        ref_kind, ref_value = ref
        wanted = _canonical_name(source_name)

        for group, requirement in requirements:
            if _canonical_name(_requirement_name(requirement)) != wanted:
                continue
            for operator, version in _requirement_specifiers(requirement):
                if _normalise_version(version) == _normalise_version(ref_value):
                    continue
                findings.append(
                    _finding(
                        "CD-020",
                        "ERROR",
                        _DIMENSION,
                        (
                            f"pyproject.toml {group} pins '{requirement.strip()}' "
                            f"while [tool.uv.sources].{source_name} resolves "
                            f'{ref_kind} = "{ref_value}"; the specifier '
                            f"'{operator}{version}' names a different version "
                            f"from the one actually installed, so the declared "
                            f"dependency misdescribes the build."
                        ),
                        (
                            f"Drop the version specifier and declare "
                            f"'{source_name}' as a bare requirement, or move "
                            f"[tool.uv.sources].{source_name} to the tag the "
                            f"specifier names — the git ref is what uv "
                            f"installs, so the two must not disagree."
                        ),
                    )
                )
                break

    return findings


def _check_source_refs_are_tags(sources: dict[str, Any]) -> list[Finding]:
    """CD-020 (4): every git source must pin a version tag.

    A branch ref re-resolves on every lock, so two builds of the same
    commit can install different library code; a 40-character SHA pins
    reproducibly but is unreadable and invisible to the library's own
    release process, so a bump is impossible to review. Only a version
    tag — ``tag = "v4.0.0"``, or the same string held in ``rev`` — is both
    stable and legible.
    """
    findings: list[Finding] = []
    tag_shaped = re.compile(r"^v?\d+(\.\d+)*([.\-+A-Za-z0-9]*)$")
    sha_shaped = re.compile(r"^[0-9a-fA-F]{40}$")

    for source_name, source in sources.items():
        if not isinstance(source, dict) or "git" not in source:
            continue
        ref = _source_ref(source)

        if ref is None:
            reason = (
                "no tag, rev or branch is given, so it floats on the default branch"
            )
        else:
            ref_kind, ref_value = ref
            if ref_kind == "branch":
                reason = f'branch = "{ref_value}" re-resolves on every lock'
            elif sha_shaped.match(ref_value):
                reason = (
                    f'{ref_kind} = "{ref_value}" is a 40-character commit SHA, '
                    f"not a version tag"
                )
            elif ref_value.lower() in {"main", "master", "head"}:
                reason = (
                    f'{ref_kind} = "{ref_value}" names a moving branch, not a '
                    f"version tag"
                )
            elif not tag_shaped.match(ref_value):
                reason = f'{ref_kind} = "{ref_value}" is not a version tag'
            else:
                continue

        findings.append(
            _finding(
                "CD-020",
                "ERROR",
                _DIMENSION,
                (
                    f"[tool.uv.sources].{source_name} does not pin a version "
                    f"tag: {reason}. The locked dependency is then either "
                    f"irreproducible or unreviewable, and the lockfile stops "
                    f"describing a releasable state."
                ),
                (
                    f"Pin [tool.uv.sources].{source_name} to a released version "
                    f'tag (for example tag = "v4.0.0") and bump it deliberately, '
                    f"so each lock records a reviewable library release."
                ),
            )
        )
    return findings


def check_cd_020(repo_path: Path) -> list[Finding]:
    """CD-020: the lockfile is released with the version it locks.

    The failure this rule exists for is quiet. A release bumps the
    project's version; a ``uv.lock`` that records that version still
    names the previous one; the built image installs from the lock and
    runs code nobody released. Nothing fails loudly, so the drift is only
    visible to a check.

    Four independent conditions, each reported separately so a repository
    learns exactly which of them is still open:

    (1) ``uv.lock`` must not record the project's own version, so a
        release cannot stale it. The file-sourced dynamic version (PY-017)
        is what keeps it out; ``.releaserc.json`` is PY-017's to check.
    (2) ``uv lock --check`` must pass against the checked-out tree, with
        the finding naming the declared and locked versions where both
        exist rather than just asserting a gap.
    (3) A dependency carrying a version specifier must not name a version
        different from the ``[tool.uv.sources]`` ref that actually
        installs it. A bare name is correct and is not flagged.
    (4) Every git source must pin a version tag rather than a branch or a
        raw commit SHA.

    A repository with no ``uv.lock`` is exempt outright: it is not
    uv-managed, there is no lockfile to drift, and PY-001 is the rule that
    speaks to that instead. Returning ``[]`` here keeps this ERROR off
    repositories the rule was never written about.
    """
    CHECK_ID = "CD-020"
    findings: list[Finding] = []

    if not (repo_path / "uv.lock").is_file():
        return findings

    data, _ = _load_toml(repo_path / "pyproject.toml")

    findings.extend(_check_lock_omits_project_version(repo_path, data))
    findings.extend(_check_lock_is_current(repo_path))

    if not isinstance(data, dict):
        return findings

    tool = data.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    uv_config = tool.get("uv")
    uv_config = uv_config if isinstance(uv_config, dict) else {}
    sources = uv_config.get("sources")
    if not isinstance(sources, dict):
        return findings

    findings.extend(_check_source_specifier_agreement(data, sources))
    findings.extend(_check_source_refs_are_tags(sources))
    return findings
