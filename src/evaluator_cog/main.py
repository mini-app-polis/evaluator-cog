"""Application entrypoint for evaluator-cog.

Registers all evaluator-cog flows as Prefect Cloud deployments and starts
a runner loop that polls for scheduled or manually triggered runs.

Railway start command: python -m evaluator_cog.main

All flows run in-process on Railway with full access to environment
variables. No work pool required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import sentry_sdk
from dotenv import load_dotenv
from prefect import serve
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

    serve(
        conformance.to_deployment(
            name="conformance-check",
            cron="0 9 * * *",
        ),
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
