"""Pure, fixed-cost local continuation policy. No worker, tool or provider path."""
from .contracts import HomeError, exact_keys, fingerprint

POLICY = {"version": "bounded-continuation/1", "max_steps": 1, "max_turns": 1,
          "max_tokens": 64, "max_time_ms": 1000,
          "units": "SYNTHETIC_FIXED_COST_NOT_PROVIDER_USAGE", "automatic_resume": False}
COST = {"steps": 1, "turns": 1, "tokens": 8, "time_ms": 1}
DEFAULT_BUDGET = {"max_" + k: POLICY["max_" + k] for k in COST}
RESUME_POLICY = {"version": "resume-decision/1", "owner": "RUNTIME", "binding": "EXACT",
                 "accepted_command": "CONTINUE", "unknown": "HOLD", "max_steps": 1,
                 "tool_execution_allowed": False, "authority_grant": False}


def validate_budget(value):
    exact_keys(value, DEFAULT_BUDGET)
    for key, ceiling in DEFAULT_BUDGET.items():
        if type(value[key]) is not int or not 0 <= value[key] <= ceiling:
            raise HomeError("INVALID_CONTINUATION_BUDGET")
    return dict(value)


def budget_receipt(request, reserved):
    value = {"policy": POLICY, "policy_fingerprint": fingerprint(POLICY),
             "limit": request["budget"], "cost": COST,
             "used": dict(COST) if reserved else {k: 0 for k in COST}}
    value["remaining"] = {k: max(0, request["budget"]["max_" + k] - value["used"][k]) for k in COST}
    return {**value, "budget_fingerprint": fingerprint(value)}


def resume_decision(request, response, owner, state, reserved, reason=None):
    outcome, why = "HOLD", reason or "MISSING_RESPONSE"
    if reason:
        outcome = "CANCEL" if reason == "CANCELLED" else "HOLD"
    elif not owner["unique"]:
        why = "OWNER_AMBIGUOUS"
    elif state == "TERMINAL":
        why = "ALREADY_STOPPED"
    elif state not in {"WAITING", "RESPONSE_DURABLE"}:
        why = "UNKNOWN_CONTROL_STATE"
    elif response:
        command = response["normalized"]["command"]
        if command == "CANCEL":
            outcome, why = "CANCEL", "CANCELLED"
        elif command != "CONTINUE":
            why = "AMBIGUOUS_RESPONSE" if command == "UNSURE" else "HUMAN_HOLD"
        elif reserved:
            why = "RECOVERY_UNKNOWN"
        elif any(request["budget"]["max_" + k] < amount for k, amount in COST.items()):
            why = "BUDGET_EXHAUSTED"
        else:
            outcome, why = "RESUME", "EXACT_LOCAL_ONE_HOP"
    value = {"policy_version": RESUME_POLICY["version"], "policy_fingerprint": fingerprint(RESUME_POLICY),
             "continuation_policy_fingerprint": fingerprint(POLICY),
             "decision": outcome, "reason": why, "request_fingerprint": request["request_fingerprint"],
             "response_fingerprint": response["response_fingerprint"] if response else None,
             "owner_fingerprint": owner["owner_fingerprint"], "authority_grant": False,
             "tool_execution_allowed": False}
    return {**value, "decision_fingerprint": fingerprint(value)}


def one_hop(request, response):
    """Called only after Runtime durably reserves the entire single-hop budget."""
    if response["normalized"]["command"] != "CONTINUE":
        raise HomeError("CONTINUATION_WITHOUT_RESPONSE")
    value = {"continuation_id": "hc-" + request["request_fingerprint"][:32],
             "goal_id": request["goal_id"], "task_id": request["task_id"],
             "human_request_id": request["human_request_id"],
             "request_fingerprint": request["request_fingerprint"],
             "response_fingerprint": response["response_fingerprint"],
             "quality_gate": "EXACT_PUBLIC_SYNTHETIC_COMMAND", "steps": 1,
             "result": "Local task continued once. STOP.", "stop_reason": "ONE_HOP_COMPLETE"}
    return {**value, "continuation_fingerprint": fingerprint(value)}
