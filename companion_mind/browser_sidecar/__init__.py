"""C1 S0: synthetic static capture primitives, without a browser/controller."""

from .normalize import CaptureError, replay_captures, reconcile_conversation
from .parser import parse_dom
from .vendor_profile import load_profile

__all__ = ["CaptureError", "load_profile", "parse_dom", "replay_captures", "reconcile_conversation"]
