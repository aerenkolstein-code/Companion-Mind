import unittest
from companion_mind.connector_s3.policy import Binding, Controller, Denied, Limits, MemoryState


def binding(): return Binding(1, 'folder', 'file-a', 'file-b', 'client', 'project', 'S-1-5-21-1')


class S3Policy(unittest.TestCase):
    def setUp(self): self.store = MemoryState(); self.c = Controller(binding(), self.store); self.c.initialize()
    def test_unknown_dispatch_consumes_budget_and_cannot_replay(self):
        epoch = self.c.reserve('write-a', lane='normal', bucket='A_B_flow', file='A'); self.c.dispatch('write-a', epoch); self.c.complete('write-a', 'UNKNOWN')
        self.assertEqual(self.store.read()['writes']['A'], 1)
        with self.assertRaisesRegex(Denied, 'FILE_UNKNOWN_RECONCILIATION_REQUIRED'): self.c.reserve('write-a-again', lane='normal', bucket='A_B_flow', file='A')
    def test_lane_and_file_limits_fail_before_new_dispatch(self):
        limited = Controller(binding(), MemoryState(), Limits(api_total=2, api_normal=1, api_safety=1, writes_a=1,
                             ordinary_buckets=(('initialization', 1),), safety_buckets=(('revoke', 1),)))
        limited.initialize(); limited.reserve('a', lane='normal', bucket='initialization', file='A')
        with self.assertRaisesRegex(Denied, 'API_LANE_EXHAUSTED'): limited.reserve('b', lane='normal', bucket='initialization')
        with self.assertRaisesRegex(Denied, 'WRITE_BUDGET_EXHAUSTED'): limited.reserve('c', lane='safety', bucket='revoke', file='A')
    def test_revoke_epoch_blocks_every_future_dispatch(self):
        self.c.revoke_local()
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'): self.c.reserve('after', lane='safety', bucket='revoke')
    def test_prepared_or_dispatched_crash_is_recovery_required(self):
        self.store.read()['operations']['lost'] = {'state': 'DISPATCHED', 'epoch': 0}
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'): self.c.recover()
    def test_bad_binding_or_ledger_never_initializes_a_new_budget(self):
        with self.assertRaisesRegex(Denied, 'BINDING_INVALID'): Binding(1, 'same', 'same', 'b', 'c', 'p', 'sid')
        self.store.raw = {'format': 'connector-s3/1'}
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'): self.c.reserve('x', lane='normal', bucket='initialization')

    def test_c_unknown_create_cannot_be_retried_or_recreated(self):
        epoch = self.c.reserve('create-c', lane='normal', bucket='C_flow', file='C', create=True); self.c.dispatch('create-c', epoch)
        self.c.complete('create-c', 'UNKNOWN')
        with self.assertRaisesRegex(Denied, 'FILE_UNKNOWN_RECONCILIATION_REQUIRED'):
            self.c.reserve('create-c-again', lane='normal', bucket='C_flow', file='C', create=True)

    def test_oauth_is_charged_before_browser_and_revoke_allows_only_cleanup(self):
        self.c.start_oauth('browser-start')
        self.assertEqual(self.store.read()['oauth'], 1)
        self.c.revoke_local()
        with self.assertRaisesRegex(Denied, 'GRANT_REVOKED'): self.c.reserve('new-work', lane='normal', bucket='initialization')
        epoch = self.c.reserve('cleanup', lane='safety', bucket='revoke', cleanup=True)
        self.c.dispatch('cleanup', epoch)

    def test_nonrollback_file_cap_blocks_sixth_a_write_before_dispatch(self):
        for number in range(5):
            epoch = self.c.reserve('a-%d' % number, lane='normal', bucket='A_B_flow', file='A')
            self.c.dispatch('a-%d' % number, epoch); self.c.complete('a-%d' % number, 'VERIFIED')
        with self.assertRaisesRegex(Denied, 'NONROLLBACK_BUDGET_EXHAUSTED'):
            self.c.reserve('a-six', lane='normal', bucket='A_B_flow', file='A')

    def test_revoke_between_reserve_and_dispatch_blocks_old_ticket(self):
        epoch = self.c.reserve('pending', lane='normal', bucket='initialization')
        self.c.revoke_local()
        with self.assertRaisesRegex(Denied, 'DISPATCH_DENIED'): self.c.dispatch('pending', epoch)

    def test_malformed_operations_and_inconsistent_counters_fail_closed(self):
        self.store.raw['operations']['broken'] = 1
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'): self.c.recover()
        self.store.raw['operations'] = {}
        self.store.raw['api_total'] = 1
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'): self.c.recover()

    def test_malformed_container_fails_closed(self):
        self.store.raw['writes'] = None
        with self.assertRaisesRegex(Denied, 'RECOVERY_REQUIRED'): self.c.recover()


if __name__ == '__main__': unittest.main()
