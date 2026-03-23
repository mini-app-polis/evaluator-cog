# Prefect Automation Setup

Prefect Cloud automations trigger evaluation findings when a flow run
enters a failed or crashed state. This is additive — the on_failure
and on_crashed hooks already wired into each flow handle crash
detection without requiring an automation. The automation provides
a second, independent signal.

## Current status

Configured and live. The automation covers all flows in the workspace
by default — no changes needed when new processors are added.

## Configuration

- **Trigger:** Flow run enters Failed, Crashed, Cancelled, or TimedOut
- **Flows:** All flows in the workspace
- **Action:** Call webhook block `evaluator-cog`
- **URL:** `https://courteous-purpose-production.up.railway.app/v1/prefect-webhook`
- **Method:** POST

## Payload sent to the webhook
```json
{
  "flow_run_id": "{{ flow_run.id }}",
  "flow_name": "{{ flow.name }}",
  "state_name": "{{ flow_run.state.name }}",
  "state_type": "{{ flow_run.state.type }}",
  "start_time": "{{ flow_run.start_time }}",
  "end_time": "{{ flow_run.end_time }}"
}
```

## How it works

The payload is received by `POST /v1/prefect-webhook` on
deejay-marvel-api, which maps state to severity (CRASHED → ERROR,
FAILED → WARN, all others → INFO) and writes a finding directly
to the pipeline_evaluations table without calling Claude.

## No authentication required

The endpoint is public. Prefect calls it with no credentials.
If you want to add a shared secret later, check it as an
`X-Prefect-Webhook-Secret` header on the endpoint.