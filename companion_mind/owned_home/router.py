"""Deterministic synthetic Authority routing; authorization precedes retrieval.

Fixtures and grants are trusted test setup. No live adapter or persistent source
copy exists here. A grant directory, not source content, supplies version routes.
"""
from dataclasses import dataclass

from .contracts import AuthorityFixture, HomeError, fingerprint, identifier, permission


@dataclass(frozen=True)
class SourceRevision(AuthorityFixture):
    revision: str = "r1"
    lifecycle: str = "CURRENT"

    def __post_init__(self):
        super().__post_init__()
        identifier(self.revision)
        if self.lifecycle not in {"CURRENT", "HISTORY"}:
            raise HomeError("INVALID_LIFECYCLE")

    def ref(self):
        return {**super().ref(), "revision": self.revision, "status": self.lifecycle}


@dataclass(frozen=True)
class EvidenceNeed:
    source_id: str
    route: str = "CURRENT"
    version: str | None = None
    revision: str | None = None

    def __post_init__(self):
        identifier(self.source_id)
        if self.route not in {"CURRENT", "HISTORY", "EXACT"}:
            raise HomeError("INVALID_AUTHORITY_ROUTE")
        if self.route == "EXACT":
            identifier(self.version)
            if self.revision is not None:
                identifier(self.revision)
        elif self.version is not None or self.revision is not None:
            raise HomeError("VERSION_REQUIRES_EXACT_ROUTE")


def source_ref(fixture):
    return {**fixture.ref(), "revision": getattr(fixture, "revision", "r1")}


def route_authorities(scope, needs, fixtures, grants, counters):
    decisions, routes, evidence, catalog, conflicts, order = [], [], [], [], [], []
    for need in sorted(needs, key=lambda n: (n.source_id, n.route, n.version or "", n.revision or "")):
        # Enumerate explicit grant identity metadata only. No candidate scan has
        # occurred. An exact-version request never widens to another version.
        versions = ([need.version] if need.route == "EXACT" else sorted({g.version for g in grants
                    if (g.universe_id, g.access_subject_id, g.source_id) ==
                    (scope.universe_id, scope.access_subject_id, need.source_id)}))
        candidates, permitted = [], False
        for version in versions:
            decision = permission(scope, need.source_id, version, grants)
            decisions.append(decision)
            order.append({"action": "AUTHORIZE", "source_id": need.source_id,
                          "version": version, "decision": decision["decision"]})
            if decision["decision"] != "ALLOW":
                continue
            permitted = True
            counters["candidate_retrievals"] += 1
            order.append({"action": "RETRIEVE", "source_id": need.source_id, "version": version})
            matched = [f for f in fixtures if (f.universe_id, f.access_subject_id, f.source_id, f.version) ==
                       (scope.universe_id, scope.access_subject_id, need.source_id, version)]
            counters["authority_reads"] += len(matched)
            candidates.extend(matched)
        if not versions:
            order.append({"action": "AUTHORIZE", "source_id": need.source_id, "decision": "DENY"})
        candidates.sort(key=lambda f: (f.source_id, f.version, source_ref(f)["revision"]))
        catalog.extend(source_ref(f) for f in candidates)
        if need.route == "CURRENT":
            selected = [f for f in candidates if source_ref(f)["status"] == "CURRENT"]
        elif need.route == "HISTORY":
            selected = [f for f in candidates if source_ref(f)["status"] == "HISTORY"]
        else:
            selected = [f for f in candidates if f.version == need.version and
                        (need.revision is None or source_ref(f)["revision"] == need.revision)]
        conflict = len(selected) > 1 and need.route != "HISTORY"
        if conflict:
            conflicts.append({"source_id": need.source_id, "reason": "AMBIGUOUS_AUTHORITY",
                              "resolution": "CONFLICT", "refs": [source_ref(f) for f in selected]})
            selected = []
        if need.route == "CURRENT" and selected:
            stale = [f for f in candidates if source_ref(f)["status"] == "HISTORY" and
                     fingerprint(f.text) != fingerprint(selected[0].text)]
            if stale:
                conflicts.append({"source_id": need.source_id, "reason": "CURRENT_HISTORY_DISAGREEMENT",
                                  "resolution": "CURRENT_WINS", "refs": [source_ref(f) for f in stale]})
        state = ("NOT_LOOKED_UP" if not permitted else "UNKNOWN" if not selected else
                 "KNOWN_EMPTY" if all(f.text == "" for f in selected) else "KNOWN_VALUE")
        routes.append({"source_id": need.source_id, "route": need.route, "requested_version": need.version,
                       "requested_revision": need.revision, "authorization": "ALLOW" if permitted else "DENY",
                       "knowledge_state": state, "conflict": "CONFLICT" if conflict else "NONE",
                       "version_result": "EXACT_MATCH" if selected and need.route == "EXACT" else
                                         "ROUTED" if selected else "UNRESOLVED",
                       "freshness": "CURRENT" if selected and need.route == "CURRENT" else
                                    "HISTORICAL" if selected and need.route == "HISTORY" else "N_A",
                       "negative_existence": "NOT_ESTABLISHED", "selected": [source_ref(f) for f in selected]})
        evidence.extend({"ref": source_ref(f), "text": f.text} for f in selected)
    # Stable ordering and deduplication, independent of fixture/grant insertion.
    def unique(values):
        return [v for _, v in sorted({fingerprint(v): v for v in values}.items())]
    catalog = unique(catalog)
    evidence = sorted(unique(evidence), key=lambda e: (e["ref"]["source_id"], e["ref"]["version"], e["ref"]["revision"]))
    projection = {"router_version": "authority-router/2", "authority": False,
                  "routes": routes, "authorization": unique(decisions), "query_order": order,
                  "queried_refs": catalog, "conflicts": conflicts}
    snapshot = {"sources": catalog, "policy_fingerprint": fingerprint(unique(decisions)),
                "routes_fingerprint": fingerprint(routes)}
    return projection, evidence, snapshot
