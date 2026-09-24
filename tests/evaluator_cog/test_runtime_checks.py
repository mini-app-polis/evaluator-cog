"""Deterministic checks for the queue-and-Lambda runtime (ADR-009, ADR-010).

The runtime is split between two kinds of repository, each evaluated on its
own (ADR-010). A pipeline cog carries its code: PIPE-016 (Prefect is gone),
PIPE-017's code half (the handler names failed records), PIPE-020, PIPE-007.
mini-app-polis/infra — type ``infrastructure`` — carries every cog's
Terraform: PIPE-017's infrastructure half, PIPE-018 (one concurrency ceiling
per cog), CD-010's dead-letter-queue alarm, CD-024 (limits), CD-027 (checked
in CI), CD-028 (pinned, locked) and SEC-008 (no state committed). PIPE-019 is
the trigger cogs'.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic._terraform import (
    dead_letter_queue_names,
    module_calls,
    of_type,
    terraform_resources,
)
from evaluator_cog.engine.deterministic.containers import check_cd_024
from evaluator_cog.engine.deterministic.delivery import (
    check_cd_010_infrastructure,
    check_ci,
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

INFRA = "infrastructure"
MOD = "modules/cog-worker"

_QUEUE_TF = """
resource "aws_sqs_queue" "dlq" {
  name = "${var.name}-jobs-dlq" # braces inside a string
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name}-jobs"
  visibility_timeout_seconds = var.timeout_seconds + 60
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}
"""

_WORKER_TF = """
resource "aws_lambda_function" "worker" {
  function_name = "${var.name}-worker"
  timeout       = var.timeout_seconds
  memory_size   = var.memory_mb
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
  dynamic "scaling_config" {
    for_each = var.max_concurrency == null ? [] : [var.max_concurrency]
    content {
      maximum_concurrency = scaling_config.value
    }
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
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
"""

_COGS_TF = """
module "alpha" {
  source          = "./modules/cog-worker"
  name            = "alpha"
  timeout_seconds = 300
  memory_mb       = 1024
  max_concurrency = 4
}

module "beta" {
  source               = "./modules/cog-worker"
  name                 = "beta"
  timeout_seconds      = 900
  memory_mb            = 512
  reserved_concurrency = 1
}
"""

#: Two platforms' hashes, which is what a stack checked in CI needs.
_LOCK_HCL = """
provider "registry.terraform.io/hashicorp/aws" {
  version     = "5.100.0"
  constraints = "~> 5.0"
  hashes = [
    "h1:darwin_arm64_placeholder",
    "h1:linux_amd64_placeholder",
  ]
}
"""

_TERRAFORM_YML = """
name: terraform
on: [pull_request]
jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - run: terraform fmt -check -recursive -diff
      - run: terraform init
      - run: terraform validate
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
    uses: mini-app-polis/.github/.github/workflows/python-test.yml@v3
"""


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _lambda_cog(repo: Path, **overrides: str) -> Path:
    """A pipeline cog that satisfies its runtime rules: code only, no infra/."""
    files = {
        "src/demo_cog/worker.py": _HANDLER,
        ".github/workflows/ci.yml": _CI_YML,
        "pyproject.toml": '[project]\nname = "demo-cog"\ndependencies = ["httpx"]\n',
    }
    files.update(overrides)
    for rel, body in files.items():
        _write(repo, rel, body)
    return repo


def _infra_repo(repo: Path, **overrides: str) -> Path:
    """An infrastructure repository that satisfies every rule scoped to it."""
    files = {
        "versions.tf": _VERSIONS_TF,
        "cogs.tf": _COGS_TF,
        f"{MOD}/queue.tf": _QUEUE_TF,
        f"{MOD}/worker.tf": _WORKER_TF,
        f"{MOD}/alarm.tf": _ALARM_TF,
        f"{MOD}/versions.tf": _VERSIONS_TF,
        ".terraform.lock.hcl": _LOCK_HCL,
        ".gitignore": ".terraform/\n*.tfstate\n*.tfstate.*\ntfplan\n",
        ".github/workflows/terraform.yml": _TERRAFORM_YML,
    }
    files.update(overrides)
    for rel, body in files.items():
        _write(repo, rel, body)
    return repo


def _messages(findings: list[dict]) -> str:
    return " | ".join(f["finding"] for f in findings)


# --- the Terraform reader ----------------------------------------------------


def test_reader_sees_through_strings_and_comments(tmp_path: Path) -> None:
    resources = terraform_resources(_infra_repo(tmp_path), INFRA)
    assert resources is not None
    assert sorted((r.type, r.name) for r in resources) == [
        ("aws_cloudwatch_metric_alarm", "dlq_not_empty"),
        ("aws_lambda_event_source_mapping", "jobs"),
        ("aws_lambda_function", "worker"),
        ("aws_sqs_queue", "dlq"),
        ("aws_sqs_queue", "jobs"),
    ]
    fn = of_type(resources, "aws_lambda_function")[0]
    # The URL's `//` is not a comment, so the closing braces after it survive
    # and the function's block ends where it should.
    assert fn.attr("memory_size") == "var.memory_mb"
    assert fn.file == f"{MOD}/worker.tf"
    assert "ReportBatchItemFailures" not in fn.body
    assert dead_letter_queue_names(resources) == {"dlq"}


def test_reader_reads_the_module_calls_at_the_root(tmp_path: Path) -> None:
    calls = module_calls(_infra_repo(tmp_path), INFRA)
    assert [(c.name, c.attr("source")) for c in calls] == [
        ("alpha", '"./modules/cog-worker"'),
        ("beta", '"./modules/cog-worker"'),
    ]
    assert calls[1].attr("reserved_concurrency") == "1"


def test_reader_skips_the_terraform_cache(tmp_path: Path) -> None:
    repo = _infra_repo(
        tmp_path,
        **{".terraform/modules/x/main.tf": 'resource "aws_sqs_queue" "cached" {}\n'},
    )
    names = {r.name for r in terraform_resources(repo, INFRA) or []}
    assert "cached" not in names


def test_reader_distinguishes_no_terraform_from_empty(tmp_path: Path) -> None:
    """Other repository types keep Terraform in infra/, and may have none."""
    assert terraform_resources(tmp_path) is None
    (tmp_path / "infra").mkdir()
    assert terraform_resources(tmp_path) == []
    _write(tmp_path, "infra/queue.tf", _QUEUE_TF)
    assert [r.name for r in terraform_resources(tmp_path) or []] == ["dlq", "jobs"]


# --- the reference repositories pass everything -------------------------------


def test_compliant_lambda_cog_passes_its_runtime_rules(tmp_path: Path) -> None:
    repo = _lambda_cog(tmp_path)
    assert check_pipe_016(repo) == []
    assert check_pipe_017(repo) == []
    assert check_pipe_020(repo) == []
    # Declared in mini-app-polis/infra, so not the cog's to answer for.
    assert check_cd_024(repo, repo_type="pipeline-cog") == []
    assert check_terraform_checked_in_ci(repo) == []
    assert check_terraform_versions_pinned(repo) == []
    assert check_sec_008(repo) == []
    layer1 = [
        f
        for f in check_three_layer_observability(repo, cog_subtype="pipeline")
        if "Layer 1" in f["finding"]
    ]
    assert layer1 == []


def test_compliant_infra_repo_passes_every_rule_scoped_to_it(tmp_path: Path) -> None:
    repo = _infra_repo(tmp_path)
    assert check_pipe_017(repo, repo_type=INFRA) == []
    assert check_pipe_018(repo, repo_type=INFRA) == []
    assert check_cd_010_infrastructure(repo) == []
    assert check_cd_024(repo, repo_type=INFRA) == []
    assert check_terraform_checked_in_ci(repo, repo_type=INFRA) == []
    assert check_terraform_versions_pinned(repo, repo_type=INFRA) == []
    assert check_sec_008(repo, repo_type=INFRA) == []


# --- PIPE-016 -----------------------------------------------------------------


def test_pipe016_does_not_look_for_infra_in_the_cog(tmp_path: Path) -> None:
    """The runtime is declared in mini-app-polis/infra (ADR-010)."""
    assert check_pipe_016(_lambda_cog(tmp_path)) == []
    assert not (tmp_path / "infra").exists()


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


def test_pipe017_cog_half_flags_a_handler_that_never_returns_failures(
    tmp_path: Path,
) -> None:
    handler = "def lambda_handler(event, context):\n    return None\n"
    repo = _lambda_cog(tmp_path, **{"src/demo_cog/worker.py": handler})
    findings = check_pipe_017(repo)
    assert len(findings) == 1
    assert "batchItemFailures" in findings[0]["finding"]


def test_pipe017_infra_half_ignores_source(tmp_path: Path) -> None:
    """The infrastructure repository has no handler to read."""
    assert check_pipe_017(_infra_repo(tmp_path), repo_type=INFRA) == []


def test_pipe017_infra_half_is_silent_without_a_mapping(tmp_path: Path) -> None:
    repo = _infra_repo(
        tmp_path,
        **{
            f"{MOD}/worker.tf": _WORKER_TF.split('resource "aws_lambda_event_source')[0]
        },
    )
    assert check_pipe_017(repo, repo_type=INFRA) == []


def test_pipe017_flags_mapping_without_report_batch_item_failures(
    tmp_path: Path,
) -> None:
    worker = _WORKER_TF.replace(
        '  function_response_types = ["ReportBatchItemFailures"]\n', ""
    )
    repo = _infra_repo(tmp_path, **{f"{MOD}/worker.tf": worker})
    findings = check_pipe_017(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert "ReportBatchItemFailures" in findings[0]["finding"]


def test_pipe017_flags_queue_without_redrive(tmp_path: Path) -> None:
    queue = (
        'resource "aws_sqs_queue" "jobs" {\n'
        "  visibility_timeout_seconds = var.timeout_seconds + 60\n"
        "}\n"
    )
    repo = _infra_repo(tmp_path, **{f"{MOD}/queue.tf": queue})
    assert "redrive_policy" in _messages(check_pipe_017(repo, repo_type=INFRA))


@pytest.mark.parametrize(
    ("visibility", "timeout", "passes"),
    [
        ("var.timeout_seconds + 60", "var.timeout_seconds", True),
        ("960", "900", True),
        ("900", "900", False),
        ("300", "900", False),
        # Referencing the variable alone is not "derived": equal is not longer.
        ("var.timeout_seconds", "var.timeout_seconds", False),
        # A literal against a variable cannot be confirmed from source.
        ("960", "var.timeout_seconds", False),
        ("var.visibility", "var.timeout_seconds", False),
    ],
)
def test_pipe017_visibility_timeout_must_outlast_the_function(
    tmp_path: Path, visibility: str, timeout: str, passes: bool
) -> None:
    queue = _QUEUE_TF.replace("var.timeout_seconds + 60", visibility)
    worker = _WORKER_TF.replace(
        "timeout       = var.timeout_seconds", f"timeout       = {timeout}"
    )
    repo = _infra_repo(
        tmp_path, **{f"{MOD}/queue.tf": queue, f"{MOD}/worker.tf": worker}
    )
    flagged = "visibility_timeout_seconds" in _messages(
        check_pipe_017(repo, repo_type=INFRA)
    )
    assert flagged is not passes


# --- PIPE-018 -----------------------------------------------------------------


def _one_cog(**settings: str) -> str:
    body = "".join(f"  {k} = {v}\n" for k, v in settings.items())
    return (
        'module "gamma" {\n  source = "./modules/cog-worker"\n'
        '  name = "gamma"\n' + body + "}\n"
    )


def test_pipe018_flags_a_cog_with_no_ceiling(tmp_path: Path) -> None:
    repo = _infra_repo(tmp_path, **{"cogs.tf": _one_cog()})
    findings = check_pipe_018(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert 'module "gamma"' in findings[0]["finding"]
    assert "neither" in findings[0]["finding"]


def test_pipe018_flags_a_cog_with_both(tmp_path: Path) -> None:
    """AWS rejects a mapping maximum above the function's reservation."""
    repo = _infra_repo(
        tmp_path,
        **{"cogs.tf": _one_cog(reserved_concurrency="1", max_concurrency="2")},
    )
    assert "both" in _messages(check_pipe_018(repo, repo_type=INFRA))


@pytest.mark.parametrize("value", ["-1", "0"])
def test_pipe018_does_not_count_an_unreserving_value(
    tmp_path: Path, value: str
) -> None:
    """-1 is "no reservation"; 0 stops the function. Neither is a ceiling."""
    repo = _infra_repo(tmp_path, **{"cogs.tf": _one_cog(reserved_concurrency=value)})
    assert check_pipe_018(repo, repo_type=INFRA)


@pytest.mark.parametrize(
    "settings",
    [
        {"reserved_concurrency": "1"},
        {"max_concurrency": "4"},
        {"reserved_concurrency": "-1", "max_concurrency": "4"},
        {"max_concurrency": "4", "reserved_concurrency": "null"},
    ],
)
def test_pipe018_accepts_exactly_one_ceiling(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repo = _infra_repo(tmp_path, **{"cogs.tf": _one_cog(**settings)})
    assert check_pipe_018(repo, repo_type=INFRA) == []


def test_pipe018_only_reads_calls_to_the_cog_module(tmp_path: Path) -> None:
    other = 'module "network" {\n  source = "./modules/network"\n}\n'
    repo = _infra_repo(tmp_path, **{"cogs.tf": _COGS_TF + other})
    assert check_pipe_018(repo, repo_type=INFRA) == []


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


def test_cd027_accepts_terraform_checked_in_any_workflow(tmp_path: Path) -> None:
    """mini-app-polis/infra checks Terraform in terraform.yml, not ci.yml."""
    repo = _infra_repo(tmp_path)
    assert not (repo / ".github/workflows/ci.yml").exists()
    assert check_terraform_checked_in_ci(repo, repo_type=INFRA) == []


def test_cd027_flags_terraform_that_no_workflow_checks(tmp_path: Path) -> None:
    workflow = _TERRAFORM_YML.replace("      - run: terraform validate\n", "")
    repo = _infra_repo(tmp_path, **{".github/workflows/terraform.yml": workflow})
    findings = check_terraform_checked_in_ci(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert "The repository declares Terraform" in findings[0]["finding"]


def test_cd027_accepts_delegation_to_the_shared_workflow(tmp_path: Path) -> None:
    """A repository keeping Terraform in infra/ may hand it to python-test.yml."""
    _write(tmp_path, "infra/queue.tf", _QUEUE_TF)
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        _CI_YML + "    with:\n      terraform-dir: infra\n",
    )
    assert check_terraform_checked_in_ci(tmp_path) == []


def test_cd027_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    assert check_terraform_checked_in_ci(_lambda_cog(tmp_path)) == []


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


# --- CD-010 and CD-024 --------------------------------------------------------


def test_cd010_pipeline_cog_does_not_carry_the_dlq_layer(tmp_path: Path) -> None:
    """The alarm is declared in mini-app-polis/infra and checked there."""
    findings = check_three_layer_observability(
        _lambda_cog(tmp_path), cog_subtype="pipeline"
    )
    assert not any("Layer 1" in f["finding"] for f in findings)


def test_cd010_infra_flags_alarm_without_actions(tmp_path: Path) -> None:
    alarm = _ALARM_TF.replace("[aws_sns_topic.alerts.arn]", "[]")
    repo = _infra_repo(tmp_path, **{f"{MOD}/alarm.tf": alarm})
    findings = check_cd_010_infrastructure(repo)
    assert len(findings) == 1
    assert "Layer 1" in findings[0]["finding"]


def test_cd010_infra_flags_alarm_on_the_work_queue(tmp_path: Path) -> None:
    """An alarm on the work queue fires on ordinary backlog, not on failure."""
    alarm = _ALARM_TF.replace("aws_sqs_queue.dlq.name", "aws_sqs_queue.jobs.name")
    repo = _infra_repo(tmp_path, **{f"{MOD}/alarm.tf": alarm})
    assert len(check_cd_010_infrastructure(repo)) == 1


def test_cd010_infra_without_a_queue_worker_has_nothing_to_alarm_on(
    tmp_path: Path,
) -> None:
    repo = _infra_repo(
        tmp_path,
        **{
            f"{MOD}/worker.tf": _WORKER_TF.split('resource "aws_lambda_event_source')[0]
        },
    )
    assert check_cd_010_infrastructure(repo) == []


def test_cd010_trigger_still_needs_healthchecks(tmp_path: Path) -> None:
    _write(tmp_path, "src/watch/__init__.py", "")
    findings = check_three_layer_observability(tmp_path, cog_subtype="trigger")
    assert any("HEALTHCHECKS_URL" in f["finding"] for f in findings)


def test_cd024_pipeline_cog_is_answered_in_infra(tmp_path: Path) -> None:
    """Not a Railway finding, and not the cog's: its limits are in infra."""
    assert check_cd_024(tmp_path, repo_type="pipeline-cog") == []


def test_cd024_infra_flags_a_function_without_limits(tmp_path: Path) -> None:
    worker = _WORKER_TF.replace("  memory_size   = var.memory_mb\n", "")
    repo = _infra_repo(tmp_path, **{f"{MOD}/worker.tf": worker})
    findings = check_cd_024(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert "memory_size" in findings[0]["finding"]
    assert "railway" not in findings[0]["finding"].lower()


def test_cd024_infra_flags_a_cog_without_its_limits(tmp_path: Path) -> None:
    repo = _infra_repo(
        tmp_path, **{"cogs.tf": _one_cog(max_concurrency="4", memory_mb="512")}
    )
    findings = check_cd_024(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert 'module "gamma"' in findings[0]["finding"]
    assert "timeout_seconds" in findings[0]["finding"]


def test_cd024_other_types_still_read_railway(tmp_path: Path) -> None:
    findings = check_cd_024(tmp_path, repo_type="api-service")
    assert "railway" in findings[0]["finding"].lower()


# --- CD-028 -------------------------------------------------------------------


def test_cd028_flags_a_provider_with_no_version(tmp_path: Path) -> None:
    versions = _VERSIONS_TF.replace('      version = "~> 5.0"\n', "")
    repo = _infra_repo(tmp_path, **{"versions.tf": versions})
    text = _messages(check_terraform_versions_pinned(repo, repo_type=INFRA))
    assert "no version constraint" in text


def test_cd028_reads_the_modules_providers_too(tmp_path: Path) -> None:
    """An unpinned provider in a module widens what the root can resolve."""
    versions = _VERSIONS_TF.replace('      version = "~> 5.0"\n', "")
    repo = _infra_repo(tmp_path, **{f"{MOD}/versions.tf": versions})
    text = _messages(check_terraform_versions_pinned(repo, repo_type=INFRA))
    assert "no version constraint" in text


def test_cd028_flags_a_missing_required_version(tmp_path: Path) -> None:
    versions = _VERSIONS_TF.replace('  required_version = ">= 1.11"\n', "")
    repo = _infra_repo(tmp_path, **{"versions.tf": versions})
    text = _messages(check_terraform_versions_pinned(repo, repo_type=INFRA))
    assert "required_version" in text
    assert "the repository root" in text


def test_cd028_flags_an_uncommitted_lock(tmp_path: Path) -> None:
    repo = _infra_repo(tmp_path)
    (repo / ".terraform.lock.hcl").unlink()
    findings = check_terraform_versions_pinned(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert ".terraform.lock.hcl" in findings[0]["finding"]


def test_cd028_flags_a_single_platform_lock_when_ci_runs_terraform(
    tmp_path: Path,
) -> None:
    """The failure all three cogs hit the day Terraform reached CI."""
    lock = _LOCK_HCL.replace('    "h1:linux_amd64_placeholder",\n', "")
    repo = _infra_repo(tmp_path, **{".terraform.lock.hcl": lock})
    text = _messages(check_terraform_versions_pinned(repo, repo_type=INFRA))
    assert "1 platform hash" in text


def test_cd028_accepts_a_single_platform_lock_when_ci_does_not(
    tmp_path: Path,
) -> None:
    """A stack applied only from one workstation needs only that platform."""
    repo = _infra_repo(
        tmp_path,
        **{
            ".terraform.lock.hcl": _LOCK_HCL.replace(
                '    "h1:linux_amd64_placeholder",\n', ""
            )
        },
    )
    (repo / ".github/workflows/terraform.yml").unlink()
    assert check_terraform_versions_pinned(repo, repo_type=INFRA) == []


def test_cd028_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    assert check_terraform_versions_pinned(_lambda_cog(tmp_path)) == []


# --- SEC-008 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "terraform.tfvars",
        "prod.auto.tfvars",
        "tfplan",
        f"{MOD}/terraform.tfstate",
    ],
)
def test_sec008_flags_a_committed_state_or_variable_file(
    tmp_path: Path, path: str
) -> None:
    """No .git here, so every file present is tracked by construction."""
    repo = _infra_repo(tmp_path, **{path: "x = 1\n"})
    findings = check_sec_008(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert path in findings[0]["finding"]
    assert findings[0]["severity"] == "ERROR"


def test_sec008_does_not_flag_the_committed_template_or_lock(tmp_path: Path) -> None:
    repo = _infra_repo(
        tmp_path, **{"terraform.tfvars.example": 'region = "us-east-1"\n'}
    )
    assert check_sec_008(repo, repo_type=INFRA) == []


def test_sec008_requires_a_gitignore_at_the_terraform_root(tmp_path: Path) -> None:
    repo = _infra_repo(tmp_path)
    (repo / ".gitignore").unlink()
    findings = check_sec_008(repo, repo_type=INFRA)
    assert len(findings) == 1
    assert ".gitignore" in findings[0]["finding"]


def test_sec008_still_reads_infra_for_other_types(tmp_path: Path) -> None:
    _write(tmp_path, "infra/queue.tf", _QUEUE_TF)
    _write(tmp_path, "infra/.gitignore", "*.tfstate\n")
    _write(tmp_path, "infra/terraform.tfstate", "{}")
    assert "infra/terraform.tfstate" in _messages(check_sec_008(tmp_path))


def test_sec008_skips_a_repo_with_no_terraform(tmp_path: Path) -> None:
    assert check_sec_008(_lambda_cog(tmp_path)) == []


# --- VER-003 scope ------------------------------------------------------------


def test_ver003_missing_ci_yml_respects_the_rules_scope(tmp_path: Path) -> None:
    """An infrastructure repository has no releases and no ci.yml to cut them."""
    repo = _infra_repo(tmp_path)
    assert check_ci(repo, exceptions=frozenset({"VER-003"})) == []
    assert "ci.yml not found" in _messages(check_ci(repo))
