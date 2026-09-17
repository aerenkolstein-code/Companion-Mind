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
