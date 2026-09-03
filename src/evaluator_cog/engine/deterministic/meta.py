"""Catalog/meta rule checks (META-001..007) — validate evaluation.yaml itself."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_meta_release_pipeline_wired(repo_path: Path) -> list[Finding]:
    """META-001: Release automation for the standards repo is wired end-to-end."""
    CHECK_ID = "META-001"
    findings: list[Finding] = []
    workflows_dir = repo_path / ".github" / "workflows"
    workflow_blob = ""
    if workflows_dir.is_dir():
        for wf in list(workflows_dir.rglob("*.yml")) + list(
            workflows_dir.rglob("*.yaml")
        ):
            try:
                workflow_blob += "\n" + wf.read_text().lower()
            except OSError:
                continue

    has_sem_rel_wf = any(
        s in workflow_blob
        for s in (
            "semantic-release",
            "npx semantic-release",
            "semantic_release",
        )
    )
    has_rel_hook = (
        (repo_path / ".releaserc.json").exists()
        or (repo_path / ".releaserc.cjs").exists()
        or (repo_path / ".releaserc.yaml").exists()
    )
    pkg = repo_path / "package.json"
    pkg_ok = False
    if pkg.exists():
        try:
            import json as _json

            pdata = _json.loads(pkg.read_text())
            scripts = pdata.get("scripts") or {}
            dev = pdata.get("devDependencies") or {}
            deps = pdata.get("dependencies") or {}
            scripts_blob = str(scripts).lower()
            pkg_ok = "semantic-release" in scripts_blob or any(
                "semantic-release" in str(k).lower() for k in {**dev, **deps}
            )
        except Exception:
            pkg_ok = False

    push_to_main = "push:" in workflow_blob and "main" in workflow_blob

    if not has_sem_rel_wf:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "cd_readiness",
                "No GitHub Actions workflow references semantic-release.",
                "Add a workflow that executes semantic-release on the mainline branch.",
            )
        )
    if not has_rel_hook:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "cd_readiness",
                "Missing .releaserc.* configuration alongside semantic-release.",
                "Add .releaserc.json (or .releaserc.cjs / .yaml) describing branches and plugins.",
            )
        )
    if not pkg_ok:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "cd_readiness",
                "package.json lacks semantic-release wiring (script or dependency).",
                "Declare semantic-release in devDependencies and expose an npm script if required by the catalog.",
            )
        )
    if not push_to_main:
        findings.append(
            _finding(
                "META-001",
                "WARN",
                "cd_readiness",
                "No workflow appears to trigger on push to main.",
                "Ensure release automation runs when main updates (push trigger with main branch).",
            )
        )
    return findings


def check_meta_no_scattered_metadata(repo_path: Path) -> list[Finding]:
    """META-002: Version metadata is not scattered outside canonical files."""
    CHECK_ID = "META-002"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"
    if index_path.exists():
        try:
            text = index_path.read_text()
            if re.search(r"(?m)^version\s*:", text):
                findings.append(
                    _finding(
                        "META-002",
                        "WARN",
                        "standards_currency",
                        "index.yaml still declares a top-level version: field.",
                        "Remove version from index.yaml — package.json is the single version of record.",
                    )
                )
            if re.search(r"(?m)^updated\s*:", text):
                findings.append(
                    _finding(
                        "META-002",
                        "WARN",
                        "standards_currency",
                        "index.yaml still declares a top-level updated: field.",
                        "Remove updated metadata from index.yaml; rely on git history and package.json.",
                    )
                )
        except OSError as exc:
            findings.append(
                _finding(
                    "META-002",
                    "WARN",
                    "standards_currency",
                    f"index.yaml could not be read: {exc}",
                    "Fix permissions/encoding so META-002 can scan for scattered metadata.",
                )
            )

    for stray in ("VERSION.txt", "VERSION", "version.txt"):
        candidate = repo_path / stray
        if candidate.is_file():
            findings.append(
                _finding(
                    "META-002",
                    "WARN",
                    "standards_currency",
                    f"Stray plaintext version file exists at repo root ({stray}).",
                    "Delete ad-hoc version files — package.json must remain canonical.",
                )
            )
            break
    return findings


_CANONICAL_ENUM_KEYS = (
    "repo_types",
    "traits",
    "dod_types",
    "service_statuses",
    "rule_severities",
)


def check_meta_canonical_enums_are_dicts(repo_path: Path) -> list[Finding]:
    """META-003: Schema enumerations are dict maps, not YAML lists."""
    CHECK_ID = "META-003"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"
    if not index_path.exists():
        return findings
    try:
        import yaml as _yaml

        data = _yaml.safe_load(index_path.read_text()) or {}
    except Exception:
        findings.append(
            _finding(
                "META-003",
                "WARN",
                "structural_conformance",
                "index.yaml is not parseable YAML — cannot validate canonical enum dict shapes.",
                "Fix YAML syntax errors reported by the standards CI job.",
            )
        )
        return findings

    schema = data.get("schema") or {}
    for key in _CANONICAL_ENUM_KEYS:
        val = schema.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            findings.append(
                _finding(
                    "META-003",
                    "WARN",
                    "structural_conformance",
                    f"schema.{key} is a YAML list — canonical enums must be dict maps.",
                    "Convert the enumeration to a mapping keyed by stable identifiers.",
                )
            )
    return findings


def check_meta_005_check_notes_prefix(repo_path: Path) -> list[Finding]:
    """META-005: Checkable rules declare DETERMINISTIC or LLM in check_notes.

    Scans every rule across standards/*.yaml. For each rule with
    checkable: true, verifies the first non-blank line of check_notes
    starts with `DETERMINISTIC CHECK.` or `LLM CHECK.`.

    Only runs against standards-repo — applies_to: [standards-repo].
    """
    CHECK_ID = "META-005"
    findings: list[Finding] = []
    standards_dir = repo_path / "standards"
    if not standards_dir.is_dir():
        return findings
    for yaml_path in sorted(standards_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        rules = data.get("standards") or []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if not rule.get("checkable"):
                continue
            rule_id = str(rule.get("id") or "").strip()
            notes = str(rule.get("check_notes") or "")
            stripped_lines = [ln for ln in notes.splitlines() if ln.strip()]
            first = stripped_lines[0].strip() if stripped_lines else ""
            if not (
                first.startswith("DETERMINISTIC CHECK.")
                or first.startswith("LLM CHECK.")
            ):
                findings.append(
                    _finding(
                        CHECK_ID,
                        "WARN",
                        "documentation_coverage",
                        f"Rule {rule_id} in standards/{yaml_path.name} is "
                        f"checkable but check_notes does not begin with "
                        f"`DETERMINISTIC CHECK.` or `LLM CHECK.` "
                        f"(first line: {first[:80]!r}).",
                        "Prepend the correct marker line. Do not rewrite "
                        "the rest of check_notes.",
                    )
                )
    return findings


def check_meta_006_prefix_file_correlation(repo_path: Path) -> list[Finding]:
    """META-006: Rule ID prefix matches the file's declared rule_prefix.

    Reads index.yaml's files: section. For each file entry with a
    rule_prefix, opens the referenced standards file and verifies
    every rule ID starts with one of the declared prefixes.

    Only runs against standards-repo.
    """
    CHECK_ID = "META-006"
    findings: list[Finding] = []
    index_path = repo_path / "index.yaml"
    if not index_path.is_file():
        return findings
    try:
        index = yaml.safe_load(index_path.read_text()) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return findings
    raw_files = index.get("files") or []
    if not isinstance(raw_files, list):
        return findings

    id_prefix_re = re.compile(r"^([A-Z]+)-")

    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        file_rel = str(entry.get("file") or "").strip()
        raw_prefix = entry.get("rule_prefix")
        if not file_rel or raw_prefix is None:
            continue
        if isinstance(raw_prefix, str):
            declared = {raw_prefix}
        elif isinstance(raw_prefix, list):
            declared = {str(x) for x in raw_prefix if isinstance(x, str)}
        else:
            continue
        if not declared:
            continue

        target = repo_path / file_rel
        if not target.is_file():
            continue
        try:
            data = yaml.safe_load(target.read_text()) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        rules = data.get("standards") or []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("id") or "").strip()
            m = id_prefix_re.match(rule_id)
            if not m:
                continue
            rule_prefix = m.group(1)
            if rule_prefix not in declared:
                findings.append(
                    _finding(
                        CHECK_ID,
                        "WARN",
                        "structural_conformance",
                        f"Rule {rule_id} lives in {file_rel} which "
                        f"declares rule_prefix {sorted(declared)!r}. "
                        f"The rule's prefix {rule_prefix!r} does not match.",
                        f"Move the rule to the correct file, or add "
                        f"{rule_prefix!r} to rule_prefix in index.yaml "
                        f"if this file legitimately owns that prefix.",
                    )
                )
    return findings


def check_meta_007_rule_ids_unique(repo_path: Path) -> list[Finding]:
    """META-007: Rule IDs are append-only — retired numbers should not be reused.

    Collects every rule id across all standards/*.yaml files and
    flags any ID appearing more than once. Detects in-tree repurposing.
    Git-history-based retired-then-reintroduced detection is out of
    scope — covered by playbook discipline.

    Only runs against standards-repo.
    """
    CHECK_ID = "META-007"
    findings: list[Finding] = []
    standards_dir = repo_path / "standards"
    if not standards_dir.is_dir():
        return findings

    from collections import defaultdict

    id_locations: dict[str, list[str]] = defaultdict(list)
    for yaml_path in sorted(standards_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        rules = data.get("standards") or []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("id") or "").strip()
            if rule_id:
                id_locations[rule_id].append(yaml_path.name)

    for rule_id, locations in id_locations.items():
        if len(locations) > 1:
            findings.append(
                _finding(
                    CHECK_ID,
                    "WARN",
                    "structural_conformance",
                    f"Rule ID {rule_id} appears {len(locations)} times "
                    f"across standards/ ({', '.join(locations)}). Rule IDs "
                    f"must be unique — IDs are append-only and cannot be "
                    f"reused.",
                    f"Rename one of the duplicate {rule_id} rules to a "
                    f"fresh, unused ID from the prefix's sequence.",
                )
            )
    return findings
