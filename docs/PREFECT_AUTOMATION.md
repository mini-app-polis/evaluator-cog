# Prefect Automation Setup

Prefect Cloud automations trigger evaluation findings when a flow run
enters a failed or crashed state. This is additive — the on_failure
and on_crashed hooks already wired into each flow handle crash
detection without requiring an automation. The automation provides
a second, independent signal.

## Current status

The POST /v1/prefect-webhook endpoint is live on deejay-marvel-api.
Automation is not yet configured in Prefect Cloud.

## How to configure

1. Go to app.prefect.cloud → Automations → Create
2. Choose the **Custom** template
3. Trigger:
   - Type: Flow run state change
   - Flows: process-new-csv-files, update-dj-set-collection
   - States: Failed, Crashed
4. Action:
   - Type: Trigger a webhook
   - URL: {KAIANO_API_BASE_URL}/v1/prefect-webhook
5. Save the automation

The webhook payload is received by POST /v1/prefect-webhook on
deejay-marvel-api, which maps state to severity (CRASHED → ERROR,
FAILED → WARN) and writes a finding directly to the pipeline_evaluations
table without calling Claude.

## No authentication required

The endpoint is public. Prefect calls it with no credentials.
If you want to add a shared secret later, check it as an
X-Prefect-Webhook-Secret header on the endpoint.