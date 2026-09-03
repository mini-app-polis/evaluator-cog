"""Application entrypoint for evaluator-cog.

Registers all evaluator-cog flows as Prefect Cloud deployments and starts
a runner loop that polls for scheduled or manually triggered runs.

Railway start command: python -m evaluator_cog.main

All flows run in-process on Railway with full access to environment
variables. No work pool required.

Registration goes through ``serve_with_retry`` rather than
``prefect.serve`` directly (CD-016). ``serve()`` makes a blocking,
fail-fast call to Prefect Cloud to resolve each deployment *before* the
runner loop starts; a transient error there propagates out of ``main()``
and the process exits. Because that happens before any flow run exists,
no ``on_failure``/``on_crashed`` hook can fire — the fleet goes down
silently. ``serve_with_retry`` rides out the blip and, on give-up, posts
one CRITICAL startup finding before re-raising.

That is layer 1 of two. Layer 2 is Railway's ``restartPolicyType:
ON_FAILURE`` in a version-controlled ``railway.json`` (CD-017), which
this repo does not yet have — so the retry ceiling here is currently
the whole of the coverage rather than the first factor of it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import sentry_sdk
from dotenv import load_dotenv
from mini_app_polis.serve_resilience import serve_with_retry
from prefect.flows import flow as prefect_flow


def main() -> None:
    """Register all flows and start the Prefect runner loop."""
    load_dotenv()
    sentry_sdk.init(dsn=os.getenv("SENTRY_DSN_EVALUATOR"), environment="production")

    src_path = os.environ.get(
        "APP_SOURCE_PATH", str(Path(__file__).parent.parent.parent)
    )

    conformance = prefect_flow.from_source(
        source=src_path,
        entrypoint="src/evaluator_cog/flows/conformance.py:conformance_check_flow",
    )

    serve_with_retry(
        conformance.to_deployment(
            name="conformance-check",
            cron="0 9 * * *",
        ),
        # Required and keyword-only: the helper is shared across the
        # fleet and cannot infer which cog it is serving, and the
        # give-up finding is unattributable without it. Must match
        # [project] name in pyproject.toml for version stamping.
        repo="evaluator-cog",
    )
    # pipeline_eval (flows/pipeline_eval.py) is intentionally NOT registered here.
    # evaluate_pipeline_run() is called in-process by other cogs at the end of
    # their flows. handle_prefect_flow_run_event() is invoked via Prefect Cloud
    # automation webhook. Neither runs as a scheduled Prefect deployment.


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
