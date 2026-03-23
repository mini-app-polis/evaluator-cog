# Prefect Automation Setup

Prefect Cloud automations can be configured to trigger additional
actions when a flow run enters a failed or crashed state. This is
additive — the on_failure and on_crashed hooks already wired into
each flow handle crash detection without requiring an automation.

## Current status

Automation is not yet configured. The on_failure and on_crashed hooks
in each processor flow cover crash detection for now.

## When to set this up

Set up a Prefect automation when deejay-marvel-api has a dedicated
POST /v1/prefect-webhook endpoint that can receive Prefect flow run
event payloads. The webhook handler in
src/pipeline_evaluator/webhook.py is already built and ready.

## How to configure (when ready)

1. Go to app.prefect.cloud → Automations → Create
2. Choose the Custom template
3. Trigger:
   - Type: Flow run state change
   - Flows: process-new-csv-files, update-dj-set-collection
   - States: Failed, Crashed
4. Action:
   - Type: Trigger a webhook
   - URL: {KAIANO_API_BASE_URL}/v1/prefect-webhook
5. Save the automation

The webhook payload will be handled by
src/pipeline_evaluator/webhook.py which maps state to severity
(CRASHED → ERROR, FAILED → WARN) and posts a finding to
deejay-marvel-api without calling Claude.
