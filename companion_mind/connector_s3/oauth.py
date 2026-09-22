"""Manual exact-three-resource PKCE flow; constructing it launches nothing.

The broker must charge a stage OAuth start before binding a listener or opening
the browser. Only the user may authenticate, consent, and select resources.
"""
import base64
import hashlib
from urllib.parse import urlencode, urlparse, parse_qs

from companion_mind.connector_alpha.oauth import PkceFlow as _LoopbackFlow
from .policy import Denied, require
from .stage import GenerationBinding

SCOPE = 'https://www.googleapis.com/auth/drive.file'
ISSUER = 'https://accounts.google.com'


class PkceFlow(_LoopbackFlow):
    def __init__(self, binding, port):
        require(type(binding) is GenerationBinding, 'BINDING_INVALID')
        super().__init__(binding, port)

    @property
    def authorization_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        b = self.binding
        params = {'client_id': b.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code',
                  'scope': SCOPE, 'state': self.state, 'code_challenge': challenge,
                  'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent',
                  'trigger_onepick': 'true', 'allow_multiple': 'true', 'allow_folder_selection': 'true',
                  'file_ids': ','.join((b.folder_id, b.a_id, b.b_id)),
                  'mimetypes': 'application/vnd.google-apps.folder,application/vnd.google-apps.document'}
        return ISSUER + '/o/oauth2/v2/auth?' + urlencode(params)

    def accept_callback(self, callback_url):
        self.callback_diagnostic = {'stage': 'REJECTED'}
        require(self.used is False, 'CALLBACK_REPLAYED')
        try:
            require(type(callback_url) is str and len(callback_url) <= 8192, 'CALLBACK_INVALID')
            parsed = urlparse(callback_url)
            require(parsed.scheme == 'http' and parsed.netloc == '127.0.0.1:%d' % self.port
                    and parsed.path == '/callback' and not parsed.fragment and not parsed.params, 'CALLBACK_INVALID')
            pairs = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
            require(all(len(values) == 1 for values in pairs.values()), 'CALLBACK_DUPLICATE_PARAMETER')
            require(pairs.get('state') == [self.state], 'STATE_MISMATCH')
            if 'iss' in pairs:
                require(pairs['iss'] == [ISSUER], 'ISSUER_MISMATCH')
            self.used = True
            if 'error' in pairs:
                require(not any(key in pairs for key in ('code', 'scope', 'picked_file_ids')), 'CALLBACK_MIXED_RESPONSE')
                raise Denied('CONSENT_CANCELLED')
            require(all(pairs.get(key) and pairs[key][0] for key in ('code', 'scope', 'picked_file_ids')), 'CALLBACK_INVALID')
            require(pairs['scope'] == [SCOPE], 'SCOPE_MISMATCH')
            selected = pairs['picked_file_ids'][0].split(',')
            require(len(selected) == 3 and set(selected) == {self.binding.folder_id, self.binding.a_id, self.binding.b_id},
                    'PICKER_FILE_MISMATCH')
            code = pairs['code'][0]
            require(20 <= len(code) <= 2048 and all(33 <= ord(c) <= 126 for c in code), 'AUTHORIZATION_CODE_INVALID')
            self.callback_diagnostic = {'stage': 'ACCEPTED', 'selected_count': 3}
            return code
        except (ValueError, TypeError):
            raise Denied('CALLBACK_INVALID') from None

    def diagnose_callback(self, _):
        # Never return query fields or values, including unknown attacker input.
        return self.callback_diagnostic or {'stage': 'NOT_RECEIVED'}
