"""Exact local binding and safe projections. No credentials in configuration."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re

from companion_mind.owned_home.contracts import HomeError, encode, fingerprint

SCOPE = 'https://www.googleapis.com/auth/drive.file'
MIME = 'application/vnd.google-apps.document'
PROFILE = 'owned-home/connector-alpha/1'
MAX_CONFIG_BYTES = 16_384


class Denied(HomeError):
    """Only application-authored codes may leave the broker."""


def require(condition, code):
    if not condition:
        raise Denied(code)


def utc():
    return datetime.now(timezone.utc).isoformat()


def exact_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            require(k not in result, 'DUPLICATE_JSON_KEY')
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Denied('INVALID_JSON')))
    except (ValueError, TypeError, UnicodeError):
        raise Denied('INVALID_JSON') from None


@dataclass(frozen=True, repr=False)
class Binding:
    installation: str
    client_id: str
    project_id: str
    app_name: str
    subject_email: str
    subject_permission_id: str | None
    owner: str
    universe: str
    purpose: str
    file_id: str
    windows_sid: str
    expires_at: str
    dedicated_test_app: bool
    non_sensitive_test_file: bool
    revoke_project_grant_authorized: bool

    def __post_init__(self):
        for name in ('installation', 'project_id', 'owner', 'universe', 'purpose', 'file_id'):
            value = getattr(self, name)
            require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value)
                    and value.upper() not in {'UNKNOWN', 'UNBOUND', 'NONE'}, 'BINDING_INVALID')
        require(self.subject_permission_id is None or
                (type(self.subject_permission_id) is str and
                 re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.subject_permission_id)
                 and self.subject_permission_id.upper() not in {'UNKNOWN', 'UNBOUND', 'NONE'}),
                'SUBJECT_INVALID')
        require(type(self.client_id) is str and re.fullmatch(
            r'[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com', self.client_id), 'CLIENT_INVALID')
        require(type(self.subject_email) is str and re.fullmatch(
            r'[^\s@]{1,64}@[^\s@]{1,190}', self.subject_email), 'SUBJECT_INVALID')
        require(type(self.windows_sid) is str and re.fullmatch(r'S-1-5-[0-9-]{1,170}', self.windows_sid),
                'WINDOWS_IDENTITY_INVALID')
        require(type(self.app_name) is str and 1 <= len(self.app_name) <= 100
                and all(ord(c) >= 32 for c in self.app_name), 'APP_INVALID')
        require(self.dedicated_test_app is True and self.non_sensitive_test_file is True
                and self.revoke_project_grant_authorized is True, 'TEST_BOUNDARY_UNCONFIRMED')
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            require(expiry.tzinfo is not None, 'EXPIRY_INVALID')
        except (TypeError, ValueError):
            raise Denied('EXPIRY_INVALID') from None

    def active(self, require_enrolled=True):
        require(not require_enrolled or self.subject_permission_id is not None, 'ENROLLMENT_REQUIRED')
        require(datetime.now(timezone.utc) < datetime.fromisoformat(self.expires_at), 'BINDING_EXPIRED')

    @property
    def lifecycle_target(self):
        return 'CompanionMind.ConnectorAlpha.Lifecycle.' + fingerprint(
            {'installation': self.installation, 'windows_sid': self.windows_sid})

    @property
    def credential_target(self):
        """Opaque, per-binding Windows Credential Manager target name.

        This is deliberately derived rather than configured: a config cannot point
        the connector at another application's credential slot.
        """
        return 'CompanionMind.ConnectorAlpha.' + self.digest

    @property
    def digest(self):
        return fingerprint(asdict(self))

    @classmethod
    def load(cls, path):
        try:
            with open(path, 'rb') as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            require(len(raw) <= MAX_CONFIG_BYTES, 'CONFIG_LIMIT')
            return cls(**exact_json(raw.decode('utf-8')))
        except Denied:
            raise
        except Exception:
            raise Denied('CONFIG_INVALID') from None


def receipt(status, reason, **counts):
    # Never include private bindings, provider text, URLs, headers, or secret hashes.
    return dict(profile=PROFILE, origin='OWNED_HOME_WINDOWS_CLI', status=status, reason=reason,
                observed_at=utc(), authority=False, retries=0, fallback_count=0,
                provider_business_writes=0, automatic_actions=0, **counts)
