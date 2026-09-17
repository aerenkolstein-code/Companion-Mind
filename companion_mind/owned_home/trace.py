"""Allowlisted, content-minimized trace projection; never a reasoning log."""
from .contracts import fingerprint


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
