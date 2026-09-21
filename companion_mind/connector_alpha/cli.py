"""The only Connector Alpha operator entrypoint. Commands are never automatic."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
import sys
import tempfile
import webbrowser

from .contract import Denied, receipt
from .secret_store import CredentialBroker
from .session import ConnectorSession
from .transport import GoogleTransport
from .contract import Binding
from .oauth import PkceFlow
from .contract import SCOPE, require


def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(',', ':')))

def _write_content(path, value):
    require = __import__('companion_mind.connector_alpha.contract', fromlist=['require']).require
    require(path is not None and not os.path.isdir(path), 'CONTENT_OUTPUT_REQUIRED')
    parent = os.path.dirname(os.path.abspath(path))
    require(os.path.isdir(parent), 'CONTENT_OUTPUT_PATH_INVALID')
    fd, temporary = tempfile.mkstemp(prefix='connector-alpha-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def _write_binding(path, binding):
    parent = os.path.dirname(os.path.abspath(path))
    require(os.path.isfile(path) and os.path.isdir(parent), 'BINDING_OUTPUT_PATH_INVALID')
    fd, temporary = tempfile.mkstemp(prefix='connector-alpha-binding-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(asdict(binding), stream, sort_keys=True, separators=(',', ':'))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='connector-alpha')
    parser.add_argument('--config', required=True, help='non-secret binding JSON')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--content-out')
    parser.add_argument('command', choices=('authorize', 'read', 'revoke'))
    args = parser.parse_args(argv)
    transport = None
    try:
        binding = Binding.load(args.config)
        broker, transport = CredentialBroker(binding), GoogleTransport(binding)
        if args.command == 'authorize':
            token, installed, listener = None, False, None
            transport.cleanup_remote_state = 'NOT_NEEDED'
            flow = PkceFlow(binding, args.port)
            try:
                listener = flow.bind_listener()
                if webbrowser.open(flow.authorization_url) is not True:
                    raise Denied('SYSTEM_BROWSER_UNAVAILABLE')
                code = flow.await_callback(listener)
                response = transport.exchange_code(code, flow.verifier, flow.redirect_uri)
                token = response.get('access_token') if type(response) is dict else None
                require(type(response) is dict and response.get('scope') == SCOPE
                        and response.get('token_type') == 'Bearer'
                        and type(response.get('expires_in')) is int and 1 <= response['expires_in'] <= 3600
                        and type(token) is str and 20 <= len(token) <= 2048, 'TOKEN_RESPONSE_INVALID')
                identity = transport.read('identity', token).get('user')
                require(type(identity) is dict and identity.get('emailAddress') == binding.subject_email
                        and type(identity.get('permissionId')) is str and identity['permissionId'], 'IDENTITY_MISMATCH')
                require(binding.subject_permission_id in (None, identity['permissionId']), 'IDENTITY_MISMATCH')
                enrolled = replace(binding, subject_permission_id=identity['permissionId'])
                _write_binding(args.config, enrolled)
                broker = CredentialBroker(enrolled)
                broker.install_access_token(token, response['expires_in'])
                installed = True
            finally:
                if token is not None and not installed:
                    try:
                        transport.revoke(token); transport.cleanup_remote_state = 'REVOKED'
                    except BaseException:
                        transport.cleanup_remote_state = 'UNKNOWN'
                token = code = None
                if listener is not None:
                    flow.close_listener(listener)
            _emit(receipt('AUTHORIZED', 'LOCAL_SESSION_CREDENTIAL_STORED', oauth_exchanges=transport.counts['oauth_exchanges'],
                          google_reads=transport.counts['google_reads'], content_delivered=False,
                          authorization_cleanup_remote_state=transport.cleanup_remote_state,
                          listener_closed=True))
            return 0
        session = ConnectorSession(binding, broker, transport)
        if args.command == 'revoke':
            cleanup = session.revoke()
            _emit(cleanup)
            return 0 if (cleanup['local_closed'] and cleanup['credential_deleted']
                         and cleanup['remote_state'] == 'REVOKED') else 2
        result, cleanup, failure = None, None, None
        try:
            result = session.read_once()
            if result['receipt']['status'] == 'SUCCESS':
                session.deliver(result, lambda content: _write_content(args.content_out, content))
                result['receipt']['content_output'] = 'WRITTEN_EXPLICIT_LOCAL_PATH'
        except Exception as exc:
            failure = exc
        finally:
            cleanup = session.revoke()
        if failure is not None:
            code = str(failure) if type(failure) is Denied else 'CONNECTOR_FAILED_CLOSED'
            _emit(receipt('BLOCKED', code, google_reads=transport.counts['google_reads'],
                          content_delivered=False, cleanup_local_closed=cleanup['local_closed'],
                          cleanup_remote_state=cleanup['remote_state'],
                          cleanup_credential_deleted=cleanup['credential_deleted']))
            return 2
        result['receipt']['cleanup_local_closed'] = cleanup['local_closed']
        result['receipt']['cleanup_remote_state'] = cleanup['remote_state']
        result['receipt']['cleanup_credential_deleted'] = cleanup['credential_deleted']
        if not (cleanup['local_closed'] and cleanup['credential_deleted'] and cleanup['remote_state'] == 'REVOKED'):
            result['receipt']['status'] = 'CLEANUP_INCOMPLETE'
            result['receipt']['reason'] = 'CLEANUP_PARTIAL'
            _emit(result['receipt'])
            return 2
        _emit(result['receipt'])
        return 0
    except Exception as exc:
        code = str(exc) if type(exc) in (Denied,) else 'CONNECTOR_FAILED_CLOSED'
        counts = transport.counts if transport is not None else {}
        _emit(receipt('BLOCKED', code, google_reads=counts.get('google_reads', 'UNKNOWN'),
                      oauth_exchanges=counts.get('oauth_exchanges', 'UNKNOWN'), content_delivered=False,
                      authorization_cleanup_remote_state=getattr(transport, 'cleanup_remote_state', 'NOT_APPLICABLE')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
