"""Tests for the entrypoint — CD-016's startup registration contract.

main.py had no test coverage before this file, which is how it kept
calling prefect.serve() directly through the whole Keystone pass: the
deterministic check that would have caught it did not exist yet, and
nothing else looked. These tests assert the contract rather than the
call shape, so a future refactor that reintroduces a bare serve() fails
here as well as in CD-016.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import evaluator_cog.main as main_module


def test_main_registers_through_serve_with_retry() -> None:
    """The runner loop is entered via the resilient wrapper, not serve()."""
    with (
        patch.object(main_module, "serve_with_retry") as serve_with_retry,
        patch.object(main_module, "sentry_sdk"),
        patch.object(main_module, "load_dotenv"),
        patch.object(main_module, "prefect_flow") as prefect_flow,
    ):
        prefect_flow.from_source.return_value = MagicMock()
        main_module.main()

    serve_with_retry.assert_called_once()
    assert serve_with_retry.call_args.args, (
        "serve_with_retry must be handed at least one deployment"
    )


def test_main_passes_the_repo_keyword() -> None:
    """CD-016 (2): without repo=, the give-up finding is unattributable.

    serve_with_retry is shared across the fleet and cannot infer which
    cog it is serving. It posts one CRITICAL finding when startup
    retries are exhausted, and that finding has no owner without this.
    The value must match [project] name in pyproject.toml, because
    version stamping resolves through it.
    """
    with (
        patch.object(main_module, "serve_with_retry") as serve_with_retry,
        patch.object(main_module, "sentry_sdk"),
        patch.object(main_module, "load_dotenv"),
        patch.object(main_module, "prefect_flow") as prefect_flow,
    ):
        prefect_flow.from_source.return_value = MagicMock()
        main_module.main()

    assert serve_with_retry.call_args.kwargs.get("repo") == "evaluator-cog"


def test_main_does_not_call_prefect_serve_directly() -> None:
    """A bare serve() anywhere in the entrypoint defeats the wrapper.

    Asserted against the AST rather than by mocking, because the failure
    mode is a *second* call added beside the wrapped one — mocking
    serve_with_retry would not see it.
    """
    source = Path(main_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    assert "serve" not in called, (
        "main.py calls serve() directly — startup registration must go "
        "through serve_with_retry() so a transient Prefect Cloud error "
        "during deployment resolution does not take the cog down "
        "silently (CD-016)"
    )
    assert "serve_with_retry" in called
