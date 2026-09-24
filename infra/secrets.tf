# Which of the ecosystem's secrets this worker may read, by name.
#
# Doppler syncs the whole mini-app-polis-ecosystem/prd config into SSM
# Parameter Store under /mini-app-polis/prd/. The worker loads the names below
# at cold start (mini_app_polis.ssm_secrets.load_secrets, called from the
# package's __init__), and its role may read these parameters and no others.
#
# Names only: no value passes through Terraform, so none is in state or in a
# plan. To add a secret, put it in Doppler and add its name here.
#
# Environment variable name = parameter name. They are the same today; the
# map exists so one can differ from the other — a per-cog SENTRY_DSN_<COG>
# in Doppler arriving as SENTRY_DSN, for instance.

locals {
  ssm_prefix = "/mini-app-polis/prd/"

  # Missing any of these fails the cold start, loudly, before any work.
  ssm_parameters = {
    EVALUATOR_COG_API_KEY = "EVALUATOR_COG_API_KEY"
    GITHUB_TOKEN          = "GITHUB_TOKEN"
  }

  # The code has its own default for these. Absent from Doppler means unset
  # here — never "" — which is how "not configured" arrives.
  ssm_optional_parameters = {
    ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
    SENTRY_DSN        = "SENTRY_DSN"
  }

  ssm_parameter_arns = [
    for name in distinct(values(merge(local.ssm_parameters, local.ssm_optional_parameters))) :
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}${name}"
  ]
}
