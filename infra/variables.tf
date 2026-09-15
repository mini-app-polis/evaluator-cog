variable "region" {
  description = <<-DESC
    Match the Railway fleet, which runs in US East. The worker downloads a
    repository and then POSTs findings back to api-kaianolevine-com dozens
    of times per run, so every round trip pays whatever distance separates
    the two.
  DESC
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. One prefix per cog when the fleet generalises."
  type        = string
  default     = "evaluator"
}

variable "github_repo" {
  description = "owner/repo allowed to assume the deploy role via OIDC."
  type        = string
  default     = "mini-app-polis/evaluator-cog"
}

# ── Budget ───────────────────────────────────────────────────────────────

variable "alert_email" {
  description = "Where the budget alarm goes. The ticket's first step: costs here should be pennies, and the value of the alarm is knowing immediately if they are not."
  type        = string
}

variable "budget_limit_usd" {
  description = "Monthly budget. Set low on purpose — this should never fire."
  type        = number
  default     = 10
}

# ── Worker sizing ────────────────────────────────────────────────────────

variable "worker_timeout_seconds" {
  description = <<-DESC
    Above the slowest observed job with headroom. Measured today: one
    repository is ~13s, a 16-repository sweep is ~46s. The queue's
    visibility timeout is derived from this rather than configured
    separately, so the two cannot drift apart.
  DESC
  type        = number
  default     = 300
}

variable "worker_memory_mb" {
  description = "Lambda scales CPU with memory, and the AST work is CPU-bound. Start here and tune against the billed-duration metric."
  type        = number
  default     = 1024
}

variable "reserved_concurrency" {
  description = <<-DESC
    The throttle. Sixteen workers each posting findings to a single Railway
    container through Cloudflare is a self-inflicted load test, and this is
    also where the fleet-wide prefect.concurrency semaphores eventually land.

    -1 means unreserved, and it is the default on purpose. AWS refuses to
    reserve concurrency for a function if doing so would leave the account
    with fewer than 100 unreserved executions, and a new account's total
    limit is well below that — so any positive value here fails at apply
    time with a message about UnreservedConcurrentExecution rather than
    anything that sounds like a quota.

    Until that quota is raised the account limit is itself the throttle,
    which for a pilot is adequate. Raise the quota (Service Quotas ->
    Lambda -> "Concurrent executions"), then set this.
  DESC
  type        = number
  default     = -1
}

variable "max_receive_count" {
  description = "Deliveries before a message goes to the DLQ. Not automatic — without a redrive policy a poison message retries forever."
  type        = number
  default     = 3
}

variable "log_retention_days" {
  description = "CloudWatch Logs is inherited whether you want it or not; an explicit group means it does not retain forever by default."
  type        = number
  default     = 30
}

# ── Worker environment ───────────────────────────────────────────────────
#
# The current set from evaluator-cog's Railway service, minus anything about
# inbound requests: EVALUATOR_INVOKE_SECRET is not here because the worker
# has no inbound surface.

variable "kaiano_api_base_url" {
  description = "Base URL for api-kaianolevine-com."
  type        = string
}

variable "evaluator_cog_api_key" {
  description = "This cog's own named API key (CD-019). No fallback — unset or wrong means 401 on every call."
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "Read access to mini-app-polis repos. Unset means 60 unauthenticated requests/hour and repos silently throttled out of runs."
  type        = string
  sensitive   = true
}

variable "sentry_dsn" {
  description = "Sentry DSN for the worker."
  type        = string
  sensitive   = true
  default     = ""
}

variable "anthropic_api_key" {
  description = "Only the llm mode needs this; the release path is deterministic and costs no tokens."
  type        = string
  sensitive   = true
  default     = ""
}

variable "create_github_oidc_provider" {
  description = <<-DESC
    False when the account already has the GitHub OIDC provider — there can
    only be one per account, and a second `terraform apply` in a different
    cog's directory would otherwise fail on a resource that already exists.
    True for the first cog, false for every one after.
  DESC
  type        = bool
  default     = true
}

variable "stub_fail" {
  description = <<-DESC
    Makes the stub raise on every invocation, so a message is retried
    max_receive_count times and then lands in the dead-letter queue.
    Foundation ticket step 6: a DLQ nobody has watched a message enter is
    not yet a DLQ.

    A variable rather than a console or CLI edit because a Lambda's
    environment is a single map — setting one value with
    update-function-configuration replaces the whole thing, and putting the
    others back means typing secrets on a command line. Flip this, watch
    the DLQ, flip it back.
  DESC
  type        = bool
  default     = false
}

variable "worker_consumes_queue" {
  description = <<-DESC
    Whether the Lambda is attached to the queue as a consumer.

    False until step 5, and this is not a stylistic default. SQS delivers a
    message to exactly one consumer. While the worker is still the stub —
    which logs its event, probes the API and returns success — an enabled
    event source mapping makes it a competing consumer against the Railway
    container that does the real evaluation, and it wins nearly every race
    because Lambda's pollers are more aggressive than one container's long
    poll. The symptom is an empty queue, an empty dead-letter queue, and no
    evaluation: the job was consumed and discarded, which looks identical to
    a job that was never enqueued.

    Flip this to true in step 5, in the same change that stops the Railway
    consumer. Never have both running.
  DESC
  type        = bool
  default     = false
}
