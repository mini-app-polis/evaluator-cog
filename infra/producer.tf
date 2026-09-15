# The Railway API, as a caller. The one piece of cross-cloud coupling in
# the plan, and the one long-lived credential.

resource "aws_iam_user" "producer" {
  name = "${var.name_prefix}-producer"
}

data "aws_iam_policy_document" "producer" {
  # Send, and nothing else. It cannot read the queue, cannot delete from
  # it, and cannot see the DLQ.
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_user_policy" "producer" {
  name   = "${var.name_prefix}-producer-send"
  user   = aws_iam_user.producer.name
  policy = data.aws_iam_policy_document.producer.json
}

# No aws_iam_access_key here, deliberately.
#
# Terraform would hold the secret in state, and this state is a plaintext
# file on a workstation. Mint the key once, by hand, and put it straight
# into Doppler:
#
#     aws iam create-access-key --user-name evaluator-producer
#
# There is no OIDC path from Railway, so this is a long-lived credential
# and it is the thing in this plan most worth a rotation reminder.
