import unittest

from companion_mind.connector_s3.policy import Denied, Limits, MemoryState
from companion_mind.connector_s3.stage import GenerationBinding, StageLifecycle


def bind(generation=1, *, scope='drive.file', expires_at=500, issued_at=0):
    return GenerationBinding(generation, 'account', 'subject', 'googleapis.com', 'client',
                             'project', scope, 'synthetic-sid', issued_at, expires_at,
                             'folder', 'file-a', 'file-b')


class StageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryState()
        self.one = bind()
        self.stage = StageLifecycle(self.one, self.store)
        self.stage.initialize()

    def test_closed_generation_cannot_revive_and_next_preserves_totals(self):
        self.stage.start_oauth(self.one, 'oauth-1', now=10)
        self.stage.reserve_request(self.one, 'write-a', now=10, lane='normal', bucket='A_B_flow', file='A')
        self.stage.close_generation(1)
        with self.assertRaisesRegex(Denied, 'GENERATION_CLOSED'):
            self.stage.start_oauth(self.one, 'old', now=10)
        two = bind(2, expires_at=600)
        self.stage.start_next_generation(two)
        reopened = StageLifecycle(two, self.store)
        reopened.reserve_request(two, 'refresh-2', now=11, lane='normal', bucket='continuity', refresh=True)
        totals = reopened.snapshot()['totals']
        self.assertEqual((totals['oauth'], totals['refresh'], totals['writes']['A']), (1, 1, 1))

    def test_next_generation_cannot_expand_immutable_scope(self):
        self.stage.close_generation(1)
        with self.assertRaisesRegex(Denied, 'GENERATION_SCOPE_EXPANSION'):
            self.stage.start_next_generation(GenerationBinding(2, 'other-account', 'subject', 'googleapis.com',
                'client', 'project', 'drive.file', 'synthetic-sid', 0, 600, 'folder', 'file-a', 'file-b'))
        with self.assertRaisesRegex(Denied, 'BINDING_INVALID'):
            bind(2, scope='drive.readonly')

    def test_stage_oauth_and_reconnect_limits_do_not_reset(self):
        self.stage.start_oauth(self.one, 'o0', now=1)
        with self.assertRaisesRegex(Denied, 'RECONNECT_CLASSIFICATION_DENIED'):
            self.stage.start_oauth(self.one, 'hidden-reconnect', now=1, reconnect=False)
        for number in range(3):
            self.stage.start_oauth(self.one, 'r%d' % number, now=1, reconnect=True)
        self.stage.close_generation(1); two = bind(2); self.stage.start_next_generation(two)
        with self.assertRaisesRegex(Denied, 'OAUTH_BUDGET_EXHAUSTED'):
            self.stage.start_oauth(two, 'o5', now=1)
        with self.assertRaisesRegex(Denied, 'OAUTH_BUDGET_EXHAUSTED'):
            self.stage.start_oauth(two, 'r4', now=1, reconnect=True)

    def test_reconnect_cap_is_observable_when_total_oauth_remains_available(self):
        store = MemoryState()
        lifecycle = StageLifecycle(bind(), store, Limits(oauth=5))
        lifecycle.initialize()
        lifecycle.start_oauth(bind(), 'initial', now=1)
        for number in range(3):
            lifecycle.start_oauth(bind(), 'reconnect-%d' % number, now=1, reconnect=True)
        with self.assertRaisesRegex(Denied, 'RECONNECT_BUDGET_EXHAUSTED'):
            lifecycle.start_oauth(bind(), 'reconnect-4', now=1, reconnect=True)

    def test_retest_caps_consume_stage_pool_and_are_per_package(self):
        self.stage.start_oauth(self.one, 'rt-oauth', now=1, retest_package=1)
        with self.assertRaisesRegex(Denied, 'RETEST_PACKAGE_EXHAUSTED'):
            self.stage.start_oauth(self.one, 'rt-oauth-again', now=1, retest_package=1)
        for number in range(2):
            self.stage.reserve_request(self.one, 'rt-n%d' % number, now=1, lane='normal', bucket='A_B_flow',
                                       file='A', retest_package=1)
        self.stage.reserve_request(self.one, 'rt-r', now=1, lane='normal', bucket='A_B_flow',
                                   file='A', rollback=True, retest_package=1)
        with self.assertRaisesRegex(Denied, 'RETEST_PACKAGE_EXHAUSTED'):
            self.stage.reserve_request(self.one, 'rt-r-again', now=1, lane='normal', bucket='A_B_flow',
                                       file='A', rollback=True, retest_package=1)
        self.stage.reserve_request(self.one, 'package-two', now=1, lane='normal', bucket='A_B_flow',
                                   file='B', retest_package=2)
        self.assertEqual(self.stage.snapshot()['totals']['retest']['2']['api'], 1)

    def test_retest_api_cap_refresh_and_c_create_stay_stage_cumulative(self):
        for number in range(20):
            self.stage.reserve_request(self.one, 'api-%d' % number, now=1, lane='normal',
                                       bucket='initialization', retest_package=1)
        with self.assertRaisesRegex(Denied, 'RETEST_PACKAGE_EXHAUSTED'):
            self.stage.reserve_request(self.one, 'api-21', now=1, lane='normal',
                                       bucket='initialization', retest_package=1)
        self.stage.reserve_request(self.one, 'create-c', now=1, lane='normal', bucket='C_flow',
                                   file='C', create=True)
        for number in range(6):
            self.stage.reserve_request(self.one, 'refresh-%d' % number, now=1, lane='normal',
                                       bucket='continuity', refresh=True)
        self.stage.close_generation(1); two = bind(2); self.stage.start_next_generation(two)
        with self.assertRaisesRegex(Denied, 'CREATE_BUDGET_EXHAUSTED'):
            self.stage.reserve_request(two, 'create-c-again', now=1, lane='normal', bucket='C_flow',
                                       file='C', create=True)
        with self.assertRaisesRegex(Denied, 'REFRESH_BUDGET_EXHAUSTED'):
            self.stage.reserve_request(two, 'refresh-7', now=1, lane='normal', bucket='continuity', refresh=True)

    def test_expiry_and_corrupt_totals_fail_closed(self):
        with self.assertRaisesRegex(Denied, 'GENERATION_EXPIRED'):
            self.stage.start_oauth(self.one, 'expired', now=500)
        self.store.raw['totals']['oauth'] = 9
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.stage.snapshot()
        with self.assertRaisesRegex(Denied, 'BINDING_INVALID'):
            bind(expires_at=72 * 60 * 60 + 1)

    def test_operations_rebuild_every_counter_and_rejects_bad_schema(self):
        self.stage.start_oauth(self.one, 'oauth', now=1)
        self.stage.reserve_request(self.one, 'request', now=1, lane='normal', bucket='A_B_flow',
                                   file='A', retest_package=1)
        valid = self.stage.snapshot()
        self.store.raw['totals'] = self.stage._totals()
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.stage.snapshot()
        self.store.raw['totals'] = valid['totals']
        self.store.raw['operations']['request']['lane'] = []
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'):
            self.stage.snapshot()

    def test_local_rejection_never_persists_partial_retest_charge(self):
        before = self.stage.snapshot()
        with self.assertRaisesRegex(Denied, 'CREATE_DENIED'):
            self.stage.reserve_request(self.one, 'bad', now=1, lane='normal', bucket='A_B_flow',
                                       file='A', create=True, retest_package=1)
        self.assertEqual(self.stage.snapshot(), before)

    def test_initialization_is_generation_one_and_generation_cap_is_finite(self):
        with self.assertRaisesRegex(Denied, 'GENERATION_INITIALIZATION_DENIED'):
            StageLifecycle(bind(2), MemoryState()).initialize()
        current = self.stage
        for number in range(1, 4):
            current.close_generation(number)
            successor = bind(number + 1)
            current.start_next_generation(successor)
            current = StageLifecycle(successor, self.store)
        current.close_generation(4)
        with self.assertRaisesRegex(Denied, 'GENERATION_SCOPE_EXPANSION'):
            current.start_next_generation(bind(5))

    def test_persisted_revoke_closes_business_refresh_but_allows_fixed_cleanup(self):
        self.stage.revoke_local(self.one)
        reopened = StageLifecycle(self.one, self.store)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            reopened.start_oauth(self.one, 'new-oauth', now=1)
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'):
            reopened.reserve_request(self.one, 'refresh', now=1, lane='normal', bucket='continuity', refresh=True)
        reopened.reserve_cleanup(self.one, 'cleanup', now=1)
        cleanup = reopened.snapshot()['operations']['cleanup']
        self.assertEqual((cleanup['lane'], cleanup['bucket'], cleanup['cleanup']), ('safety', 'revoke', True))

    def test_manual_reconnect_keeps_budget_but_requires_verified_consent(self):
        self.stage.start_oauth(self.one, 'first', now=1)
        self.stage.revoke_local(self.one)
        two = bind(2)
        self.stage.begin_manual_reconnect(two)
        with self.assertRaisesRegex(Denied, 'GENERATION_CLOSED'):
            self.stage.reserve_request(two, 'business-too-early', now=1, lane='normal', bucket='continuity')
        self.stage.start_oauth(two, 'second', now=1)
        self.stage.activate_after_verified_consent(two, 'a' * 64)
        self.stage.reserve_request(two, 'after-proof', now=1, lane='normal', bucket='continuity')
        self.assertEqual(self.stage.snapshot()['totals']['oauth'], 2)

    def test_write_intent_pin_survives_reopen_and_must_match_generation(self):
        self.stage.pin_write_intent(self.one, 'pin-a', now=1, resource='file-a', revision='r1',
                                    intent_hash='a' * 64, recovery_hash='b' * 64)
        self.stage.reserve_request(self.one, 'write-a', now=1, lane='normal', bucket='A_B_flow',
                                   file='A', intent_ref='pin-a', intent_hash='a' * 64,
                                   intent_resource='file-a', intent_revision='r1')
        self.assertEqual(self.stage.snapshot()['operations']['write-a']['intent_ref'], 'pin-a')
        with self.assertRaisesRegex(Denied, 'INTENT_PIN_MISMATCH'):
            self.stage.reserve_request(self.one, 'missing-pin', now=1, lane='normal', bucket='A_B_flow',
                                       file='A', intent_ref='missing')


if __name__ == '__main__':
    unittest.main()
