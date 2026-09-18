"""Runtime permission is an exact contract, never a model/UI assertion."""
from dataclasses import asdict, dataclass

from .contracts import HomeError, fingerprint, identifier
from .tool_gateway import CONNECTOR, SKILLS

POLICY = {"version": "synthetic-tool-permission/v1", "execution_tiers": ("P0_PURE", "P1_SCOPED_READ", "P2_REVERSIBLE_WRITE"),
          "hold_tiers": ("P3_CONSEQUENTIAL_WRITE", "P4_CRITICAL"), "break_glass": "DENY",
          "authority_mutation": "DENY", "human_override": "DISABLED", "automatic_redispatches": 0,
          "credential_domain_id": "synthetic-domain", "P2_success_requires": "READBACK_VERIFIED"}


@dataclass(frozen=True)
class SyntheticGrant:
    grant_id: str
    universe_id: str
    access_subject_id: str
    resource_ids: tuple
    operations: tuple
    credential_domain_id: str = "synthetic-domain"
    connector_class: str = CONNECTOR
    validity: str = "VALID"
    revoked: bool = False
    synthetic: bool = True
    public_safe: bool = True

    def __post_init__(self):
        for v in (self.grant_id, self.universe_id, self.access_subject_id, self.credential_domain_id):
            identifier(v)
        if not isinstance(self.resource_ids, (list, tuple)) or not 1 <= len(self.resource_ids) <= 16:
            raise HomeError("INVALID_SYNTHETIC_SCOPE")
        for resource in self.resource_ids:
            identifier(resource)
        if not isinstance(self.operations, (list, tuple)) or not self.operations or len(self.operations) > 2:
            raise HomeError("INVALID_SYNTHETIC_OPERATIONS")
        if any(v not in ("read", "write") for v in self.operations):
            raise HomeError("INVALID_SYNTHETIC_OPERATIONS")
        if self.validity not in ("VALID", "EXPIRED", "NOT_YET_VALID") or type(self.revoked) is not bool:
            raise HomeError("INVALID_SYNTHETIC_VALIDITY")
        if self.synthetic is not True or self.public_safe is not True or self.connector_class != CONNECTOR:
            raise HomeError("PUBLIC_SYNTHETIC_GRANT_REQUIRED")
        object.__setattr__(self, "resource_ids", tuple(sorted(set(self.resource_ids))))
        object.__setattr__(self, "operations", tuple(sorted(set(self.operations))))

    def projection(self):
        value = asdict(self)
        value.update(resource_ids=list(self.resource_ids), operations=list(self.operations))
        return {**value, "grant_fingerprint": fingerprint(value), "contains_secret": False}


def evaluate_permission(action, intent, skill, runtime_scope, grants, *, control_state="UNSEEN", invalid_binding=False):
    grant = next((g for g in grants if g.grant_id == action.grant_id), None)
    grant_ref = grant.projection() if grant else None
    tier = intent["permission_tier"]
    context = {"actor": runtime_scope.access_subject_id, "scope": runtime_scope.projection(),
               "skill_ref": intent["skill_ref"], "skill_fingerprint": intent["skill_fingerprint"],
               "action_fingerprint": intent["intent_fingerprint"], "target_id": intent["target_id"],
               "target_ref": intent["target_ref"], "permission_tier": tier,
               "side_effect_class": intent["side_effect_class"], "reversible": action.reversible,
               "grant_ref": grant_ref, "human_confirmation_ref": None,
               "policy_version": POLICY["version"], "policy_fingerprint": fingerprint(POLICY)}
    checks = {"exact_skill": skill is not None, "scope": action.scope == runtime_scope,
              "declared_tier": action.declared_tier in (None, tier),
              "declared_side_effect": action.declared_side_effect in (None, intent["side_effect_class"]),
              "exact_supplied_bindings": not invalid_binding}
    decision, reason = "DENY", "EXACT_SKILL_UNRESOLVED"
    if skill is not None:
        if not checks["scope"]:
            reason = "SCOPE_DENIED"
        elif not checks["declared_tier"] or not checks["declared_side_effect"]:
            reason = "DECLARED_PERMISSION_MISMATCH"
        elif invalid_binding:
            reason = "STALE_OR_INVALID_ACTION_PERMISSION_BINDING"
        elif tier == "P3_CONSEQUENTIAL_WRITE":
            decision, reason = "REQUIRE_HUMAN", "P3_EXECUTION_CLOSED"
        elif tier == "P4_CRITICAL":
            if action.parameters["operation"] in ("break_glass", "credential_mutation", "grant_mutation", "security_reconfiguration", "destructive_delete"):
                reason = "CRITICAL_OPERATION_DISABLED"
            else:
                decision, reason = "REQUIRE_HUMAN", "P4_EXECUTION_CLOSED"
        elif tier == "P0_PURE":
            decision, reason = "ALLOW", "EXACT_PURE_CONTRACT"
        else:
            operation = SKILLS[skill.skill_id][2]
            checks.update(target_resolved=intent["target_ref"] is not None,
                          grant_present=grant is not None,
                          grant_scope=bool(grant and (grant.universe_id, grant.access_subject_id) ==
                                           (action.universe_id, action.access_subject_id)),
                          grant_domain=bool(grant and grant.credential_domain_id == POLICY["credential_domain_id"]),
                          grant_resource=bool(grant and action.target_id in grant.resource_ids),
                          grant_operation=bool(grant and operation in grant.operations),
                          grant_valid=bool(grant and grant.validity == "VALID" and not grant.revoked))
            if tier == "P2_REVERSIBLE_WRITE":
                checks.update(reversible=action.reversible,
                              expected_readback=intent["expected_readback_class"] == "VALUE_FINGERPRINT")
            if all(checks.values()):
                decision, reason = "ALLOW", "EXACT_SCOPED_SYNTHETIC_GRANT"
            else:
                reason = "SCOPED_GRANT_OR_PRECONDITION_DENIED"
    # Mutable lifecycle is observable context, not a new authorization. The
    # exact one-action binding is stable across harmless observation/replay.
    authorization = fingerprint(context)
    context.update(current_action_control_state=control_state, authority=False)
    result = {"decision_id": "tool-decision-" + fingerprint({"binding": authorization, "decision": decision, "reason": reason})[:32],
              "action_fingerprint": intent["intent_fingerprint"], "skill_ref": intent["skill_ref"],
              "policy_version": POLICY["version"], "policy_fingerprint": fingerprint(POLICY),
              "authorization_binding_fingerprint": authorization, "scope": intent["scope"],
              "grant_ref": grant_ref, "checks": checks, "decision": decision, "reason_code": reason,
              "execution_allowed": decision == "ALLOW", "one_action_only": True,
              "expiry": "EXACT_ACTION_POLICY_AND_CURRENT_GRANT", "human_confirmation_ref": None,
              "authority_mutation_allowed": False}
    result["decision_fingerprint"] = fingerprint(result)
    return context, result
