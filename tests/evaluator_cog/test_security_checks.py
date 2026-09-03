"""Deterministic security-posture checks: SEC-001..SEC-006."""

from __future__ import annotations

from pathlib import Path

from evaluator_cog.engine.deterministic.security import (
    check_sec_001,
    check_sec_002,
    check_sec_003,
    check_sec_004,
    check_sec_005,
    check_sec_006,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _ids(findings: list[dict], rule_id: str) -> list[dict]:
    return [f for f in findings if f["rule_id"] == rule_id]


# --- SEC-001 -----------------------------------------------------------------


def test_sec001_flags_absent_precommit_config(tmp_path: Path) -> None:
    f = check_sec_001(tmp_path)
    assert len(_ids(f, "SEC-001")) == 1
    assert f[0]["severity"] == "WARN"
    assert f[0]["dimension"] == "security_posture"
    assert ".pre-commit-config.yaml" in f[0]["finding"]


def test_sec001_passes_with_gitleaks_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.18.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n",
    )
    assert check_sec_001(tmp_path) == []


def test_sec001_passes_with_detect_secrets_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n"
        "  - repo: https://github.com/Yelp/detect-secrets\n"
        "    rev: v1.5.0\n"
        "    hooks:\n"
        "      - id: detect-secrets\n",
    )
    assert check_sec_001(tmp_path) == []


def test_sec001_passes_with_trufflehog_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n"
        "  - repo: https://github.com/trufflesecurity/trufflehog\n"
        "    rev: v3.63.0\n"
        "    hooks:\n"
        "      - id: trufflehog\n",
    )
    assert check_sec_001(tmp_path) == []


def test_sec001_flags_config_without_secret_scanner(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n"
        "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
        "    rev: v0.8.0\n"
        "    hooks:\n"
        "      - id: ruff\n",
    )
    f = check_sec_001(tmp_path)
    assert len(_ids(f, "SEC-001")) == 1
    assert "ruff-pre-commit" in f[0]["finding"]


def test_sec001_matches_repo_url_not_hook_id(tmp_path: Path) -> None:
    """A hook id that happens to say 'gitleaks' under an unrelated repo
    must not satisfy the rule — check_notes matches the repo URL."""
    _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        entry: echo no-op\n"
        "        language: system\n",
    )
    assert len(_ids(check_sec_001(tmp_path), "SEC-001")) == 1


def test_sec001_flags_unparseable_config(tmp_path: Path) -> None:
    _write(tmp_path, ".pre-commit-config.yaml", "repos: [\n  - repo: broken\n")
    f = check_sec_001(tmp_path)
    assert len(_ids(f, "SEC-001")) == 1
    assert "could not be read or parsed" in f[0]["finding"]


# --- SEC-002 -----------------------------------------------------------------


def _pr_workflow(step_body: str, *, job_extra: str = "") -> str:
    return (
        "name: ci\n"
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  scan:\n"
        "    runs-on: ubuntu-latest\n"
        f"{job_extra}"
        "    steps:\n"
        f"{step_body}"
    )


def test_sec002_flags_absent_workflows_dir(tmp_path: Path) -> None:
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert f[0]["severity"] == "ERROR"
    assert f[0]["dimension"] == "security_posture"


def test_sec002_passes_with_gitleaks_action(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow(
            "      - name: Scan for secrets\n        uses: gitleaks/gitleaks-action@v2\n"
        ),
    )
    assert check_sec_002(tmp_path) == []


def test_sec002_passes_with_trufflehog_run(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow("      - run: trufflehog git file://. --only-verified\n"),
    )
    assert check_sec_002(tmp_path) == []


def test_sec002_passes_with_reusable_security_workflow(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  security:\n"
        "    uses: mini-app-polis/.github/.github/workflows/security.yml@v1\n",
    )
    assert check_sec_002(tmp_path) == []


def test_sec002_flags_pr_workflow_without_secret_scan(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow("      - run: pytest -q\n"),
    )
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert "ci.yml" in f[0]["finding"]


def test_sec002_flags_scan_only_on_push(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  scan:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: gitleaks/gitleaks-action@v2\n",
    )
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert "pull_request" in f[0]["finding"]


def test_sec002_flags_step_level_continue_on_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow(
            "      - name: gitleaks\n"
            "        uses: gitleaks/gitleaks-action@v2\n"
            "        continue-on-error: true\n"
        ),
    )
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert "the step sets continue-on-error: true" in f[0]["finding"]


def test_sec002_flags_job_level_continue_on_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow(
            "      - name: gitleaks\n        uses: gitleaks/gitleaks-action@v2\n",
            job_extra="    continue-on-error: true\n",
        ),
    )
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert "job `scan` sets continue-on-error: true" in f[0]["finding"]


def test_sec002_flags_non_gating_reusable_workflow_job(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  secrets:\n"
        "    continue-on-error: true\n"
        "    uses: mini-app-polis/.github/.github/workflows/secret-scan.yml@v1\n",
    )
    f = check_sec_002(tmp_path)
    assert len(_ids(f, "SEC-002")) == 1
    assert "secret-scan.yml" in f[0]["finding"]


def test_sec002_one_gating_step_is_enough(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _pr_workflow(
            "      - name: advisory scan\n"
            "        run: trufflehog git file://.\n"
            "        continue-on-error: true\n"
            "      - name: blocking scan\n"
            "        uses: gitleaks/gitleaks-action@v2\n"
        ),
    )
    assert check_sec_002(tmp_path) == []


# --- SEC-003 -----------------------------------------------------------------


def _audit_workflow(step_body: str, *, job_extra: str = "") -> str:
    return (
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  audit:\n"
        "    runs-on: ubuntu-latest\n"
        f"{job_extra}"
        "    steps:\n"
        f"{step_body}"
    )


def test_sec003_no_manifest_returns_nothing(tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/ci.yml", _audit_workflow("      - run: make\n"))
    assert check_sec_003(tmp_path) == []


def test_sec003_flags_absent_workflows_dir(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    f = check_sec_003(tmp_path)
    assert len(_ids(f, "SEC-003")) == 1
    assert f[0]["severity"] == "ERROR"
    assert "pip-audit" in f[0]["finding"]


def test_sec003_python_passes_with_pip_audit(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pip-audit --strict\n"),
    )
    assert check_sec_003(tmp_path) == []


def test_sec003_python_flags_missing_pip_audit(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pytest -q\n"),
    )
    f = check_sec_003(tmp_path)
    assert len(_ids(f, "SEC-003")) == 1
    assert "pip-audit" in f[0]["finding"]


def test_sec003_typescript_passes_with_pnpm_audit(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}\n')
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pnpm audit --audit-level high\n"),
    )
    assert check_sec_003(tmp_path) == []


def test_sec003_typescript_passes_with_npm_audit(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}\n')
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: npm audit --audit-level=high\n"),
    )
    assert check_sec_003(tmp_path) == []


def test_sec003_typescript_flags_missing_audit(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}\n')
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pnpm test\n"),
    )
    assert len(_ids(check_sec_003(tmp_path), "SEC-003")) == 1


def test_sec003_polyglot_repo_satisfied_by_either_scanner(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "package.json", '{"name": "x"}\n')
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pnpm audit\n"),
    )
    assert check_sec_003(tmp_path) == []


def test_sec003_flags_step_level_continue_on_error(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow(
            "      - name: audit\n"
            "        run: pip-audit\n"
            "        continue-on-error: true\n"
        ),
    )
    f = check_sec_003(tmp_path)
    assert len(_ids(f, "SEC-003")) == 1
    assert "the step sets continue-on-error: true" in f[0]["finding"]


def test_sec003_flags_job_level_continue_on_error(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow(
            "      - name: audit\n        run: pip-audit\n",
            job_extra="    continue-on-error: true\n",
        ),
    )
    f = check_sec_003(tmp_path)
    assert len(_ids(f, "SEC-003")) == 1
    assert "job `audit` sets continue-on-error: true" in f[0]["finding"]


def test_sec003_does_not_inspect_severity_flags(tmp_path: Path) -> None:
    """check_notes forbids comparing the tool's severity flag to SEC-006;
    a bare pip-audit with no threshold flag still passes."""
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pip-audit\n"),
    )
    assert check_sec_003(tmp_path) == []


# --- SEC-004 -----------------------------------------------------------------


def test_sec004_flags_absent_workflows_dir(tmp_path: Path) -> None:
    f = check_sec_004(tmp_path)
    assert len(_ids(f, "SEC-004")) == 1
    assert f[0]["severity"] == "WARN"


def test_sec004_passes_with_codeql_analyze(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/codeql.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  analyze:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: github/codeql-action/init@v3\n"
        "      - uses: github/codeql-action/analyze@v3\n",
    )
    assert check_sec_004(tmp_path) == []


def test_sec004_flags_codeql_init_without_analyze(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/codeql.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  analyze:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: github/codeql-action/init@v3\n",
    )
    assert len(_ids(check_sec_004(tmp_path), "SEC-004")) == 1


def test_sec004_passes_with_semgrep_run(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/sast.yml",
        _audit_workflow("      - run: semgrep --config auto\n"),
    )
    assert check_sec_004(tmp_path) == []


def test_sec004_passes_with_bandit_run(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/sast.yml",
        _audit_workflow("      - run: bandit -r src\n"),
    )
    assert check_sec_004(tmp_path) == []


def test_sec004_non_gating_sast_still_passes(tmp_path: Path) -> None:
    """Unlike SEC-002/003, gating is explicitly not part of SEC-004."""
    _write(
        tmp_path,
        ".github/workflows/sast.yml",
        _audit_workflow(
            "      - run: semgrep --config auto\n        continue-on-error: true\n",
            job_extra="    continue-on-error: true\n",
        ),
    )
    assert check_sec_004(tmp_path) == []


def test_sec004_flags_workflows_without_sast(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pytest -q\n"),
    )
    f = check_sec_004(tmp_path)
    assert len(_ids(f, "SEC-004")) == 1
    assert "ci.yml" in f[0]["finding"]


# --- SEC-005 -----------------------------------------------------------------


def test_sec005_flags_absent_workflows_dir(tmp_path: Path) -> None:
    f = check_sec_005(tmp_path)
    assert len(_ids(f, "SEC-005")) == 1
    assert f[0]["severity"] == "WARN"


def test_sec005_passes_with_sbom_and_upload_in_same_job(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: anchore/sbom-action@v0\n"
        "      - uses: actions/upload-artifact@v4\n",
    )
    assert check_sec_005(tmp_path) == []


def test_sec005_passes_with_syft_run_and_upload(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: syft . -o cyclonedx-json > sbom.json\n"
        "      - uses: actions/upload-artifact@v4\n"
        "        with:\n"
        "          path: sbom.json\n",
    )
    assert check_sec_005(tmp_path) == []


def test_sec005_flags_sbom_and_upload_in_different_jobs(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  sbom:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: anchore/sbom-action@v0\n"
        "  package:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/upload-artifact@v4\n",
    )
    f = check_sec_005(tmp_path)
    assert len(_ids(f, "SEC-005")) == 1
    assert "release.yml::sbom" in f[0]["finding"]
    assert "release.yml::package" in f[0]["finding"]


def test_sec005_flags_sbom_generated_but_never_uploaded(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  sbom:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: cyclonedx-py environment -o sbom.json\n",
    )
    f = check_sec_005(tmp_path)
    assert len(_ids(f, "SEC-005")) == 1
    assert "never uploads it" in f[0]["finding"]


def test_sec005_flags_upload_without_sbom(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/release.yml",
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  package:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/upload-artifact@v4\n",
    )
    f = check_sec_005(tmp_path)
    assert len(_ids(f, "SEC-005")) == 1
    assert "No SBOM is generated" in f[0]["finding"]


def test_sec005_flags_workflows_with_neither(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _audit_workflow("      - run: pytest -q\n"),
    )
    f = check_sec_005(tmp_path)
    assert len(_ids(f, "SEC-005")) == 1
    assert "generates an SBOM" in f[0]["finding"]


# --- SEC-006 -----------------------------------------------------------------


_SEVERITIES_BLOCK = (
    "severities:\n"
    "  CRITICAL: System-level failure.\n"
    "  HIGH: Serious but not system-level.\n"
    "  ERROR: Requires remediation.\n"
    "  WARN: Remediation recommended.\n"
    "  INFO: Observation worth noting.\n"
)


def test_sec006_flags_absent_index_yaml(tmp_path: Path) -> None:
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert f[0]["severity"] == "INFO"
    assert f[0]["dimension"] == "security_posture"


def test_sec006_passes_with_declared_deadlines(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        _SEVERITIES_BLOCK + "vulnerability_response:\n"
        "  deadlines:\n"
        "    CRITICAL: 7\n"
        "    HIGH: 30\n",
    )
    assert check_sec_006(tmp_path) == []


def test_sec006_flags_missing_vulnerability_response(tmp_path: Path) -> None:
    _write(tmp_path, "index.yaml", _SEVERITIES_BLOCK)
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "vulnerability_response" in f[0]["finding"]


def test_sec006_flags_missing_deadlines_block(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        _SEVERITIES_BLOCK + "vulnerability_response:\n  measured_from: scan date\n",
    )
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "deadlines" in f[0]["finding"]


def test_sec006_flags_missing_high_deadline(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        _SEVERITIES_BLOCK + "vulnerability_response:\n  deadlines:\n    CRITICAL: 7\n",
    )
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "HIGH" in f[0]["finding"]


def test_sec006_flags_non_integer_deadline(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        _SEVERITIES_BLOCK + "vulnerability_response:\n"
        "  deadlines:\n"
        "    CRITICAL: seven days\n"
        "    HIGH: 30\n",
    )
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "not an integer" in f[0]["finding"]


def test_sec006_flags_undeclared_severity_name(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        _SEVERITIES_BLOCK + "vulnerability_response:\n"
        "  deadlines:\n"
        "    CRITICAL: 7\n"
        "    HIGH: 30\n"
        "    MODERATE: 90\n",
    )
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "MODERATE" in f[0]["finding"]


def test_sec006_flags_absent_severities_block_once(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        "vulnerability_response:\n  deadlines:\n    CRITICAL: 7\n    HIGH: 30\n",
    )
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "severities" in f[0]["finding"]


def test_sec006_accepts_severities_as_sequence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "index.yaml",
        "severities:\n"
        "  - CRITICAL\n"
        "  - HIGH\n"
        "vulnerability_response:\n"
        "  deadlines:\n"
        "    CRITICAL: 7\n"
        "    HIGH: 30\n",
    )
    assert check_sec_006(tmp_path) == []


def test_sec006_flags_unparseable_index_yaml(tmp_path: Path) -> None:
    _write(tmp_path, "index.yaml", "severities: [\n  CRITICAL\n")
    f = check_sec_006(tmp_path)
    assert len(_ids(f, "SEC-006")) == 1
    assert "could not be read or parsed" in f[0]["finding"]
