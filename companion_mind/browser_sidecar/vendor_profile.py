"""Load the single frozen synthetic profile; production DOM compatibility is unverified."""

from copy import deepcopy
import json
from pathlib import Path

from .normalize import CaptureError, fingerprint

PROFILE_ID = "ChatGPT-Web-v0.1"
PROFILE_VERSION = "0.1"
# Set from the reviewed JSON excluding profile_fingerprint, not from input claims.
PROFILE_FINGERPRINT = "9aeb984182bcbc8a9562eeff60c9b541fb89fcb0dbbd2fbcde6ffab727701268"
DEFAULT_PROFILE = Path(__file__).with_name("profiles") / "chatgpt_web_v0_1.json"


def validate_profile(value):
    if not isinstance(value, dict):
        raise CaptureError("PROFILE_DRIFT")
    profile = deepcopy(value)
    declared = profile.pop("profile_fingerprint", None)
    try:
        actual = fingerprint(profile)
    except (TypeError, ValueError, RecursionError):
        raise CaptureError("PROFILE_DRIFT") from None
    if (profile.get("profile_id") != PROFILE_ID or profile.get("profile_version") != PROFILE_VERSION
            or declared != actual or actual != PROFILE_FINGERPRINT):
        raise CaptureError("PROFILE_DRIFT")
    return {**profile, "profile_fingerprint": actual}


def load_profile(path=DEFAULT_PROFILE):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        raise CaptureError("PROFILE_DRIFT") from None
    return validate_profile(value)
