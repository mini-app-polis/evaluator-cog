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

    TODO(lambda-quota): request the increase, then set this.

      Service Quotas -> Lambda -> "Concurrent executions" -> Request
      increase. The default account limit is 1,000 in most regions but a
      new account is throttled well below it; the ask is to be raised to
      the standard limit, not above it, so it is routine rather than a
      capacity case.

    Not urgent, and worth saying why rather than leaving it open-ended.
    max_concurrency on the event source mapping is a real ceiling and
    needs no quota — it is what actually limits a fleet pass today. What
    this variable adds once available is a *reservation*: guaranteed
    capacity for this function rather than a cap on it, which matters when
    a second cog's worker starts competing for the same account pool.

    So the trigger for doing this is the second cog going to Lambda, not
    a date.
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

variable "max_concurrency" {
  description = <<-DESC
    How many workers the queue may run at once.

    Not reserved_concurrency, which is the other knob above and is
    unavailable: AWS refuses to reserve for a function if that would leave
    the account under 100 unreserved, and this account is below that. This
    one lives on the event source mapping instead, needs no quota, and is
    the throttle that actually exists today.

    It matters because a fleet pass is now N concurrent jobs rather than
    one serial loop. Each clones a repository and posts its findings back
    through Cloudflare to a single Railway container, so an unthrottled
    pass is the self-inflicted load test the fleet's own notes warn about,
    aimed at the API that every other service also depends on.

    Four is a starting point, not a measurement: it keeps a pass roughly
    four times faster than the old serial sweep while leaving the API most
    of its headroom. Raise it once a pass has been watched under load. AWS
    requires at least 2.
  DESC
  type        = number
  default     = 4

  validation {
    condition     = var.max_concurrency >= 2
    error_message = "SQS event source mappings require maximum_concurrency >= 2."
  }
}

variable "create_api_producer" {
  description = <<-DESC
    Whether this state owns the API's sending identity.

    True for the first cog, false for every one after — the same shape as
    create_github_oidc_provider, and for the same reason. There is one
    api-kaianolevine-com, so there should be one IAM user for it, holding
    one access key. A producer per cog means the API carries five
    credentials, five Doppler entries and five client configurations by
    the fifth cog, all saying the same thing.

    Its policy is a wildcard over `*-jobs`, so a new cog's queue is covered
    the moment it exists without a cross-state reference back to here.
  DESC
  type        = bool
  default     = true
}
