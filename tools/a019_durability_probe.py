"""Synthetic syscall witness; run under strace, never on user conversation data."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion_mind.journal import Journal
from tools.a019_conformance import pair


class WitnessTrace(list):
    def append(self, marker):
        super().append(marker)
        if marker in {"USER_DURABLE_RECEIPT", "PROVIDER_INTENT_DURABLE", "PROVIDER_STUB_INVOKED"}:
            os.write(2, ("A019_WITNESS:" + marker + "\n").encode("ascii"))


if __name__ == "__main__":
    with Journal(sys.argv[1]) as journal:
        journal.trace = WitnessTrace()
        journal.append_user_then_invoke_stub(*pair(), attempt_id="syscall-witness")
