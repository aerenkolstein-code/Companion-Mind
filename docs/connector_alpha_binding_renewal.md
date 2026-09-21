# Connector Alpha binding renewal

The lifecycle ledger is tied to an installation and Windows identity. It also
checks the complete binding digest, including its expiry. Editing a binding in
place does not renew an old grant and is rejected with `LIFECYCLE_INVALID`.

To renew an expired binding or change its authorized boundary:

1. Keep the original binding configuration and run `revoke` with it. Cleanup
   works after expiry and without an unlocked desktop. Require confirmed local
   closure, credential deletion, and remote revocation before continuing. Resolve
   incomplete cleanup using the original configuration.
2. Create a separate non-secret binding configuration with a new, unique
   `installation` value and the newly authorized expiry/boundary. Set
   `subject_permission_id` to `null` for fresh same-token identity enrollment.
3. Perform a fresh explicit authorization. An old closed ledger stays closed;
   the new installation creates a separate ledger. Do not erase or reset old
   ledgers to force an existing installation to accept a changed binding.

This is the supported manual renewal procedure for Alpha, not an automatic
refresh mechanism. Local code, the Windows user, and administrators remain
trusted; this is not protection against hostile configuration edits by them.

The lifecycle tests cover rejection of changed expiry on an existing
installation, a clean new-installation grant, and continued rejection of the old
grant. They also verify that detecting a locked session before the first read
persists closure, so unlocking cannot revive that grant. Simulated gate checks
do not prove behavior under a physical Windows lock/unlock test.
