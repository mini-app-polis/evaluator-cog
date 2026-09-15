# The Railway worker, as a caller. Temporary by construction.
#
# Step 5 moves the worker to Lambda, where aws_iam_role.worker supplies
# exactly these permissions through the execution role and no key exists at
# all. Until then the consumer runs on Railway, which cannot assume a role,
# so it needs a user and a long-lived key — the same trade producer.tf
# makes, and the same one that disappears when the worker moves. Delete
# this file at step 5.

resource "aws_iam_user" "consumer" {
  name = "${var.name_prefix}-consumer"
}

data "aws_iam_policy_document" "consumer" {
  # Read and finish, and nothing else. It deliberately cannot SendMessage:
  # a worker that can enqueue its own work can loop, and the producer is
  # the only thing that should be putting jobs on this queue. It also
  # cannot see the dead-letter queue, which is a separate ARN.
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_user_policy" "consumer" {
  name   = "${var.name_prefix}-consumer-receive"
  user   = aws_iam_user.consumer.name
  policy = data.aws_iam_policy_document.consumer.json
}

# No aws_iam_access_key here, for the reason producer.tf gives: Terraform
# would hold the secret in plaintext local state. Mint it by hand:
#
#     aws iam create-access-key --user-name evaluator-consumer
