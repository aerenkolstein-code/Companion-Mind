"""Pure, source-local normalization and static replay. No persistence/identity runtime."""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata

from companion_mind.journal.codec import JournalError, REDACTED, safe_label, scrub


class CaptureError(ValueError):
    """Constant safe reason code only; never include an input value or raw DOM."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def label(value):
    try:
        return safe_label(value)
    except JournalError:
        raise CaptureError("AMBIGUOUS_IDENTITY") from None


def normalize_text(value):
    """Canonical text normalization independent of secret detection."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def clean_text(value):
    """Reuse A019 scrub policy and omit credential-bearing/query-bearing URLs.

    This is the declared conservative pattern boundary, not a general DLP claim.
    Joining DOM text precedes scrubbing so splitting a token across spans cannot
    evade the boundary.
    """
    value = normalize_text(value)
    clean = scrub(value)
    clean = re.sub(r"https?://[^\s<>]*[?#@][^\s<>]*", REDACTED, clean)
    return clean


@dataclass(frozen=True)
class Attachment:
    attachment_id: str
    filename: str | None
    media_type: str | None
    source_ref: str
    capture_status: str
    url_omitted: bool = True


@dataclass(frozen=True)
class Capture:
    """A source-local observation, deliberately not a CanonicalEvent mapping."""
    scope_alias: str
    conversation_id: str
    conversation_kind: str
    message_id: str
    role: str
    source_position: int
    payload: str
    attachments: tuple[Attachment, ...]
    terminal_observation: str | None
    control_state: str
    redaction_state: str
    profile_id: str
    profile_version: str
    profile_fingerprint: str

    @property
    def source_event_key(self):
        return "c1-" + fingerprint([self.profile_id, self.scope_alias, self.conversation_kind,
                                    self.conversation_id, self.message_id])

    @property
    def normalized_fingerprint(self):
        return fingerprint({k: v for k, v in asdict(self).items()
                            if k not in {"scope_alias", "conversation_id", "conversation_kind", "message_id"}})

    def to_dict(self):
        return {**asdict(self), "kind": "SOURCE_LOCAL_ONLY", "capture_id": self.source_event_key,
                "source_event_key": self.source_event_key,
                "normalized_fingerprint": self.normalized_fingerprint,
                "canonical_commit": False, "ingest_state": "NOT_ATTEMPTED", "authority": False}


def replay_captures(captures):
    """Pure fixture reconciliation only; callers retain no runtime/store here."""
    unique = {}
    positions = {}
    for capture in captures:
        if not isinstance(capture, Capture):
            raise CaptureError("INVALID_CAPTURE")
        key = capture.source_event_key
        prior = unique.get(key)
        if prior and prior.normalized_fingerprint != capture.normalized_fingerprint:
            raise CaptureError("CONFLICT")
        position = (capture.profile_id, capture.scope_alias, capture.conversation_kind,
                    capture.conversation_id, capture.source_position)
        if position in positions and positions[position] != key:
            raise CaptureError("AMBIGUOUS_IDENTITY")
        positions[position] = key
        unique[key] = capture
    return tuple(unique[k] for k in sorted(unique, key=lambda k: (
        unique[k].scope_alias, unique[k].conversation_id, unique[k].source_position, k)))


def reconcile_conversation(temporary, stable, mapping):
    """Return explicit, verifiable mapping evidence; never change either capture."""
    expected = {"scope_alias": temporary.scope_alias, "temporary_id": temporary.conversation_id,
                "stable_id": stable.conversation_id}
    if (mapping != expected or temporary.conversation_kind != "temporary" or
            stable.conversation_kind != "stable" or temporary.scope_alias != stable.scope_alias or
            temporary.profile_id != stable.profile_id or temporary.message_id != stable.message_id or
            temporary.normalized_fingerprint != stable.normalized_fingerprint):
        raise CaptureError("AMBIGUOUS_IDENTITY")
    return {"kind": "EXPLICIT_CONVERSATION_RECONCILIATION", **expected,
            "old_capture_id": temporary.source_event_key, "new_capture_id": stable.source_event_key,
            "normalized_fingerprint": stable.normalized_fingerprint, "authority": False,
            "canonical_commit": False}


def calibrate_window(traces, margin_ms=50):
    """Synthetic-only candidate: largest post-terminal mutation gap plus margin."""
    if type(margin_ms) is not int or margin_ms <= 0 or not traces:
        raise CaptureError("INVALID_CALIBRATION")
    gaps = []
    for trace in traces:
        times, terminal = trace["mutation_times_ms"], trace["terminal_observed_ms"]
        if (type(terminal) is not int or terminal < 0 or len(times) < 2 or
                any(type(t) is not int or t < 0 for t in times) or
                any(a >= b for a, b in zip(times, times[1:])) or terminal not in times):
            raise CaptureError("INVALID_CALIBRATION")
        post = [t for t in times if t >= terminal]
        gaps.extend(b-a for a, b in zip(post, post[1:]))
    if not gaps:
        raise CaptureError("INVALID_CALIBRATION")
    return max(gaps) + margin_ms
