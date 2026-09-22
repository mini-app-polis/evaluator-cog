"""Deterministic checks for the queue-and-Lambda runtime (ADR-009).

PIPE-007 (call-site retry), PIPE-016 (Lambda behind its own queue),
PIPE-017 (per-record failure, redrive, visibility), PIPE-018 (concurrency
ceiling, stated once), PIPE-019 (trigger cogs go through the API),
PIPE-020 (a run stops before the function timeout), CD-027 (Terraform
checked in CI), CD-028 (versions pinned, lock committed), SEC-008
(state and tfvars never committed), and the pipeline-cog branches of
CD-010 (DLQ alarm as Layer 1) and CD-024 (function limits).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic._terraform import (
    dead_letter_queue_names,
    infra_resources,
    of_type,
)
from evaluator_cog.engine.deterministic.containers import check_cd_024
from evaluator_cog.engine.deterministic.delivery import (
    check_terraform_checked_in_ci,
    check_terraform_versions_pinned,
    check_three_layer_observability,
)
from evaluator_cog.engine.deterministic.pipeline import (
    check_pipe_016,
    check_pipe_017,
    check_pipe_018,
    check_pipe_019,
    check_pipe_020,
    check_retry_logic,
)
from evaluator_cog.engine.deterministic.security import check_sec_008

_QUEUE_TF = """
resource "aws_sqs_queue" "dlq" {
  name = "${var.name_prefix}-jobs-dlq" # braces inside a string
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name_prefix}-jobs"
  visibility_timeout_seconds = var.worker_timeout_seconds + 60
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}
"""

_WORKER_TF = """
resource "aws_lambda_function" "worker" {
  function_name = "demo-worker"
  timeout       = var.worker_timeout_seconds
  memory_size   = 1024
  environment {
    variables = {
      REDIRECT = "http://127.0.0.1:8888/callback" // a comment after a URL
    }
  }
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn        = aws_sqs_queue.jobs.arn
  function_name           = aws_lambda_function.worker.arn
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 2
  }
}
"""

_ALARM_TF = """
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  namespace     = "AWS/SQS"
  metric_name   = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
"""

_VERSIONS_TF = """
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
"""

#: Two platforms' hashes, which is what a stack checked in CI needs.
_LOCK_HCL = """
provider "registry.terraform.io/hashicorp/aws" {
  version     = "5.82.2"
  constraints = "~> 5.0"
  hashes = [
    "h1:darwin_arm64_placeholder",
    "h1:linux_amd64_placeholder",
  ]
}
"""

_HANDLER = (
    "def lambda_handler(event, context):\n"
    "    context.get_remaining_time_in_millis()\n"
    "    return {'batchItemFailures': []}\n"
)

_CI_YML = """
name: CI
on: [push]
jobs:
  test:
    uses: mini-app-polis/.github/.github/workflows/python-test.yml@v1
    with:
      terraform-dir: infra
"""


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _lambda_cog(repo: Path, **overrides: str) -> Path:
    """A pipeline cog that satisfies every runtime rule, with parts replaceable."""
    files = {
        "infra/queue.tf": _QUEUE_TF,
        "infra/worker.tf": _WORKER_TF,
        "infra/account.tf": _ALARM_TF,
        "src/demo_cog/worker.py": _HANDLER,
        ".github/workflows/ci.yml": _CI_YML,
        "infra/versions.tf": _VERSIONS_TF,
        "infra/.terraform.lock.hcl": _LOCK_HCL,
        "infra/.gitignore": "*.tfstate\n*.tfstate.*\nterraform.tfvars\ntfplan\n",
        "pyproject.toml": '[project]\nname = "demo-cog"\ndependencies = ["httpx"]\n',
    }
    files.update(overrides)
    for rel, body in files.items():
        _write(repo, rel, body)
    return repo


def _messages(findings: list[dict]) -> str:
    return " | ".join(f["finding"] for f in findings)


# --- the Terraform reader ----------------------------------------------------


def test_reader_sees_through_strings_and_comments(tmp_path: Path) -> None:
    resources = infra_resources(_lambda_cog(tmp_path))
    assert resources is not None
    assert [(r.type, r.name) for r in resources] == [
        ("aws_cloudwatch_metric_alarm", "dlq_not_empty"),
        ("aws_sqs_queue", "dlq"),
        ("aws_sqs_queue", "jobs"),
        ("aws_lambda_function", "worker"),
        ("aws_lambda_event_source_mapping", "jobs"),
    ]
    fn = of_type(resources, "aws_lambda_function")[0]
    # The URL's `//` is not a comment, so the closing braces after it survive
    # and the function's block ends where it should.
    assert fn.attr("memory_size") == "1024"
    assert "ReportBatchItemFailures" not in fn.body
    assert dead_letter_queue_names(resources) == {"dlq"}


def test_reader_distinguishes_no_infra_from_empty_infra(tmp_path: Path) -> None:
    assert infra_resources(tmp_path) is None
    (tmp_path / "infra").mkdir()
    assert infra_resources(tmp_path) == []


# --- the reference cogs pass everything ---------------------------------------


def test_compliant_lambda_cog_passes_every_runtime_rule(tmp_path: Path) -> None:
    repo = _lambda_cog(tmp_path)
    assert check_pipe_016(repo) == []
    assert check_pipe_017(repo) == []
    assert check_pipe_018(repo) == []
    assert check_pipe_020(repo) == []
    assert check_terraform_checked_in_ci(repo) == []
    assert check_terraform_versions_pinned(repo) == []
    assert check_sec_008(repo) == []
    assert check_cd_024(repo, repo_type="pipeline-cog") == []
    layer1 = [
        f
        for f in check_three_layer_observability(repo, cog_subtype="pipeline")
        if "Layer 1" in f["finding"]
    ]
    assert layer1 == []


# --- PIPE-016 -----------------------------------------------------------------


def test_pipe016_names_every_missing_resource_when_infra_is_absent(
    tmp_path: Path,
) -> None:
    findings = check_pipe_016(tmp_path)
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert "infra/ (absent)" in text
    for rtype in (
        "aws_sqs_queue",
        "aws_lambda_function",
        "aws_lambda_event_source_mapping",
    ):
        assert rtype in text


def test_pipe016_names_only_the_missing_mapping(tmp_path: Path) -> None:
    worker_without_mapping = _WORKER_TF.split('resource "aws_lambda_event_source')[0]
    repo = _lambda_cog(tmp_path, **{"infra/worker.tf": worker_without_mapping})
    text = _messages(check_pipe_016(repo))
    assert "aws_lambda_event_source_mapping" in text
    assert "aws_sqs_queue," not in text


@pytest.mark.parametrize(
    "dependency",
    ['"prefect>=3.0,<4.0"', '"prefect[aws]==3.1"', "'prefect'"],
)
def test_pipe016_flags_prefect_still_declared(tmp_path: Path, dependency: str) -> None:
    repo = _lambda_cog(
        tmp_path,
        **{"pyproject.toml": f"[project]\ndependencies = [{dependency}]\n"},
    )
    assert "prefect is still a declared dependency" in _messages(check_pipe_016(repo))


def test_pipe016_flags_prefect_in_requirements_txt(tmp_path: Path) -> None:
    repo = _lambda_cog(tmp_path, **{"requirements.txt": "prefect==3.1.0\n"})
    assert "prefect" in _messages(check_pipe_016(repo))


def test_pipe016_ignores_packages_that_merely_start_with_prefect(
    tmp_path: Path,
) -> None:
    repo = _lambda_cog(
        tmp_path,
        **{"pyproject.toml": '[project]\ndependencies = ["prefect-shell>=0.2"]\n'},
    )
    assert check_pipe_016(repo) == []


# --- PIPE-017 -----------------------------------------------------------------


def test_pipe017_is_silent_without_infra(tmp_path: Path) -> None:
    """PIPE-016 reports the missing infra/; four echoes of it would bury it."""
    assert check_pipe_017(tmp_path) == []


def test_pipe017_flags_mapping_without_report_batch_item_failures(
    tmp_path: Path,
) -> None:
    worker = _WORKER_TF.replace(
        '  function_response_types = ["ReportBatchItemFailures"]\n', ""
    )
    repo = _lambda_cog(tmp_path, **{"infra/worker.tf": worker})
    findings = check_pipe_017(repo)
    assert len(findings) == 1
    assert "ReportBatchItemFailures" in findings[0]["finding"]


def test_pipe017_flags_queue_without_redrive(tmp_path: Path) -> None:
    queue = (
        'resource "aws_sqs_queue" "jobs" {\n'
        "  visibility_timeout_seconds = var.worker_timeout_seconds + 60\n"
        "}\n"
    )
    repo = _lambda_cog(tmp_path, **{"infra/queue.tf": queue})
    assert "redrive_policy" in _messages(check_pipe_017(repo))


@pytest.mark.parametrize(
    ("visibility", "timeout", "passes"),
    [
        ("var.worker_timeout_seconds + 60", "var.worker_timeout_seconds", True),
        ("960", "900", True),
        ("900", "900", False),
        ("300", "900", False),
        # Referencing the variable alone is not "derived": equal is not longer.
        ("var.worker_timeout_seconds", "var.worker_timeout_seconds", False),
        # A literal against a variable cannot be confirmed from source.
        ("960", "var.worker_timeout_seconds", False),
        ("var.visibility", "var.worker_timeout_seconds", False),
    ],
)
def test_pipe017_visibility_timeout_must_outlast_the_function(
    tmp_path: Path, visibility: str, timeout: str, passes: bool
) -> None:
    queue = _QUEUE_TF.replace("var.worker_timeout_seconds + 60", visibility)
    worker = _WORKER_TF.replace(
        "timeout       = var.worker_timeout_seconds", f"timeout       = {timeout}"
    )
    repo = _lambda_cog(tmp_path, **{"infra/queue.tf": queue, "infra/worker.tf": worker})
    flagged = "visibility_timeout_seconds" in _messages(check_pipe_017(repo))
    assert flagged is not passes


def test_pipe017_flags_handler_that_never_returns_batch_item_failures(
    tmp_path: Path,
) -> None:
    handler = "def lambda_handler(event, context):\n    return None\n"
    repo = _lambda_cog(tmp_path, **{"src/demo_cog/worker.py": handler})
    assert "batchItemFailures" in _messages(check_pipe_017(repo))


# --- PIPE-018 -----------------------------------------------------------------


def _without_scaling_config(worker: str) -> str:
    return worker.replace("  scaling_config {\n    maximum_concurrency = 2\n  }\n", "")


def test_pipe018_flags_no_ceiling(tmp_path: Path) -> None:
    repo = _lambda_cog(
        tmp_path, **{"infra/worker.tf": _without_scaling_config(_WORKER_TF)}
    )
    findings = check_pipe_018(repo)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "PIPE-018"


def test_pipe018_accepts_reserved_concurrency_instead(tmp_path: Path) -> None:
    worker = _without_scaling_config(_WORKER_TF).replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n  reserved_concurrent_executions = 1\n",
    )
    assert check_pipe_018(_lambda_cog(tmp_path, **{"infra/worker.tf": worker})) == []


def test_pipe018_does_not_count_unreserved_minus_one(tmp_path: Path) -> None:
    """-1 is Lambda's spelling of "no reservation", not a ceiling."""
    worker = _without_scaling_config(_WORKER_TF).replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n  reserved_concurrent_executions = -1\n",
    )
    assert check_pipe_018(_lambda_cog(tmp_path, **{"infra/worker.tf": worker}))


def test_pipe018_flags_a_reservation_below_the_mapping_ceiling(
    tmp_path: Path,
) -> None:
    """AWS rejects this pair at create time; the stack only applies until it is."""
    worker = _WORKER_TF.replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n  reserved_concurrent_executions = 1\n",
    )
    findings = check_pipe_018(_lambda_cog(tmp_path, **{"infra/worker.tf": worker}))
    assert len(findings) == 1
    text = findings[0]["finding"]
    assert "reserved_concurrent_executions = 1" in text
    assert "maximum_concurrency = 2" in text


def test_pipe018_reads_a_reservation_through_its_variable_default(
    tmp_path: Path,
) -> None:
    """The reservation is written as var.reserved_concurrency in every cog."""
    worker = _WORKER_TF.replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n"
        "  reserved_concurrent_executions = var.reserved_concurrency\n",
    )
    variables = 'variable "reserved_concurrency" {\n  default = 1\n}\n'
    repo = _lambda_cog(
        tmp_path,
        **{"infra/worker.tf": worker, "infra/variables.tf": variables},
    )
    assert "AWS rejects" in _messages(check_pipe_018(repo))


def test_pipe018_accepts_a_reservation_above_the_mapping_ceiling(
    tmp_path: Path,
) -> None:
    """The pair is only wrong when the reservation is the lower of the two."""
    worker = _WORKER_TF.replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n  reserved_concurrent_executions = 10\n",
    )
    assert check_pipe_018(_lambda_cog(tmp_path, **{"infra/worker.tf": worker})) == []


def test_pipe018_says_nothing_about_a_pair_it_cannot_read(tmp_path: Path) -> None:
    """A variable with no default is not a number this check may compare."""
    worker = _WORKER_TF.replace(
        "  memory_size   = 1024\n",
        "  memory_size   = 1024\n"
        "  reserved_concurrent_executions = var.reserved_concurrency\n",
    )
    variables = 'variable "reserved_concurrency" {\n  type = number\n}\n'
    repo = _lambda_cog(
        tmp_path,
        **{"infra/worker.tf": worker, "infra/variables.tf": variables},
    )
    assert check_pipe_018(repo) == []


# --- PIPE-020 -----------------------------------------------------------------


def test_pipe020_flags_a_handler_that_never_reads_the_remaining_time(
    tmp_path: Path,
) -> None:
    handler = "def lambda_handler(event, context):\n    return {}\n"
    repo = _lambda_cog(tmp_path, **{"src/demo_cog/worker.py": handler})
    findings = check_pipe_020(repo)
    assert len(findings) == 1
    assert "get_remaining_time_in_millis" in findings[0]["finding"]


def test_pipe020_accepts_the_read_from_a_helper_module(tmp_path: Path) -> None:
    """The deadline lives in its own module in every cog that has one."""
    repo = _lambda_cog(
        tmp_path,
        **{
            "src/demo_cog/worker.py": "def lambda_handler(event, context):\n"
            "    return {}\n",
            "src/demo_cog/_deadline.py": "def deadline(context):\n"
            "    return context.get_remaining_time_in_millis()\n",
        },
    )
    assert check_pipe_020(repo) == []


def test_pipe020_says_nothing_about_a_repo_with_no_source(tmp_path: Path) -> None:
    assert check_pipe_020(tmp_path) == []


# --- CD-027 -------------------------------------------------------------------


def test_cd027_flags_infra_that_ci_never_checks(tmp_path: Path) -> None:
    ci = _CI_YML.replace("    with:\n      terraform-dir: infra\n", "")
    findings = check_terraform_checked_in_ci(
        _lambda_cog(tmp_path, **{".github/workflows/ci.yml": ci})
    )
    assert len(findings) == 1
    assert "terraform-dir" in findings[0]["finding"]


def test_cd027_accepts_terraform_run_inline(tmp_path: Path) -> None:
    ci = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: terraform fmt -check -recursive
      - run: terraform validate
"""
    repo = _lambda_cog(tmp_path, **{".github/workflows/ci.yml": ci})
    assert check_terraform_checked_in_ci(repo) == []


def test_cd027_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _CI_YML.replace(
            "      terraform-dir: infra\n", "      python-version: '3.11'\n"
        ),
    )
    assert check_terraform_checked_in_ci(tmp_path) == []


# --- PIPE-019 -----------------------------------------------------------------


def test_pipe019_passes_a_trigger_that_posts_to_the_api(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/watch/trigger.py",
        "def fire(client):\n    client.post('/v1/deejay/runs', json={})\n",
    )
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["httpx"]\n')
    assert check_pipe_019(tmp_path) == []


def test_pipe019_flags_prefect_client_calls(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/watch/trigger.py",
        "from prefect.deployments import run_deployment\n"
        "def fire():\n    run_deployment('x/y')\n",
    )
    text = _messages(check_pipe_019(tmp_path))
    assert "run_deployment" in text


def test_pipe019_flags_prefect_dependency(tmp_path: Path) -> None:
    _write(tmp_path, "src/watch/__init__.py", "")
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["prefect>=3"]\n')
    assert "Prefect" in _messages(check_pipe_019(tmp_path))


def test_pipe019_flags_direct_sqs_send(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/watch/trigger.py",
        "import boto3\n"
        "def fire():\n"
        "    boto3.client('sqs').send_message(QueueUrl='u', MessageBody='{}')\n",
    )
    assert "sends to SQS directly" in _messages(check_pipe_019(tmp_path))


# --- PIPE-007 -----------------------------------------------------------------


def _module(tmp_path: Path, body: str) -> Path:
    _write(tmp_path, "src/demo_cog/calls.py", body)
    return tmp_path


def test_pipe007_flags_a_bare_direct_call(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\n\ndef fetch(url):\n    return httpx.get(url).json()\n",
    )
    findings = check_retry_logic(repo)
    assert len(findings) == 1
    assert "calls.py::fetch" in findings[0]["finding"]


def test_pipe007_flags_a_client_built_outside_any_loop(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\n\n"
        "def send(url, body):\n"
        "    with httpx.Client() as client:\n"
        "        return client.post(url, json=body)\n",
    )
    assert len(check_retry_logic(repo)) == 1


def test_pipe007_accepts_tenacity(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\nfrom tenacity import retry, stop_after_attempt\n\n"
        "@retry(stop=stop_after_attempt(3))\n"
        "def fetch(url):\n    return httpx.get(url)\n",
    )
    assert check_retry_logic(repo) == []


def test_pipe007_accepts_an_attempt_loop(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\n\n"
        "def fetch(url):\n"
        "    for attempt in range(3):\n"
        "        try:\n"
        "            return httpx.get(url)\n"
        "        except httpx.TransportError:\n"
        "            continue\n",
    )
    assert check_retry_logic(repo) == []


def test_pipe007_accepts_a_no_retry_comment(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\n\n"
        "def ping(url):\n"
        "    # no-retry: a missed heartbeat is reported by its absence\n"
        "    httpx.get(url)\n",
    )
    assert check_retry_logic(repo) == []


def test_pipe007_a_loop_outside_a_closure_does_not_cover_it(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "import httpx\n\n"
        "def outer(urls):\n"
        "    for u in urls:\n"
        "        def inner():\n"
        "            return httpx.get(u)\n"
        "        inner()\n",
    )
    findings = check_retry_logic(repo)
    assert [f["finding"].split("::")[1].split(" ")[0] for f in findings] == ["inner"]


def test_pipe007_ignores_calls_through_shared_clients(tmp_path: Path) -> None:
    repo = _module(
        tmp_path,
        "from mini_app_polis.api import KaianoApiClient\n\n"
        "def post(body):\n"
        "    KaianoApiClient.from_env().post('/v1/x', json=body)\n",
    )
    assert check_retry_logic(repo) == []


# --- CD-010 and CD-024, pipeline-cog branches --------------------------------


def _layer1(repo: Path) -> list[dict]:
    return [
        f
        for f in check_three_layer_observability(repo, cog_subtype="pipeline")
        if "Layer 1" in f["finding"]
    ]


def test_cd010_pipeline_layer1_is_not_healthchecks(tmp_path: Path) -> None:
    """A queue-driven cog has nothing to ping between jobs."""
    repo = _lambda_cog(tmp_path)
    assert "HEALTHCHECKS" not in (repo / "pyproject.toml").read_text()
    assert _layer1(repo) == []


def test_cd010_pipeline_flags_alarm_without_actions(tmp_path: Path) -> None:
    alarm = _ALARM_TF.replace("[aws_sns_topic.alerts.arn]", "[]")
    repo = _lambda_cog(tmp_path, **{"infra/account.tf": alarm})
    assert len(_layer1(repo)) == 1


def test_cd010_pipeline_flags_alarm_on_the_work_queue(tmp_path: Path) -> None:
    """An alarm on the work queue fires on ordinary backlog, not on failure."""
    alarm = _ALARM_TF.replace("aws_sqs_queue.dlq.name", "aws_sqs_queue.jobs.name")
    repo = _lambda_cog(tmp_path, **{"infra/account.tf": alarm})
    assert len(_layer1(repo)) == 1


def test_cd010_trigger_still_needs_healthchecks(tmp_path: Path) -> None:
    _write(tmp_path, "src/watch/__init__.py", "")
    findings = check_three_layer_observability(tmp_path, cog_subtype="trigger")
    assert any("HEALTHCHECKS_URL" in f["finding"] for f in findings)


def test_cd024_pipeline_reads_the_function_not_railway(tmp_path: Path) -> None:
    worker = _WORKER_TF.replace("  memory_size   = 1024\n", "")
    repo = _lambda_cog(tmp_path, **{"infra/worker.tf": worker})
    findings = check_cd_024(repo, repo_type="pipeline-cog")
    assert len(findings) == 1
    assert "memory_size" in findings[0]["finding"]
    assert "railway" not in findings[0]["finding"].lower()


def test_cd024_pipeline_without_a_function(tmp_path: Path) -> None:
    findings = check_cd_024(tmp_path, repo_type="pipeline-cog")
    assert len(findings) == 1
    assert "aws_lambda_function" in findings[0]["finding"]


def test_cd024_other_types_still_read_railway(tmp_path: Path) -> None:
    findings = check_cd_024(tmp_path, repo_type="api-service")
    assert "railway" in findings[0]["finding"].lower()


# --- CD-028 -------------------------------------------------------------------


def test_cd028_flags_a_provider_with_no_version(tmp_path: Path) -> None:
    versions = _VERSIONS_TF.replace('    version = "~> 5.0"\n', "")
    repo = _lambda_cog(tmp_path, **{"infra/versions.tf": versions})
    assert "no version constraint" in _messages(check_terraform_versions_pinned(repo))


def test_cd028_flags_a_missing_required_version(tmp_path: Path) -> None:
    versions = _VERSIONS_TF.replace('  required_version = ">= 1.6"\n', "")
    repo = _lambda_cog(tmp_path, **{"infra/versions.tf": versions})
    assert "required_version" in _messages(check_terraform_versions_pinned(repo))


def test_cd028_flags_an_uncommitted_lock(tmp_path: Path) -> None:
    repo = _lambda_cog(tmp_path)
    (repo / "infra" / ".terraform.lock.hcl").unlink()
    findings = check_terraform_versions_pinned(repo)
    assert len(findings) == 1
    assert ".terraform.lock.hcl" in findings[0]["finding"]


def test_cd028_flags_a_single_platform_lock_when_ci_runs_terraform(
    tmp_path: Path,
) -> None:
    """The failure all three cogs hit the day Terraform reached CI."""
    lock = _LOCK_HCL.replace('    "h1:linux_amd64_placeholder",\n', "")
    repo = _lambda_cog(tmp_path, **{"infra/.terraform.lock.hcl": lock})
    text = _messages(check_terraform_versions_pinned(repo))
    assert "1 platform hash" in text


def test_cd028_accepts_a_single_platform_lock_when_ci_does_not(
    tmp_path: Path,
) -> None:
    """A stack applied only from one workstation needs only that platform."""
    lock = _LOCK_HCL.replace('    "h1:linux_amd64_placeholder",\n', "")
    ci = _CI_YML.replace("    with:\n      terraform-dir: infra\n", "")
    repo = _lambda_cog(
        tmp_path,
        **{"infra/.terraform.lock.hcl": lock, ".github/workflows/ci.yml": ci},
    )
    assert check_terraform_versions_pinned(repo) == []


def test_cd028_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    assert check_terraform_versions_pinned(tmp_path) == []


# --- SEC-008 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "terraform.tfvars",
        "prod.auto.tfvars",
        "tfplan",
    ],
)
def test_sec008_flags_a_committed_state_or_variable_file(
    tmp_path: Path, name: str
) -> None:
    """No .git here, so every file present is tracked by construction."""
    repo = _lambda_cog(tmp_path, **{f"infra/{name}": "x = 1\n"})
    findings = check_sec_008(repo)
    assert len(findings) == 1
    assert name in findings[0]["finding"]
    assert findings[0]["severity"] == "ERROR"


def test_sec008_does_not_flag_the_committed_template_or_lock(tmp_path: Path) -> None:
    repo = _lambda_cog(
        tmp_path,
        **{"infra/terraform.tfvars.example": 'name_prefix = "demo"\n'},
    )
    assert check_sec_008(repo) == []


def test_sec008_requires_an_infra_gitignore(tmp_path: Path) -> None:
    repo = _lambda_cog(tmp_path)
    (repo / "infra" / ".gitignore").unlink()
    findings = check_sec_008(repo)
    assert len(findings) == 1
    assert ".gitignore" in findings[0]["finding"]


def test_sec008_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    assert check_sec_008(tmp_path) == []
