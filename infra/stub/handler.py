"""A Lambda that proves the delivery path and nothing else.

Foundation ticket, step 3. It logs the event it was handed, makes one
authenticated call against api-kaianolevine-com, and returns. That is
enough to prove, before any real code exists:

  - SQS -> event source mapping -> Lambda actually delivers;
  - the deploy pipeline can replace this function's code;
  - and the Cloudflare hop works from AWS address space.

That last one is the thing most likely to bite. Bot Fight Mode
managed-challenged CI traffic from GitHub runner ranges and cost an
evening that looked like three unrelated failures. AWS egress is a new
source range hitting the same zone, so the probe below reports whether it
got JSON or an HTML challenge page — deliberately, at the step where the
answer is cheap, rather than from a queue full of retried messages.

Set EVALUATOR_STUB_FAIL=1 to make it raise. That is step 6: watch the
message retry maxReceiveCount times and land in the DLQ. Making it a
configuration change rather than a code edit means the failure path is
exercised with exactly the artifact that is deployed.

Standard library only, on purpose. A dependency here would test the
packaging rather than the path.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

#: Cloudflare's browser integrity check rejects unidentified automation.
_USER_AGENT = (
    "evaluator-cog/lambda-stub (+https://github.com/mini-app-polis/evaluator-cog)"
)

#: Cheap, authenticated, and the same endpoint a real run reads first.
_PROBE_PATH = "/v1/standards/catalog"


def _probe_api() -> dict:
    """One authenticated GET, reported rather than raised.

    Returns what an operator needs to tell the three failure modes apart:
    a challenge page (HTML, usually 403), a credential problem (401), and
    a working route (JSON).
    """
    base = (os.environ.get("KAIANO_API_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("EVALUATOR_COG_API_KEY") or "").strip()
    if not base:
        return {"ok": False, "reason": "KAIANO_API_BASE_URL is not set"}
    if not key:
        return {"ok": False, "reason": "EVALUATOR_COG_API_KEY is not set"}

    request = urllib.request.Request(
        f"{base}{_PROBE_PATH}",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(2048)
            content_type = response.headers.get("Content-Type", "")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read(2048)
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — the probe reports, it does not raise
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    text = body.decode("utf-8", "replace")
    looks_like_html = content_type.startswith("text/html") or text.lstrip()[:1] == "<"

    return {
        "ok": status == 200 and not looks_like_html,
        "status": status,
        "content_type": content_type,
        # The tell. An HTML body from a JSON endpoint is a challenge page,
        # and a WAF rule for this source range is the fix.
        "looks_like_challenge_page": looks_like_html,
        "body_prefix": text[:200],
    }


def lambda_handler(event, context):  # noqa: ARG001 — context is unused by design
    """Log what arrived, probe the API, and return."""
    records = event.get("Records", []) if isinstance(event, dict) else []
    log.info("stub: invoked with %d record(s)", len(records))

    for record in records:
        log.info(
            "stub: messageId=%s receiveCount=%s body=%s",
            record.get("messageId"),
            (record.get("attributes") or {}).get("ApproximateReceiveCount"),
            (record.get("body") or "")[:1000],
        )

    probe = _probe_api()
    log.info("stub: api probe %s", json.dumps(probe))

    if os.environ.get("EVALUATOR_STUB_FAIL") == "1":
        # Step 6: exercise the redrive policy with the deployed artifact.
        raise RuntimeError(
            "EVALUATOR_STUB_FAIL is set — failing on purpose so this message "
            "retries and lands in the dead-letter queue"
        )

    return {"ok": True, "records": len(records), "probe": probe}
