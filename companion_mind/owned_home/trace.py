"""Allowlisted, content-minimized trace projection; never a reasoning log."""
from .contracts import fingerprint


def human_trace(record, projection):
    req = record["request"]
    value = {"trace_version": "human-trace/1", "authority": False,
             "identity": {k: req[k] for k in ("human_request_id", "request_id", "trace_id", "goal_id", "task_id",
                                             "session_id", "turn_id", "turn_no", "universe_id", "access_subject_id", "owner_id")},
             "request_fingerprint": req["request_fingerprint"], "request_state": record["state"],
             "response_fingerprint": record["response"]["response_fingerprint"] if record["response"] else None,
             "raw_payload_fingerprint": record["response"]["raw_payload_fingerprint"] if record["response"] else None,
             "normalized_fingerprint": record["response"]["normalized_fingerprint"] if record["response"] else None,
             "raw_evidence_owner": "A019", "normalized_is_derived": True,
             "resume_decision": projection["resume_decision"], "budget": projection["budget"],
             "owner": projection["owner"], "recovery_outcome": projection["status"],
             "continuation_count": projection["continuation_count"], "continuation_upper_bound": projection["continuation_upper_bound"],
             "automatic_resumes": 0, "notifications": 0, "authority_mutation_count": 0,
             "real_credential_reads": 0, "real_external_side_effects": 0}
    return {**value, "trace_fingerprint": fingerprint(value)}


def tool_trace(intent, decision, control, receipt):
    value = {"trace_version": "tool-trace/1", "skill_ref": intent["skill_ref"],
             "skill_fingerprint": intent["skill_fingerprint"], "action_id": intent["action_id"],
             "action_fingerprint": intent["intent_fingerprint"], "permission_tier": intent["permission_tier"],
             "side_effect_class": intent["side_effect_class"], "scope": intent["scope"],
             "grant_ref": decision["grant_ref"], "permission_decision": decision["decision"],
             "permission_reason": decision["reason_code"], "permission_fingerprint": decision["decision_fingerprint"],
             "policy_version": decision["policy_version"], "policy_fingerprint": decision["policy_fingerprint"],
             "dispatch_state": control["state"], "milestones": control["milestones"],
             "readback_status": receipt["readback"]["status"] if receipt else "NOT_DISPATCHED",
             "receipt_fingerprint": receipt["receipt_fingerprint"] if receipt else None,
             "terminal_outcome": receipt["outcome"] if receipt and control["state"] == "TERMINAL" else None,
             "retry_decision": "NO_AUTOMATIC_REDISPATCH", "automatic_redispatches": 0,
             "execution_count": receipt["execution_count"] if receipt else 0,
             "execution_upper_bound": receipt["execution_upper_bound"] if receipt else 0,
             "authority_mutation_count": 0, "real_credential_reads": 0, "real_external_side_effects": 0,
             "authority": False}
    return {**value, "trace_fingerprint": fingerprint(value)}


def context_trace(turn, router, context):
    result = {"trace_version": "context-trace/2", "authority": False,
              "active_topic": turn.topic_id, "session_id": turn.session_id,
              "authority_routes": router["routes"], "authorization": router["authorization"],
              "query_order": router["query_order"], "queried_refs": router["queried_refs"],
              "included_refs": context["included"], "omitted_refs": context["omitted"],
              "compacted_refs": context["compacted"], "conflicts": context["conflicts"],
              "budget": context["budget"], "context_fingerprint": context["context_fingerprint"],
              "invalidation_reasons": context["invalidation_reasons"],
              "rebuild_reasons": context["rebuild_reasons"],
              "terminal_reason": context["stop_reason"] or "CONTEXT_READY"}
    result["trace_fingerprint"] = fingerprint(result)
    return result


def model_trace(gateway, result):
    """Pure projection from selected control metadata and A019 result evidence."""
    selection, spec = gateway["selection"], gateway["call_spec"]
    trace = {"trace_version": "model-trace/1", "authority": False,
             "candidate_profile_refs": selection["candidates"],
             "selected_profile": selection["selected"],
             "selection_reason": selection["selection_reason"],
             "fallback_reason": selection["fallback_reason"],
             "capability_requirements": selection["required_capabilities"],
             "allowed_capabilities": selection["allowed_capabilities"],
             "budget_decision": {k: selection[k] for k in (
                 "requested_budget", "effective_input_budget", "usable_input_capacity",
                 "output_reserve", "envelope_reserve", "context_capacity")},
             "profile_fingerprint": selection["selected_profile_fingerprint"],
             "estimator_fingerprint": selection["estimator_fingerprint"],
             "context_fingerprint": spec["context_fingerprint"] if spec else gateway["context_fingerprint"],
             "call_spec_fingerprint": spec["spec_fingerprint"] if spec else None,
             "attempt_id": spec["identity"]["attempt_id"] if spec else None,
             "terminal_model_outcome": result["outcome"] if result else "NOT_INVOKED",
             "result_fingerprint": result["result_fingerprint"] if result else None,
             "retry_decision": "NO_AUTOMATIC_RETRY", "automatic_retries": 0,
             "provider_invocation_count": result["invocation_count"] if result else 0,
             "provider_invocation_upper_bound": result["invocation_upper_bound"] if result else 0,
             "real_provider_invocations": 0, "stop_reason": gateway["stop_reason"]}
    trace["trace_fingerprint"] = fingerprint(trace)
    return trace


def readonly_trace(envelope):
    """Content-minimized public trace for the P3-S1 readonly profile."""
    value = {
        "trace_version": "readonly-trace/1", "authority": False,
        "profile_version": envelope["profile_version"],
        "task_id": envelope["task_id"], "session_id": envelope["session_id"],
        "request_id": envelope["request_id"],
        "package": {
            "package_id": envelope["package_id"],
            "package_version": envelope["package_version"],
            "manifest_digest": envelope["manifest_digest"],
        },
        "grant_ref": envelope["grant_ref"], "status": envelope["status"],
        "answerability": envelope["answerability"], "time_basis": envelope["time_basis"],
        "as_of": envelope["as_of"], "source_refs": envelope["source_refs"],
        "conflicts": envelope["conflicts"],
        "omitted_required_evidence": envelope["omitted_required_evidence"],
        "synthetic_cognition": True, "authorization_effect": "NONE",
        "real_provider_invocations": 0, "external_side_effects": 0,
        "authority_mutation_count": 0,
    }
    return {**value, "trace_fingerprint": fingerprint(value)}
