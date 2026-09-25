"""Local S3 composition for fixed, counted identity and A/B reads.

This module has no enrollment or refresh authority. In particular an expired
access token fails closed; a future refresh coordinator needs an unambiguous
expiry signal and a purpose-bound client-secret capability.
"""
from __future__ import annotations

import time

from .credentials import NativeCredentials
from .policy import require
from .provider import GoogleProvider
from .stage import GenerationBinding, StageLifecycle


class S3Runtime:
    """One entry point for counted identity and exact, validated reads.

    The caller supplies an already initialized stage, native credential
    capability, and fixed provider. Tests replace HTTPS at the provider edge.
    """
    def __init__(self, lifecycle, binding, credentials, provider, *, clock=time.time):
        require(type(lifecycle) is StageLifecycle and type(binding) is GenerationBinding
                and type(credentials) is NativeCredentials and credentials.lifecycle is lifecycle
                and credentials.binding == binding and type(provider) is GoogleProvider
                and provider.binding == binding and provider.c_id is None
                and callable(clock), 'RUNTIME_CONFIGURATION_DENIED')
        self._stage, self._binding = lifecycle, binding
        self._credentials, self._provider = credentials, provider
        self._clock = clock

    def _now(self):
        value = self._clock()
        require(type(value) in (int, float), 'RUNTIME_CLOCK_DENIED')
        return int(value)

    def _reserve_all(self, operation_ids, bucket, *, retest_package=None):
        now = self._now()
        for operation_id in operation_ids:
            self._stage.reserve_request(self._binding, operation_id, now=now,
                                        lane='normal', bucket=bucket, retest_package=retest_package)

    def _access(self):
        now = self._now()
        self._stage.assert_active(self._binding, now=now)
        return self._credentials.business(now)

    def identity(self, operation_id, *, retest_package=None):
        with self._stage.lock:
            self._reserve_all((operation_id,), 'initialization', retest_package=retest_package)
            self._provider.identity(self._access())
            return {'status': 'VERIFIED'}

    def metadata(self, operation_id, resource_id, *, retest_package=None):
        require(resource_id in {self._binding.folder_id, self._binding.a_id, self._binding.b_id},
                'RUNTIME_RESOURCE_DENIED')
        with self._stage.lock:
            if resource_id != self._binding.folder_id:
                self._reserve_all((operation_id + '.folder', operation_id + '.resource'),
                                  'A_B_flow', retest_package=retest_package)
                self._provider.metadata(self._access(), self._binding.folder_id)
            else:
                self._reserve_all((operation_id + '.resource',), 'A_B_flow',
                                  retest_package=retest_package)
            self._provider.metadata(self._access(), resource_id)
            return {'status': 'VERIFIED', 'resource_id': resource_id}

    def document(self, operation_id, resource_id, *, retest_package=None):
        require(resource_id in {self._binding.a_id, self._binding.b_id}, 'RUNTIME_RESOURCE_DENIED')
        with self._stage.lock:
            self._reserve_all((operation_id + '.folder', operation_id + '.resource', operation_id + '.document'),
                              'A_B_flow', retest_package=retest_package)
            self._provider.metadata(self._access(), self._binding.folder_id)
            self._provider.metadata(self._access(), resource_id)
            snapshot = self._provider.document(self._access(), resource_id)
            return {'status': 'READ', 'resource_id': resource_id,
                    'revision': snapshot.revision, 'text_sha256': snapshot.text_hash}
