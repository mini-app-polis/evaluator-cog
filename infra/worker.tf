# The worker, and the event source mapping that is the whole point: AWS
# polls the queue and invokes the function, so the polling still happens
# but it is not a container of yours doing it.

data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name_prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

data "aws_iam_policy_document" "worker" {
  # Read the queue. The event source mapping does the receiving, but it
  # does it with this role's permissions.
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  # Write logs. CloudWatch is inherited whether or not you want it, so it
  # is accepted as a second log destination rather than fought.
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "${var.name_prefix}-worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# Declared rather than left to Lambda's implicit creation, which retains
# forever and is owned by nothing.
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${var.name_prefix}-worker"
  retention_in_days = var.log_retention_days
}

# The stub. A function that logs its event, probes the API and exits —
# enough to prove the delivery path, the deploy pipeline and the Cloudflare
# hop before any real code exists. Step 5 replaces the contents of the zip,
# not this resource.
data "archive_file" "stub" {
  type        = "zip"
  source_dir  = "${path.module}/stub"
  output_path = "${path.module}/stub.zip"
}

resource "aws_lambda_function" "worker" {
  function_name = "${var.name_prefix}-worker"
  role          = aws_iam_role.worker.arn
  runtime       = "python3.11"
  handler       = "handler.lambda_handler"
  architectures = ["arm64"]

  filename         = data.archive_file.stub.output_path
  source_code_hash = data.archive_file.stub.output_base64sha256

  timeout     = var.worker_timeout_seconds
  memory_size = var.worker_memory_mb

  # The throttle, and eventually the home of the fleet-wide concurrency
  # limits that prefect.concurrency held.
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = merge(
      {
        KAIANO_API_BASE_URL   = var.kaiano_api_base_url
        EVALUATOR_COG_API_KEY = var.evaluator_cog_api_key
        GITHUB_TOKEN          = var.github_token
        SENTRY_DSN_EVALUATOR  = var.sentry_dsn
        ANTHROPIC_API_KEY     = var.anthropic_api_key
        ENVIRONMENT           = "production"
      },
      var.stub_fail ? { EVALUATOR_STUB_FAIL = "1" } : {}
    )
  }

  depends_on = [aws_cloudwatch_log_group.worker]

  lifecycle {
    # Terraform owns the function's configuration; CI owns its code.
    #
    # Without this, every `terraform apply` after a deploy would quietly
    # roll the function back to whatever is in infra/stub — which is the
    # "which version is actually deployed" confusion that skipping a
    # container registry was supposed to avoid, reintroduced from the
    # other side.
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn

  # Off until step 5. One queue, one consumer — see worker_consumes_queue.
  enabled = var.worker_consumes_queue

  # One job per invocation. The handler's unit of work is one repository,
  # and the visibility-timeout arithmetic above is per job — a batch would
  # make the deadline depend on how many arrived together. Batching is a
  # tuning knob for later, and it needs ReportBatchItemFailures before it
  # is safe, or one bad record redelivers its whole batch.
  batch_size = 1

  function_response_types = ["ReportBatchItemFailures"]
}
