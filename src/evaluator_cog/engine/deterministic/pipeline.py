"""Pipeline and trigger cog rule checks: runtime, retry, triggers, evaluation."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
    _is_checker_self_source,
    production_python_text,
)
from evaluator_cog.engine.deterministic._terraform import (
    module_calls,
    of_type,
    terraform_resources,
)


def check_healthchecks_integration(
    repo_path: Path,
    cog_subtype: str | None = None,
) -> list[Finding]:
    """CD-007: Healthchecks.io for trigger cogs."""
    CHECK_ID = "CD-007"
    findings: list[Finding] = []
    if cog_subtype != "trigger":
        return findings
    env_example = repo_path / ".env.example"
    env_text = env_example.read_text() if env_example.exists() else ""
    src_text = production_python_text(repo_path) if (repo_path / "src").is_dir() else ""
    if "HEALTHCHECKS_URL_" not in env_text or (
        "HEALTHCHECKS_URL_" not in src_text and "healthchecks" not in src_text.lower()
    ):
        findings.append(
            _finding(
                "CD-007",
                "WARN",
                "cd_readiness",
                "Trigger cog is missing Healthchecks.io integration signals.",
                "Declare HEALTHCHECKS_URL_<SERVICE> in .env.example and ping it in trigger loop code.",
            )
        )
    return findings


def check_final_evaluation_task(
    repo_path: Path, cog_subtype: str | None = None
) -> list[Finding]:
    """PIPE-011: Pipeline cogs end with an AI evaluation task.

    Exempt: trigger-cogs (they fire flow runs, don't run pipelines),
    and evaluator-cog itself.
    """
    CHECK_ID = "PIPE-011"
    findings: list[Finding] = []
    if cog_subtype == "trigger":
        return findings
    # Check if this is evaluator-cog itself
    if (repo_path / "src" / "evaluator_cog").is_dir():
        return findings
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    # NOTE: ``/v1/pipeline_evaluations`` is intentionally absent. That
    # path was a documentation bug — the canonical route is
    # ``/v1/evaluations`` (router prefix ``/v1`` + in-router path
    # ``/evaluations``); ``pipeline_evaluations`` is the underlying SQL
    # table name. Cogs still on the wrong path 404 silently and produce
    # no pipeline-health rows, so PIPE-011 must NOT treat the wrong
    # path as evidence that an evaluation task is wired up.
    evaluation_markers = (
        "pipeline_eval",
        "evaluation_client",
        "/v1/evaluations",
        "PipelineEvaluator",
    )
    found_marker = False
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        if any(m in text for m in evaluation_markers):
            found_marker = True
            break
    if not found_marker:
        findings.append(
            _finding(
                "PIPE-011",
                "WARN",
                "pipeline_consistency",
                "No AI evaluation task found in pipeline-cog source.",
                "Add a final task that writes to pipeline_evaluations (via the evaluation "
                "client from common-python-utils) so quality can be tracked.",
            )
        )
    return findings


_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "request"})
_HTTP_CLIENT_CLASSES = frozenset({"Client", "AsyncClient", "Session"})


def _is_direct_http_call(node: ast.Call) -> bool:
    """``httpx.get(...)``, ``requests.post(...)``, ``httpx.Client(...)`` and kin.

    Only the module-qualified spelling is recognised. A call through a
    client object (``client.post``) is already inside a function that
    built the client with one of these, and that construction is what is
    found.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return False
    if func.value.id not in ("httpx", "requests"):
        return False
    return func.attr in _HTTP_VERBS or func.attr in _HTTP_CLIENT_CLASSES


def _has_retry_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if name == "retry":
            return True
    return False


def _uncovered_http_calls(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Call]:
    """Direct HTTP calls in ``fn`` that sit inside no loop of ``fn``'s own.

    Nested functions are left to their own visit: a loop in the outer
    function does not retry a call made in a closure it defines.
    """
    found: list[ast.Call] = []

    def visit(node: ast.AST, in_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            looped = in_loop or isinstance(child, (ast.For, ast.AsyncFor, ast.While))
            if (
                isinstance(child, ast.Call)
                and not looped
                and _is_direct_http_call(child)
            ):
                found.append(child)
            visit(child, looped)

    visit(fn, False)
    return found


def check_retry_logic(repo_path: Path) -> list[Finding]:
    """PIPE-007: external API calls are retried at the call site.

    Read per function. A direct httpx/requests call is covered by a
    ``@retry`` decorator (tenacity's, or any decorator of that name), by
    sitting inside a loop in the same function — the shape of a
    hand-written attempt loop — or by a ``# no-retry: <reason>`` comment
    in the function, for a call whose failure does not matter.

    Calls through common-python-utils clients and SDKs are not examined:
    they are not direct calls, and the clients retry internally.

    Queue redelivery does not count. It re-runs the whole job minutes
    later and spends an attempt the job may need; the catalog entry says
    why at more length.
    """
    findings: list[Finding] = []
    src = repo_path / "src"
    if not src.is_dir():
        return findings

    for py_file in sorted(src.rglob("*.py")):
        if _is_checker_self_source(py_file):
            continue
        text = py_file.read_text(errors="replace")
        if "httpx" not in text and "requests" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = py_file.relative_to(repo_path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _has_retry_decorator(node):
                continue
            if "# no-retry:" in (ast.get_source_segment(text, node) or ""):
                continue
            if not _uncovered_http_calls(node):
                continue
            findings.append(
                _finding(
                    "PIPE-007",
                    "WARN",
                    "structural_conformance",
                    f"{rel}::{node.name} calls an external API directly with no retry.",
                    "Retry transient failures at the call site: tenacity's "
                    "@retry, an attempt loop, or a common-python-utils client. "
                    "If the call's failure does not matter, say so with a "
                    "`# no-retry: <reason>` comment in the function.",
                )
            )
    return findings


def check_no_retired_trigger_patterns(repo_path: Path) -> list[Finding]:
    """PIPE-008: Narrowed retired GitHub / GAS / gh CLI trigger patterns (2026-04).

    Fires only when: (1) a workflow uses ``repository_dispatch`` together with
    app-invoking steps, (2) Python/JS source actively POSTs to GitHub
    ``/dispatches``, (3) the retired ``google-app-script-trigger`` string appears,
    or (4) ``gh workflow run`` is invoked (shell or argv list form). Bare URL
    literals are intentionally ignored — see LLM rule PIPE-014 for consistency
    reasoning across input types.
    """
    CHECK_ID = "PIPE-008"
    findings: list[Finding] = []
    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.rglob("*.yml")) + sorted(wf_dir.rglob("*.yaml")):
            try:
                text = wf.read_text()
            except OSError:
                continue
            low = text.lower()
            if "repository_dispatch" not in low:
                continue
            relay = any(
                k in low
                for k in (
                    "prefect deployment run",
                    "prefect deploy",
                    "run_deployment(",
                    "npx prefect",
                )
            ) or (
                "/dispatches" in text
                and any(k in low for k in ("curl ", "httpx.", "requests."))
            )
            pure_ci = ("pytest" in low or "ruff" in low) and not relay
            if relay and not pure_ci:
                findings.append(
                    _finding(
                        "PIPE-008",
                        "WARN",
                        "structural_conformance",
                        f"repository_dispatch in GitHub workflow with app-triggering steps ({wf.relative_to(repo_path)}).",
                        "Use watcher-cog + Prefect instead of GHA repository_dispatch relays.",
                    )
                )

    code_exts = {".py", ".ts", ".tsx", ".js"}
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if "tests/" in str(path).replace("\\", "/"):
            continue
        if path.suffix.lower() not in code_exts:
            continue
        if ".github/workflows/" in str(path).replace("\\", "/"):
            continue
        try:
            body = path.read_text()
        except OSError:
            continue
        low = body.lower()
        if re.search(
            r"(httpx|requests)\.(post|put)\([^\)]*api\.github\.com/[^\"'\)]+/dispatches",
            body,
            re.I,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Active HTTP client call to GitHub dispatches API ({path.relative_to(repo_path)}).",
                    "Use watcher-cog + Prefect instead of repository_dispatch HTTP relays.",
                )
            )
        if "google-app-script-trigger" in body:
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"Retired google-app-script-trigger reference in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect; remove legacy Apps Script trigger hooks.",
                )
            )
        if re.search(r"\bgh\s+workflow\s+run\b", low):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of driving workflows via gh CLI.",
                )
            )
        if re.search(
            r"\[\s*['\"]gh['\"]\s*,\s*['\"]workflow['\"]\s*,\s*['\"]run['\"]",
            body,
        ):
            findings.append(
                _finding(
                    "PIPE-008",
                    "WARN",
                    "structural_conformance",
                    f"gh workflow run argv-style invocation in {path.relative_to(repo_path)}.",
                    "Use watcher-cog + Prefect instead of subprocess gh workflow relays.",
                )
            )
    return findings


# --- Runtime: queue and Lambda (PIPE-016..019, ADR-009) ---------------------


def _declared_dependency_text(repo_path: Path) -> str:
    """pyproject.toml and requirements.txt, lowercased and concatenated."""
    parts: list[str] = []
    for rel in ("pyproject.toml", "requirements.txt"):
        path = repo_path / rel
        if path.is_file():
            parts.append(path.read_text(errors="replace").lower())
    return "\n".join(parts)


_PREFECT_REQUIREMENT = re.compile(
    r"""(?:^|["'\s])prefect(\[[^\]]*\])?\s*([<>=!~;"']|$)""", re.M
)


def _declares_prefect(repo_path: Path) -> bool:
    """True when prefect itself — not prefect-something — is a dependency."""
    return _PREFECT_REQUIREMENT.search(_declared_dependency_text(repo_path)) is not None


def check_pipe_016(repo_path: Path) -> list[Finding]:
    """PIPE-016: the cog has left Prefect.

    The other half of the rule — the cog runs on Lambda behind its own
    queue — is declared in mini-app-polis/infra, not in the cog (ADR-010),
    and each evaluation reads only its own repository. A cog with no module
    block there has no function to deploy to, so its deploy job fails; that
    failure is the check for the declaration.
    """
    if not _declares_prefect(repo_path):
        return []
    return [
        _finding(
            "PIPE-016",
            "ERROR",
            "structural_conformance",
            "prefect is still a declared dependency.",
            "Remove prefect: pipeline cogs run on Lambda behind a queue "
            "(ADR-009). A cog that still declares it has not finished moving.",
        )
    ]


_INT_LITERAL = re.compile(r"^\d+$")


def _visibility_outlasts_timeout(visibility: str | None, timeout: str | None) -> bool:
    """The queue's visibility timeout is derived from, or exceeds, the function's.

    Derived means the queue's expression references the variable the
    function's timeout is. Two literals are compared. Anything else —
    a literal against a variable, two unrelated variables — cannot be
    confirmed from the source and is not accepted.
    """
    if not visibility or not timeout:
        return False
    if _INT_LITERAL.match(visibility) and _INT_LITERAL.match(timeout):
        return int(visibility) > int(timeout)
    var = re.fullmatch(r"(var|local)\.[\w-]+", timeout)
    if var and re.search(rf"\b{re.escape(timeout)}\b", visibility):
        return visibility.strip() != timeout.strip()
    return False


def check_pipe_017(repo_path: Path, repo_type: str = "") -> list[Finding]:
    """PIPE-017: a failed job is handed back per record, retried, and dead-lettered.

    Two halves, in the two repositories that own them (ADR-010). The
    infrastructure half — ReportBatchItemFailures on the mapping, a redrive
    policy to a dead-letter queue, a visibility timeout that outlasts the
    function — is declared once, in mini-app-polis/infra's cog module. The
    code half — the handler names the records that failed — is in the cog.
    """

    findings: list[Finding] = []

    def fail(message: str, suggestion: str) -> None:
        findings.append(
            _finding("PIPE-017", "ERROR", "pipeline_reliability", message, suggestion)
        )

    if repo_type == "infrastructure":
        resources = terraform_resources(repo_path, repo_type)
        if not resources:
            return findings
        mappings = of_type(resources, "aws_lambda_event_source_mapping")
        if not mappings:
            return findings
        if not any(
            "ReportBatchItemFailures" in (m.attr("function_response_types") or "")
            for m in mappings
        ):
            fail(
                "The event source mapping does not set function_response_types = "
                '["ReportBatchItemFailures"].',
                "Set it, so a failed record is handed back alone instead of the "
                "mapping deleting it (or redelivering its whole batch).",
            )

        queues = of_type(resources, "aws_sqs_queue")
        redriven = [q for q in queues if "deadLetterTargetArn" in (q.body or "")]
        if not redriven:
            fail(
                "No aws_sqs_queue sets a redrive_policy with a deadLetterTargetArn.",
                "Give the work queue a redrive policy to a dead-letter queue; "
                "without one a poison message retries forever.",
            )

        functions = of_type(resources, "aws_lambda_function")
        timeout = functions[0].attr("timeout") if functions else None
        work_queues = redriven or queues
        if work_queues and not any(
            _visibility_outlasts_timeout(q.attr("visibility_timeout_seconds"), timeout)
            for q in work_queues
        ):
            fail(
                "The work queue's visibility_timeout_seconds is not derived from, "
                "or longer than, the function's timeout.",
                "Derive it from the function's timeout variable (e.g. "
                "var.timeout_seconds + 60) so SQS never redelivers a job "
                "that is still running.",
            )
        return findings

    src = repo_path / "src"
    returns_failures = src.is_dir() and any(
        "batchItemFailures" in f.read_text(errors="replace")
        for f in src.rglob("*.py")
        if not _is_checker_self_source(f)
    )
    if not returns_failures:
        fail(
            "No source file under src/ returns batchItemFailures.",
            "Have the handler return {'batchItemFailures': [...]} naming the "
            "records that raised, rather than raising itself.",
        )
    return findings


def _is_set(value: str | None) -> bool:
    return value is not None and value.strip() not in ("", "null")


def check_pipe_018(repo_path: Path, repo_type: str = "infrastructure") -> list[Finding]:
    """PIPE-018: each cog's concurrency ceiling is stated once.

    Read from the module calls in mini-app-polis/infra (ADR-010), where each
    cog's settings are decided: every call to the cog-worker module sets
    exactly one of ``reserved_concurrency`` (a positive reservation — the
    only way to get 1) or ``max_concurrency`` (the mapping's ceiling, at
    least 2). The module refuses a plan that breaks this; the check makes
    the same statement visible in evaluations.
    """
    findings: list[Finding] = []
    for call in module_calls(repo_path, repo_type):
        if "cog-worker" not in (call.attr("source") or ""):
            continue
        # -1 is the module's default and means unreserved; 0 would stop the
        # function entirely. Anything else — a positive literal, or an
        # expression this check cannot evaluate — states a reservation.
        reserved = (call.attr("reserved_concurrency") or "").strip()
        reserves = _is_set(reserved) and reserved != "-1" and reserved != "0"
        caps = _is_set(call.attr("max_concurrency"))
        if reserves == caps:
            state = "both" if reserves else "neither"
            findings.append(
                _finding(
                    "PIPE-018",
                    "WARN",
                    "pipeline_reliability",
                    f'module "{call.name}" in {call.file} sets {state} of '
                    "reserved_concurrency and max_concurrency.",
                    "Set exactly one: reserved_concurrency = 1 for a cog that "
                    "must run alone (the mapping's ceiling cannot go below 2), "
                    "or max_concurrency = N for a ceiling above that. AWS "
                    "rejects a mapping maximum above the function's reservation.",
                )
            )
    return findings


_REMAINING_TIME = "get_remaining_time_in_millis"


def check_pipe_020(repo_path: Path) -> list[Finding]:
    """PIPE-020: a run stops before the function timeout, and says so."""
    CHECK_ID = "PIPE-020"
    if not (repo_path / "src").is_dir():
        return []
    if _REMAINING_TIME in production_python_text(repo_path):
        return []
    return [
        _finding(
            CHECK_ID,
            "WARN",
            "pipeline_reliability",
            "No source under src/ reads the Lambda context's "
            f"{_REMAINING_TIME}(), so a run that outlives the function "
            "timeout is killed with no report.",
            "Stop the run a margin before the deadline — transcription-cog's "
            "_deadline.py raises 30 seconds early — so the flow reports what "
            "it was doing and re-raises, instead of the invocation ending in "
            "silence.",
        )
    ]


_PREFECT_CLIENT_CALLS = ("create_flow_run", "run_deployment")


def check_pipe_019(repo_path: Path) -> list[Finding]:
    """PIPE-019: trigger cogs start work through the API, not Prefect or SQS."""
    findings: list[Finding] = []
    text = production_python_text(repo_path) if (repo_path / "src").is_dir() else ""

    prefect_calls = [c for c in _PREFECT_CLIENT_CALLS if f"{c}(" in text]
    prefect_client = "get_client(" in text and "prefect" in text
    if _declares_prefect(repo_path) or prefect_calls or prefect_client:
        found = prefect_calls + (["get_client"] if prefect_client else [])
        detail = f" (source calls {', '.join(found)})" if found else ""
        findings.append(
            _finding(
                "PIPE-019",
                "WARN",
                "structural_conformance",
                f"Trigger cog still depends on Prefect{detail}.",
                "Start runs by POSTing to api-kaianolevine-com "
                "(/v1/<cog>/runs) through KaianoApiClient; the API enqueues "
                "onto the owning cog's queue.",
            )
        )
    if re.search(r"\.send_message(_batch)?\(", text) and "sqs" in text.lower():
        findings.append(
            _finding(
                "PIPE-019",
                "WARN",
                "structural_conformance",
                "Trigger cog sends to SQS directly.",
                "Ask the API to run the job instead; it is the only producer "
                "to any cog's queue, and the trigger cog should hold no key "
                "that can write to one.",
            )
        )
    return findings
