# evaluator-cog

Standalone pipeline evaluation package for the Kaiano ecosystem.
Calls Claude against the engineering standards document after every
pipeline run and posts structured findings to deejay-marvel-api.

## Adding to a processor

In pyproject.toml [tool.uv.sources]:
  pipeline_evaluator = { git = "https://github.com/kaianolevine/evaluator-cog.git", rev = "main" }

In dependencies:
  "pipeline_evaluator"

## Wiring into a Prefect flow

from pipeline_evaluator.evaluator import evaluate_pipeline_run

# At the end of your flow:
evaluate_pipeline_run(
    run_id=os.environ.get("GITHUB_RUN_ID", "local-run"),
    repo="your-repo-name",
    ...
)

# For crash/failure detection, add hooks to your @flow decorator:
def _handle_flow_failure(flow, flow_run, state) -> None:
    evaluate_pipeline_run(
        run_id=str(flow_run.id),
        repo="your-repo-name",
        direct_finding_text=f"Flow entered {state.name} unexpectedly",
        direct_severity="ERROR" if "crash" in state.name.lower() else "WARN",
        ...
    )

@flow(
    name="your-flow-name",
    on_failure=[_handle_flow_failure],
    on_crashed=[_handle_flow_failure],
)
def your_flow():
    ...

## Prefect automation setup

See docs/PREFECT_AUTOMATION.md for setting up the Prefect Cloud
automation that triggers evaluation on FAILED and CRASHED state changes.
