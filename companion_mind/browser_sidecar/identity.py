"""S1 source-local, in-memory identity reduction. No persistence or canonical emit.

Inputs must be exact, already-scrubbed S0 Capture values. All public mutations
are atomic; snapshots are detached values. This is not a browser controller,
concurrent-tab service, durable journal, or live exactly-once implementation.
"""
from dataclasses import asdict, dataclass, fields, replace
import re

from .normalize import Attachment, Capture, CaptureError, clean_text, fingerprint, label, reconcile_conversation
from .vendor_profile import PROFILE_FINGERPRINT, PROFILE_ID, PROFILE_VERSION


@dataclass(frozen=True)
class Decision:
    status: str
    member_count: int
    authority: bool = False
    canonical_commit: bool = False
    ingest_state: str = "NOT_ATTEMPTED"

    def to_dict(self):
        return asdict(self)


def _validate(capture):
    # Do not invoke caller-overridden properties, iterators, or coercions.
    if type(capture) is not Capture or set(vars(capture)) != {f.name for f in fields(Capture)}:
        raise CaptureError("INVALID_CAPTURE")
    strings = ("scope_alias", "conversation_id", "conversation_kind", "message_id", "role", "payload",
               "control_state", "redaction_state", "profile_id", "profile_version", "profile_fingerprint")
    if any(type(getattr(capture, name)) is not str for name in strings):
        raise CaptureError("INVALID_CAPTURE")
    for name in ("scope_alias", "conversation_id", "message_id"):
        label(getattr(capture, name))
    if capture.conversation_kind not in {"temporary", "stable"}:
        raise CaptureError("AMBIGUOUS_IDENTITY")
    if (capture.profile_id, capture.profile_version, capture.profile_fingerprint) != (
            PROFILE_ID, PROFILE_VERSION, PROFILE_FINGERPRINT):
        raise CaptureError("PROFILE_DRIFT")
    if capture.role not in {"user", "assistant"}:
        raise CaptureError("AMBIGUOUS_ROLE")
    if type(capture.source_position) is not int or not 0 <= capture.source_position <= 999999999:
        raise CaptureError("AMBIGUOUS_IDENTITY")
    if type(capture.attachments) is not tuple:
        raise CaptureError("INVALID_CAPTURE")
    try:
        clean = clean_text(capture.payload)
        capture.payload.encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise CaptureError("INVALID_CAPTURE") from None
    if clean != capture.payload:
        raise CaptureError("UNSAFE_CAPTURE")
    attachment_ids = set()
    for item in capture.attachments:
        if (type(item) is not Attachment or set(vars(item)) != {f.name for f in fields(Attachment)}
                or type(item.attachment_id) is not str):
            raise CaptureError("INVALID_CAPTURE")
        label(item.attachment_id)
        if item.attachment_id in attachment_ids:
            raise CaptureError("AMBIGUOUS_IDENTITY")
        attachment_ids.add(item.attachment_id)
        if (type(item.source_ref) is not str or item.source_ref !=
                f"message:{capture.message_id}:attachment:{item.attachment_id}" or
                type(item.capture_status) is not str or
                item.capture_status not in {"REFERENCE_ONLY", "UNAVAILABLE"} or item.url_omitted is not True):
            raise CaptureError("UNSAFE_CAPTURE")
        if item.filename is not None:
            if type(item.filename) is not str:
                raise CaptureError("INVALID_CAPTURE")
            try:
                if clean_text(item.filename) != item.filename:
                    raise CaptureError("UNSAFE_CAPTURE")
                item.filename.encode("utf-8")
            except UnicodeError:
                raise CaptureError("INVALID_CAPTURE") from None
        if item.media_type is not None and (type(item.media_type) is not str or
                not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", item.media_type)):
            raise CaptureError("INVALID_CAPTURE")
    redacted = "[SECRET_REDACTED]" in capture.payload or any(
        item.filename and "[SECRET_REDACTED]" in item.filename for item in capture.attachments)
    if capture.redaction_state != ("redacted" if redacted else "none"):
        raise CaptureError("UNSAFE_CAPTURE")
    terminal = capture.terminal_observation
    if terminal is not None and type(terminal) is not str:
        raise CaptureError("INVALID_CAPTURE")
    if capture.control_state == "TERMINAL_OBSERVED":
        if terminal not in {"complete", "partial", "failed"}:
            raise CaptureError("AMBIGUOUS_TERMINAL")
        if capture.role == "user" and terminal != "complete":
            raise CaptureError("AMBIGUOUS_TERMINAL")
        if capture.role == "assistant" and ((terminal == "failed") != (capture.payload == "")):
            raise CaptureError("AMBIGUOUS_TERMINAL")
    elif capture.control_state in {"STREAMING", "STABILIZING"}:
        if capture.role != "assistant" or terminal is not None:
            raise CaptureError("AMBIGUOUS_TERMINAL")
    else:
        raise CaptureError("AMBIGUOUS_TERMINAL")
    # Computed properties on an exact Capture, after every nested value has been
    # validated. Serialized caller-supplied hashes are never an input interface.
    return capture.source_event_key, capture.normalized_fingerprint


def _batch(values):
    if type(values) not in (tuple, list):
        raise CaptureError("INVALID_CAPTURE")
    values = tuple(values)
    for capture in values:
        _validate(capture)
    # frozen=True prevents ordinary assignment, not mutation of a caller's
    # __dict__. Store detached values, including detached attachment records.
    return tuple(replace(c, attachments=tuple(replace(a) for a in c.attachments)) for c in values)


def _conversation(capture):
    return (capture.profile_id, capture.scope_alias, capture.conversation_kind, capture.conversation_id)


def _put(observations, capture):
    key = capture.source_event_key
    previous = observations.get(key)
    if previous is not None and previous.normalized_fingerprint != capture.normalized_fingerprint:
        raise CaptureError("CONFLICT")
    observations[key] = capture


def _reduce(observations, aliases):
    members, positions = {}, {}
    for source_key, capture in sorted(observations.items()):
        conversation = aliases.get(_conversation(capture), _conversation(capture))
        logical = (*conversation, capture.message_id)
        position = (*conversation, capture.source_position)
        prior = members.get(logical)
        if prior is not None and prior["normalized_fingerprint"] != capture.normalized_fingerprint:
            raise CaptureError("CONFLICT")
        if position in positions and positions[position] != logical:
            raise CaptureError("AMBIGUOUS_IDENTITY")
        positions[position] = logical
        if prior is None:
            prior = {"logical_identity": logical, "source_event_key": "c1-" + fingerprint(list(logical)),
                     "normalized_fingerprint": capture.normalized_fingerprint,
                     "source_position": capture.source_position, "role": capture.role,
                     "source_capture_ids": []}
            members[logical] = prior
        prior["source_capture_ids"].append(source_key)
    return members, positions


class IdentityReducer:
    """Sequential S1 reducer. Conflicts raise constant CaptureError codes.

    A batch containing nonterminal input is held as a whole, without publishing
    even its valid terminal items. Callers replay observations later. This does
    not retain streaming bodies, run a clock, or promise recovery after exit.
    """

    def __init__(self):
        self._observations = {}
        self._aliases = {}
        self._evidence = frozenset()

    def _decision(self, status):
        members, _ = _reduce(self._observations, self._aliases)
        return Decision(status, len(members))

    def observe(self, captures):
        captures = _batch(captures)
        candidate = dict(self._observations)
        held = False
        for capture in captures:
            if capture.control_state != "TERMINAL_OBSERVED":
                held = True
            else:
                _put(candidate, capture)
        _reduce(candidate, self._aliases)  # Validate the whole terminal subset.
        if held:
            return self._decision("HOLD")
        changed = candidate != self._observations
        self._observations = candidate
        return self._decision("APPLIED" if changed else "IDEMPOTENT")

    def reconcile(self, temporary, stable, mapping):
        temporary, stable = _batch(temporary), _batch(stable)
        if (not temporary or not stable or type(mapping) is not dict or
                any(type(k) is not str or type(v) is not str for k, v in mapping.items()) or
                set(mapping) != {"scope_alias", "temporary_id", "stable_id"}):
            raise CaptureError("AMBIGUOUS_IDENTITY")
        for value in mapping.values():
            label(value)
        old = (PROFILE_ID, mapping["scope_alias"], "temporary", mapping["temporary_id"])
        new = (PROFILE_ID, mapping["scope_alias"], "stable", mapping["stable_id"])
        if any(_conversation(c) != old for c in temporary) or any(_conversation(c) != new for c in stable):
            raise CaptureError("AMBIGUOUS_IDENTITY")
        previous = self._aliases.get(old)
        if previous is not None and previous != new:
            raise CaptureError("AMBIGUOUS_IDENTITY")
        candidate = dict(self._observations)
        held = any(c.control_state != "TERMINAL_OBSERVED" for c in (*temporary, *stable))
        for capture in (*temporary, *stable):
            if capture.control_state == "TERMINAL_OBSERVED":
                _put(candidate, capture)
        if held:
            _reduce(candidate, self._aliases)
            return self._decision("HOLD")
        old_set = {c.message_id: c for c in candidate.values() if _conversation(c) == old}
        new_set = {c.message_id: c for c in candidate.values() if _conversation(c) == new}
        if not old_set or not old_set.keys() <= new_set.keys():
            raise CaptureError("AMBIGUOUS_IDENTITY")
        evidence = set(self._evidence)
        for message, before in sorted(old_set.items()):
            after = new_set[message]
            # Reuse S0 proof, now for EVERY affected temporary member. Stable-only
            # members remain independent observations; none are synthesized.
            proof = reconcile_conversation(before, after, mapping)
            evidence.add((proof["old_capture_id"], proof["new_capture_id"], proof["normalized_fingerprint"]))
        aliases = {**self._aliases, old: new}
        _reduce(candidate, aliases)  # Includes all stable-only members/positions.
        changed = (candidate != self._observations or aliases != self._aliases or evidence != self._evidence)
        self._observations, self._aliases, self._evidence = candidate, aliases, frozenset(evidence)
        return self._decision("APPLIED" if changed else "IDEMPOTENT")

    def snapshot(self):
        members, positions = _reduce(self._observations, self._aliases)
        return {"kind": "SOURCE_LOCAL_IDENTITY", "authority": False, "canonical_commit": False,
                "ingest_state": "NOT_ATTEMPTED", "member_count": len(members),
                "members": [members[k] for k in sorted(members)],
                "positions": [{"position": k, "logical_identity": positions[k]} for k in sorted(positions)],
                "observations": [self._observations[k].to_dict() for k in sorted(self._observations)],
                "aliases": [{"temporary": k, "stable": self._aliases[k]} for k in sorted(self._aliases)],
                "mapping_evidence": [{"old_capture_id": a, "new_capture_id": b, "normalized_fingerprint": f,
                                      "authority": False, "canonical_commit": False}
                                     for a, b, f in sorted(self._evidence)]}
