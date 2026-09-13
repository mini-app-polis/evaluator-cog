"""Ways of calling the handler.

The evaluation itself lives in ``flows.conformance.handler``. Everything
here only translates some transport into an ``EvaluationEvent`` and hands
it over, so moving to a different runtime is a new module in this package
rather than a change to the evaluator.
"""
