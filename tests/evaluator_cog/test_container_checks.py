"""Deterministic container / platform-descriptor checks: CD-017, CD-021..CD-024."""

from __future__ import annotations

import json
from pathlib import Path

from evaluator_cog.engine.deterministic.containers import (
    check_cd_017,
    check_cd_021,
    check_cd_022,
    check_cd_023,
    check_cd_024,
)


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _railway(repo: Path, payload: dict, rel: str = "railway.json") -> None:
    _write(repo, rel, json.dumps(payload, indent=2))


def _good_deploy() -> dict:
    return {
        "deploy": {
            "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": 10,
            "startCommand": "python -m evaluator_cog",
        }
    }


def _ids(findings: list[dict]) -> set[str]:
    return {f["rule_id"] for f in findings}


def _blob(findings: list[dict]) -> str:
    return " ".join(f["finding"] for f in findings)


# --- CD-017 ------------------------------------------------------------------


def test_cd017_passes_on_compliant_railway_json(tmp_path: Path) -> None:
    _railway(tmp_path, _good_deploy())
    assert check_cd_017(tmp_path) == []


def test_cd017_flags_absent_railway_json(tmp_path: Path) -> None:
    findings = check_cd_017(tmp_path)
    assert _ids(findings) == {"CD-017"}
    assert "No railway.json or railway.toml found" in _blob(findings)


def test_cd017_reads_a_railway_toml(tmp_path: Path) -> None:
    """Railway accepts either spelling and CD-024 already read both.

    deejaytools-com keeps its descriptor in railway.toml and was reported
    for having no restart policy at all, when what it actually has is a
    policy with too few retries. Which file the policy lives in was never
    the rule's question.
    """
    _write(
        tmp_path,
        "railway.toml",
        "[deploy]\n"
        'startCommand = "python -m pkg.main"\n'
        'restartPolicyType = "ON_FAILURE"\n'
        "restartPolicyMaxRetries = 10\n",
    )
    assert check_cd_017(tmp_path) == []


def test_cd017_flags_a_railway_toml_with_too_few_retries(tmp_path: Path) -> None:
    """The toml is read for content, not merely accepted for existing."""
    _write(
        tmp_path,
        "railway.toml",
        "[deploy]\n"
        'startCommand = "python -m pkg.main"\n'
        'restartPolicyType = "ON_FAILURE"\n'
        "restartPolicyMaxRetries = 3\n",
    )
    findings = check_cd_017(tmp_path)
    assert len(findings) == 1
    assert "restartPolicyMaxRetries" in findings[0]["finding"]


def test_cd017_flags_malformed_railway_json(tmp_path: Path) -> None:
    _write(tmp_path, "railway.json", "{ this is not json")
    findings = check_cd_017(tmp_path)
    assert len(findings) == 1
    assert "does not parse" in findings[0]["finding"]


def test_cd017_flags_restart_policy_never(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyType"] = "NEVER"
    _railway(tmp_path, payload)
    findings = check_cd_017(tmp_path)
    assert "restartPolicyType" in _blob(findings)
    assert "NEVER" in _blob(findings)


def test_cd017_flags_restart_policy_always(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyType"] = "ALWAYS"
    _railway(tmp_path, payload)
    assert "ALWAYS" in _blob(check_cd_017(tmp_path))


def test_cd017_flags_restart_policy_null_and_missing(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyType"] = None
    _railway(tmp_path, payload)
    assert "restartPolicyType" in _blob(check_cd_017(tmp_path))

    payload = _good_deploy()
    del payload["deploy"]["restartPolicyType"]
    _railway(tmp_path, payload)
    findings = check_cd_017(tmp_path)
    assert "restartPolicyType is missing" in _blob(findings)


def test_cd017_flags_max_retries_below_ten(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyMaxRetries"] = 3
    _railway(tmp_path, payload)
    findings = check_cd_017(tmp_path)
    assert "restartPolicyMaxRetries" in _blob(findings)


def test_cd017_accepts_max_retries_above_ten(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyMaxRetries"] = 42
    _railway(tmp_path, payload)
    assert check_cd_017(tmp_path) == []


def test_cd017_flags_max_retries_null_and_missing(tmp_path: Path) -> None:
    payload = _good_deploy()
    payload["deploy"]["restartPolicyMaxRetries"] = None
    _railway(tmp_path, payload)
    assert "restartPolicyMaxRetries" in _blob(check_cd_017(tmp_path))

    payload = _good_deploy()
    del payload["deploy"]["restartPolicyMaxRetries"]
    _railway(tmp_path, payload)
    assert "restartPolicyMaxRetries is missing" in _blob(check_cd_017(tmp_path))


def test_cd017_rejects_boolean_max_retries(tmp_path: Path) -> None:
    """True is an int in Python but is not a retry count."""
    payload = _good_deploy()
    payload["deploy"]["restartPolicyMaxRetries"] = True
    _railway(tmp_path, payload)
    assert "restartPolicyMaxRetries" in _blob(check_cd_017(tmp_path))


def test_cd017_flags_missing_and_empty_start_command(tmp_path: Path) -> None:
    payload = _good_deploy()
    del payload["deploy"]["startCommand"]
    _railway(tmp_path, payload)
    assert "startCommand is missing" in _blob(check_cd_017(tmp_path))

    payload = _good_deploy()
    payload["deploy"]["startCommand"] = "   "
    _railway(tmp_path, payload)
    assert "startCommand" in _blob(check_cd_017(tmp_path))


def test_cd017_flags_every_broken_key_at_once(tmp_path: Path) -> None:
    _railway(tmp_path, {"deploy": {}})
    findings = check_cd_017(tmp_path)
    assert len(findings) == 3


def test_cd017_prefers_monorepo_path_over_repo_root(tmp_path: Path) -> None:
    _railway(tmp_path, _good_deploy())
    service = tmp_path / "services" / "api"
    service.mkdir(parents=True)
    _railway(service, {"deploy": {"restartPolicyType": "NEVER"}})
    findings = check_cd_017(tmp_path, monorepo_path=service)
    assert "NEVER" in _blob(findings)


def test_cd017_falls_back_to_repo_root_for_monorepo_service(tmp_path: Path) -> None:
    _railway(tmp_path, _good_deploy())
    service = tmp_path / "services" / "api"
    service.mkdir(parents=True)
    assert check_cd_017(tmp_path, monorepo_path=service) == []


# --- CD-021 ------------------------------------------------------------------


def test_cd021_passes_with_dockerfile_and_dockerfile_builder(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python@sha256:" + "a" * 64 + "\n")
    _railway(tmp_path, {"build": {"builder": "DOCKERFILE"}})
    assert check_cd_021(tmp_path) == []


def test_cd021_passes_when_builder_selected_via_dockerfile_path(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    _railway(tmp_path, {"build": {"dockerfilePath": "Dockerfile"}})
    assert check_cd_021(tmp_path) == []


def test_cd021_passes_with_railway_toml_descriptor(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    _write(tmp_path, "railway.toml", '[build]\nbuilder = "dockerfile"\n')
    assert check_cd_021(tmp_path) == []


def test_cd021_records_gap_when_dockerfile_absent(tmp_path: Path) -> None:
    _railway(tmp_path, {"build": {"builder": "NIXPACKS"}})
    findings = check_cd_021(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-021"
    assert findings[0]["severity"] == "WARN"
    assert "gap recorded" in findings[0]["finding"]
    assert "no Dockerfile" in findings[0]["finding"]


def test_cd021_records_gap_when_nothing_present_at_all(tmp_path: Path) -> None:
    findings = check_cd_021(tmp_path)
    assert len(findings) == 1
    assert "no railway.json" in findings[0]["finding"]


def test_cd021_records_gap_when_platform_ignores_the_dockerfile(tmp_path: Path) -> None:
    """A Dockerfile the platform does not select is not a runtime definition."""
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    _railway(tmp_path, {"build": {"builder": "NIXPACKS"}})
    findings = check_cd_021(tmp_path)
    assert len(findings) == 1
    assert "does not select the dockerfile builder" in findings[0]["finding"]


def test_cd021_records_gap_when_dockerfile_has_no_descriptor(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    findings = check_cd_021(tmp_path)
    assert len(findings) == 1
    assert "no railway.json" in findings[0]["finding"]


# --- CD-022 ------------------------------------------------------------------

_DIGEST_A = "sha256:" + "1" * 64
_DIGEST_B = "sha256:" + "2" * 64


def test_cd022_skips_silently_when_no_dockerfile(tmp_path: Path) -> None:
    """No Dockerfile means no subject — CD-021 owns that absence, not CD-022."""
    _railway(tmp_path, _good_deploy())
    assert check_cd_022(tmp_path) == []


def test_cd022_passes_when_every_stage_is_digest_pinned(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        f"FROM python@{_DIGEST_A} AS builder\n"
        "RUN echo build\n"
        f"FROM gcr.io/distroless/python3@{_DIGEST_B}\n"
        "USER nonroot\n",
    )
    assert check_cd_022(tmp_path) == []


def test_cd022_flags_tag_pinned_from_with_line_number(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        "# syntax=docker/dockerfile:1\nRUN true\nFROM python:3.11-slim\n",
    )
    findings = check_cd_022(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-022"
    assert "Dockerfile:3" in findings[0]["finding"]
    assert "python:3.11-slim" in findings[0]["finding"]


def test_cd022_flags_only_the_unpinned_stage_of_a_multi_stage_build(
    tmp_path: Path,
) -> None:
    """The common case: one stage pinned, the other left on a floating tag."""
    _write(
        tmp_path,
        "Dockerfile",
        f"FROM python@{_DIGEST_A} AS builder\n"
        "RUN pip install .\n"
        "\n"
        "FROM python:3.11-slim AS runtime\n"
        "USER appuser\n",
    )
    findings = check_cd_022(tmp_path)
    assert len(findings) == 1
    assert "Dockerfile:4" in findings[0]["finding"]
    assert "python:3.11-slim" in findings[0]["finding"]


def test_cd022_flags_each_unpinned_stage_separately(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        "FROM node:20 AS builder\nRUN npm ci\nFROM python:3.11\nUSER appuser\n",
    )
    findings = check_cd_022(tmp_path)
    assert len(findings) == 2
    blob = _blob(findings)
    assert "Dockerfile:1" in blob
    assert "Dockerfile:3" in blob


def test_cd022_handles_lowercase_instruction_and_platform_flag(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        f"from --platform=linux/amd64 python@{_DIGEST_A} as builder\n"
        "From --platform=linux/amd64 python:3.11 As runtime\n",
    )
    findings = check_cd_022(tmp_path)
    assert len(findings) == 1
    assert "Dockerfile:2" in findings[0]["finding"]


def test_cd022_handles_line_continuations_and_comments(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        "FROM \\\n"
        "  # pick the runtime\n"
        "  python:3.11-slim \\\n"
        "  AS runtime\n"
        "USER appuser\n",
    )
    findings = check_cd_022(tmp_path)
    assert len(findings) == 1
    assert "Dockerfile:1" in findings[0]["finding"]
    assert "python:3.11-slim" in findings[0]["finding"]


def test_cd022_remediation_is_concrete(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python:3.11\n")
    findings = check_cd_022(tmp_path)
    assert len(findings[0]["suggestion"]) >= 40
    assert "sha256" in findings[0]["suggestion"]


# --- CD-023 ------------------------------------------------------------------


def test_cd023_skips_silently_when_no_dockerfile(tmp_path: Path) -> None:
    """No Dockerfile means no subject — CD-021 owns that absence, not CD-023."""
    _railway(tmp_path, _good_deploy())
    assert check_cd_023(tmp_path) == []


def test_cd023_passes_with_non_root_user_in_final_stage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        f"FROM python@{_DIGEST_A}\nRUN useradd -r appuser\nUSER appuser\n",
    )
    assert check_cd_023(tmp_path) == []


def test_cd023_passes_with_user_and_group_argument(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM scratch\nUSER appuser:appgroup\n")
    assert check_cd_023(tmp_path) == []


def test_cd023_flags_missing_user_instruction(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python:3.11\nRUN pip install .\n")
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-023"
    assert "no USER instruction" in findings[0]["finding"]


def test_cd023_flags_user_root(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python:3.11\nUSER root\n")
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert "USER root" in findings[0]["finding"]


def test_cd023_flags_user_zero(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python:3.11\nUSER 0\n")
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert "USER 0" in findings[0]["finding"]


def test_cd023_fails_when_user_is_set_only_in_an_earlier_stage(
    tmp_path: Path,
) -> None:
    """A USER in stage 1 does not carry forward into the final stage."""
    _write(
        tmp_path,
        "Dockerfile",
        f"FROM python@{_DIGEST_A} AS builder\n"
        "RUN useradd -r appuser\n"
        "USER appuser\n"
        "RUN pip install .\n"
        "\n"
        f"FROM python@{_DIGEST_B} AS runtime\n"
        "COPY --from=builder /app /app\n",
    )
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert "no USER instruction" in findings[0]["finding"]
    assert "line 6" in findings[0]["finding"]


def test_cd023_passes_multi_stage_with_user_in_the_final_stage(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        f"FROM python@{_DIGEST_A} AS builder\n"
        "RUN pip install .\n"
        f"FROM python@{_DIGEST_B} AS runtime\n"
        "COPY --from=builder /app /app\n"
        "USER appuser\n",
    )
    assert check_cd_023(tmp_path) == []


def test_cd023_flags_final_stage_reverting_to_root(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        "FROM scratch AS builder\n"
        "USER appuser\n"
        "FROM scratch AS runtime\n"
        "USER appuser\n"
        "RUN chown -R appuser /app\n"
        "USER root\n",
    )
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert "Dockerfile:6" in findings[0]["finding"]


def test_cd023_handles_lowercase_and_continuations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Dockerfile",
        "from scratch as builder\nuser appuser\nfrom \\\n  scratch as runtime\n",
    )
    findings = check_cd_023(tmp_path)
    assert len(findings) == 1
    assert "line 3" in findings[0]["finding"]


# --- CD-024 ------------------------------------------------------------------


def test_cd024_flags_absent_descriptor(tmp_path: Path) -> None:
    findings = check_cd_024(tmp_path)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "CD-024"
    assert findings[0]["severity"] == "WARN"
    assert "No railway.json or railway.toml" in findings[0]["finding"]


def test_cd024_passes_with_native_limit_keys(tmp_path: Path) -> None:
    _railway(tmp_path, {"deploy": {"memoryLimit": "2GB", "cpuLimit": 2}})
    assert check_cd_024(tmp_path) == []


def test_cd024_accepts_documented_equivalent_spellings(tmp_path: Path) -> None:
    _railway(tmp_path, {"resources": {"memory_gb": 2, "num_cpus": 1}})
    assert check_cd_024(tmp_path) == []


def test_cd024_accepts_railway_toml(tmp_path: Path) -> None:
    _write(tmp_path, "railway.toml", "[deploy]\nmemoryGB = 2\nnumCpus = 1\n")
    assert check_cd_024(tmp_path) == []


def test_cd024_flags_missing_cpu_limit_only(tmp_path: Path) -> None:
    _railway(tmp_path, {"deploy": {"memoryLimit": "2GB"}})
    findings = check_cd_024(tmp_path)
    assert len(findings) == 1
    assert "no CPU limit" in findings[0]["finding"]


def test_cd024_flags_missing_memory_limit_only(tmp_path: Path) -> None:
    _railway(tmp_path, {"deploy": {"cpuLimit": 2}})
    findings = check_cd_024(tmp_path)
    assert len(findings) == 1
    assert "no memory limit" in findings[0]["finding"]


def test_cd024_flags_null_limit_values(tmp_path: Path) -> None:
    _railway(tmp_path, {"deploy": {"memoryLimit": None, "cpuLimit": None}})
    findings = check_cd_024(tmp_path)
    assert len(findings) == 2


def test_cd024_flags_descriptor_with_no_limit_keys(tmp_path: Path) -> None:
    _railway(tmp_path, _good_deploy())
    findings = check_cd_024(tmp_path)
    assert len(findings) == 2


def test_cd024_flags_unparseable_descriptor(tmp_path: Path) -> None:
    _write(tmp_path, "railway.json", "{ nope")
    findings = check_cd_024(tmp_path)
    assert len(findings) == 1
    assert "does not parse" in findings[0]["finding"]


def test_cd024_remediation_strings_are_concrete(tmp_path: Path) -> None:
    findings = check_cd_024(tmp_path) + check_cd_024(tmp_path)
    for f in findings:
        assert len(f["suggestion"]) >= 40
