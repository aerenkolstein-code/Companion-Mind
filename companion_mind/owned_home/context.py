"""Bounded ContextPack v2 and source-linked, derived-only topic projections.

Only the canonical Journal persists turns. These pure builders keep bodies in
the transient cognition input; Working Sets/Boot Packs contain refs and hashes.
"""
from dataclasses import dataclass, field

from .contracts import HomeError, Turn, encode, fingerprint, identifier
from .router import EvidenceNeed

RECENT_TURNS = 4
MAX_EVIDENCE = 5


@dataclass(frozen=True)
class ContextTurn(Turn):
    topic_id: str = "topic-a"
    premise_id: str = "task-v1"
    evidence_needs: tuple = field(default_factory=tuple)

    def __post_init__(self):
        super().__post_init__()
        identifier(self.topic_id)
        identifier(self.premise_id)
        if not isinstance(self.evidence_needs, (list, tuple)) or len(self.evidence_needs) > 5:
            raise HomeError("EVIDENCE_NEED_LIMIT")
        needs = [EvidenceNeed(**n) for n in self.evidence_needs]
        if len({(n.source_id, n.route, n.version, n.revision) for n in needs}) != len(needs):
            raise HomeError("DUPLICATE_EVIDENCE_NEED")

    @property
    def needs(self):
        return [EvidenceNeed(**n) for n in self.evidence_needs]


def invalidation(previous, snapshot, premise_id):
    if not previous:
        return []
    old = previous["snapshot"]
    reasons = []
    def versions(refs):
        return sorted((r["source_id"], r["version"], r["revision"], r["content_fingerprint"]) for r in refs)
    def lifecycles(refs):
        return sorted((r["source_id"], r["version"], r["revision"], r["status"]) for r in refs)
    if old["policy_fingerprint"] != snapshot["policy_fingerprint"]:
        reasons.append("ACL_SCOPE_CHANGED")
    if versions(old["sources"]) != versions(snapshot["sources"]):
        reasons.append("SOURCE_REVISION_CHANGED")
    if lifecycles(old["sources"]) != lifecycles(snapshot["sources"]):
        reasons.append("LIFECYCLE_CHANGED")
    if old["routes_fingerprint"] != snapshot["routes_fingerprint"] and not reasons:
        reasons.append("AUTHORITY_ROUTE_CHANGED")
    if previous["premise_id"] != premise_id:
        reasons.append("PREMISE_CHANGED")
    return reasons


def build_context(turn, router, evidence, snapshot, *, previous=None, recent=(),
                  segments=(), reactivated=False, model_budget=None):
    reasons = invalidation(previous, snapshot, turn.premise_id)
    budget_limit = turn.budget_bytes if model_budget is None else model_budget["effective_input_budget"]
    working_id = "ws-" + fingerprint({**turn.scope.projection(), "session_id": turn.session_id,
                                      "topic_id": turn.topic_id})[:32]
    ledger, included, omitted, recent_refs = [], [], [], []
    payload = {"topic_id": turn.topic_id, "user_text": turn.text, "evidence": [], "recent_exact_turns": []}
    size = lambda value: len(encode(value).encode("utf-8"))
    base_size = size(payload)
    stop = (model_budget.get("stop_reason") if model_budget else None) or (
        "CONTEXT_BUDGET_EXCEEDED" if base_size > budget_limit else None)
    required_payload = {**payload, "evidence": evidence, "recent_exact_turns": list(recent)}
    required = size(required_payload)
    compacted = []
    # Older exact turns are not re-materialized into the input or an unbounded
    # trace. A ref-only summary accounts for every omitted historical turn.
    if len(recent) > RECENT_TURNS:
        older = recent[:-RECENT_TURNS]
        summary = {"kind": "RECENT_HISTORY", "count": len(older),
                   "refs_fingerprint": fingerprint([r["ref"] for r in older])}
        compacted.append({"ref": summary, "reason": "RECENT_TAIL_LIMIT", "input": "NONE"})
        omitted.append({"ref": summary, "reason": "RECENT_TAIL_LIMIT"})
        ledger.append({"kind": "RECENT_HISTORY", "ref": summary, "action": "COMPACT",
                       "reason": "RECENT_TAIL_LIMIT", "bytes": size(older)})
        recent = recent[-RECENT_TURNS:]
    for item in evidence:
        proposed = {**payload, "evidence": payload["evidence"] + [item]}
        reason = ("CONTEXT_BUDGET_EXCEEDED" if stop or size(proposed) > budget_limit else
                  "EVIDENCE_LIMIT" if len(included) >= MAX_EVIDENCE else None)
        ledger.append({"kind": "EVIDENCE", "ref": item["ref"], "action": "OMIT" if reason else "INCLUDE",
                       "reason": reason, "bytes": size(item)})
        if reason:
            omitted.append({"ref": item["ref"], "reason": reason})
        else:
            included.append(item["ref"])
            payload = proposed
    # Newest tail gets budget priority; selected turns are then serialized in
    # canonical chronological order. Invalidated history never enters input.
    selected_recent = []
    for index, item in enumerate(reversed(recent)):
        proposed_recent = [item] + selected_recent
        proposed = {**payload, "recent_exact_turns": proposed_recent}
        reason = ("INVALIDATED_DERIVED_INPUT" if reasons or not item.get("valid", True) else "RECENT_TAIL_LIMIT" if index >= RECENT_TURNS else
                  "CONTEXT_BUDGET_EXCEEDED" if stop or size(proposed) > budget_limit else None)
        ledger.append({"kind": "RECENT_TURN", "ref": item["ref"], "action": "OMIT" if reason else "INCLUDE",
                       "reason": reason, "bytes": size(item)})
        if reason:
            omitted.append({"ref": item["ref"], "reason": reason})
        else:
            selected_recent = proposed_recent
            payload = proposed
    recent_refs = [r["ref"] for r in selected_recent]
    states = [r["knowledge_state"] for r in router["routes"]]
    if not included:
        stop = stop or ("AUTHORITY_CONFLICT" if any(r["conflict"] == "CONFLICT" for r in router["routes"]) else
                        "PERMISSION_DENIED" if all(s == "NOT_LOOKED_UP" for s in states) else
                        "CONTEXT_BUDGET_EXCEEDED" if evidence else "AUTHORITY_SOURCE_UNAVAILABLE")
    active_payload = None if stop else payload
    source_refs = snapshot["sources"]
    working = {"working_set_id": working_id, "topic_id": turn.topic_id, "session_id": turn.session_id,
               **turn.scope.projection(), "authority": False, "derived_only": True,
               "source_refs": source_refs, "current_refs": [r for r in source_refs if r["status"] == "CURRENT"],
               "history_refs": [r for r in source_refs if r["status"] == "HISTORY"],
               "causal_link_refs": [r["ref"] for r in recent][-RECENT_TURNS:] + [turn.identity["user_event_id"]],
               "source_revisions": [{k: r[k] for k in ("source_id", "version", "revision")} for r in source_refs],
               "segment_refs": list(segments[-16:]),
               "older_segments": {"count": max(0, len(segments)-16), "refs_fingerprint": fingerprint(list(segments[:-16]))},
               "generated_at": turn.observed_at, "premise_id": turn.premise_id,
               "snapshot": snapshot, "invalidation_status": "REBUILT" if reasons else "VALID",
               "invalidated_fingerprint": previous.get("fingerprint") if previous and reasons else None,
               "invalidation_reasons": reasons, "build_ready": not bool(stop)}
    if model_budget is not None:
        working["model_profile_ref"] = model_budget["selected"]
        working["model_profile_fingerprint"] = model_budget["selected_profile_fingerprint"]
    working["fingerprint"] = fingerprint(working)
    rebuild = reasons or (["TOPIC_REACTIVATED"] if reactivated else ["TURN_ADVANCED"] if previous else ["INITIAL_BUILD"])
    if model_budget is not None and previous and previous.get("model_profile_fingerprint") != model_budget["selected_profile_fingerprint"]:
        rebuild = [*rebuild, "MODEL_PROFILE_CHANGED"]
    pack = {"context_version": "context-pack/2", "authority": False, **turn.scope.projection(),
            "topic_id": turn.topic_id, "session_id": turn.session_id, "working_set_ref": working_id,
            "current_refs": [r for r in included if r["status"] == "CURRENT"],
            "history_refs": [r for r in included if r["status"] == "HISTORY"],
            "recent_exact_turn_refs": recent_refs, "included": included, "omitted": omitted,
            "compacted": compacted, "ledger": ledger, "knowledge_states": states,
            "unused_layers_state": "N_A", "conflicts": router["conflicts"],
            "input_fingerprint": fingerprint(active_payload), "policy_fingerprint": snapshot["policy_fingerprint"],
            "invalidation_reasons": reasons, "rebuild_reasons": rebuild,
            "budget": {"unit": "UTF8_COGNITION_INPUT_BYTES", "limit": budget_limit,
                       "required": required, "included_bytes": 0 if stop else size(payload),
                       "omitted_bytes": required if stop else required - size(payload), "silent_truncations": 0},
            "status": "BLOCKED" if stop else "READY", "stop_reason": stop}
    if stop:
        # No item is reported as active/included when the whole input is held.
        pack["included"] = pack["current_refs"] = pack["history_refs"] = []
        pack["recent_exact_turn_refs"] = []
        for row in ledger:
            if row["action"] == "INCLUDE":
                row.update(action="OMIT", reason=stop)
                pack["omitted"].append({"ref": row["ref"], "reason": stop})
    if model_budget is not None:
        pack["model_budget"] = {k: model_budget[k] for k in (
            "selected", "selected_profile_fingerprint", "requested_budget", "effective_input_budget",
            "usable_input_capacity", "context_capacity", "output_reserve", "envelope_reserve", "estimator_fingerprint")}
    pack["context_fingerprint"] = fingerprint(pack)
    boot = {"derived_only": True, "authority": False, "working_set_id": working_id,
            "topic_id": turn.topic_id, "source_refs": source_refs,
            "working_set_fingerprint": working["fingerprint"], "invalidation_status": working["invalidation_status"]}
    return pack, working, boot, active_payload
