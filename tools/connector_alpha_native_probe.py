"""Explicit Windows synthetic-canary qualification. Never contacts Google.

Writes only fresh uniquely named test slots and removes those exact slots.
Outputs booleans and fixed codes; no canary, credential or SID values.
"""
from dataclasses import asdict
import json
import platform
from pathlib import Path
import secrets
import sys
import subprocess
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion_mind.connector_alpha.contract import Binding, Denied
from companion_mind.connector_alpha.secret_store import CredentialBroker, SessionGate, WindowsCredentialStore, current_sid


def run():
    sid = current_sid()
    binding = Binding('canary-' + uuid.uuid4().hex, '123-nativeprobe.apps.googleusercontent.com',
                      'synthetic-test-project', 'Native synthetic qualification', 'probe@example.invalid',
                      'synthetic-permission', 'synthetic-owner', 'synthetic-universe', 'native-qualification',
                      'synthetic-document', sid, '2099-01-01T00:00:00+00:00', True, True, True)
    store, gate = WindowsCredentialStore(sid), SessionGate(sid)
    result = {'mode': 'WINDOWS_NATIVE_SYNTHETIC_CANARY', 'google_calls': 0,
              'windows_build': platform.version(), 'physical_lock_test': 'NOT_RUN', 'checks': {}}
    token = 'SYNTHETIC_ONLY_' + secrets.token_urlsafe(32)
    broker = CredentialBroker(binding, store, gate)
    try:
        gate.require_active()
        result['checks']['actual_sid_session_input_desktop'] = True
        broker.install_access_token(token)
        reader = CredentialBroker(binding, store, gate)
        reader.begin_read()
        result['checks']['native_round_trip'] = secrets.compare_digest(reader.acquire(), token)
        child = """import json,sys
from companion_mind.connector_alpha.contract import Binding, Denied
from companion_mind.connector_alpha.secret_store import CredentialBroker
try:
    CredentialBroker(Binding(**json.loads(sys.argv[1]))).begin_read()
except Denied as e:
    sys.exit(0 if str(e) == 'GRANT_NOT_ACTIVE' else 2)
sys.exit(1)
"""
        run = subprocess.run([sys.executable, '-c', child, json.dumps(asdict(binding))],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        result['checks']['separate_process_reading_denied'] = run.returncode == 0 and not run.stdout and not run.stderr
        restart = CredentialBroker(binding, store, gate)
        try:
            restart.begin_read()
            result['checks']['fresh_broker_reading_denied'] = False
        except Denied:
            result['checks']['fresh_broker_reading_denied'] = True
        reader.close_local()
        result['checks']['closed_cleanup_only_token_available'] = secrets.compare_digest(reader.token_for_cleanup(), token)
        reader.close_and_delete()
        result['checks']['token_deleted_readback'] = store.read_record(binding.credential_target) is None
        try:
            CredentialBroker(binding, store, gate).begin_read()
            result['checks']['closed_reopen_denied'] = False
        except Denied:
            result['checks']['closed_reopen_denied'] = True
        try:
            WindowsCredentialStore('S-1-5-21-0').read(binding.credential_target)
            result['checks']['wrong_sid_denied'] = False
        except Denied as exc:
            result['checks']['wrong_sid_denied'] = str(exc) == 'WINDOWS_IDENTITY_MISMATCH'
    except Exception as exc:
        result['failure'] = str(exc) if type(exc) is Denied else 'NATIVE_PROBE_FAILED'
    finally:
        token = None
        # These two UUID-derived slots were created by this synthetic probe only.
        for name, target in [('token', binding.credential_target), ('ledger', binding.lifecycle_target)]:
            try:
                store.delete(target)
                result['checks'][name + '_test_slot_removed'] = True
            except Exception:
                result['checks'][name + '_test_slot_removed'] = False
        result['passed'] = 'failure' not in result and bool(result['checks']) and all(result['checks'].values())
    print(json.dumps(result, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
