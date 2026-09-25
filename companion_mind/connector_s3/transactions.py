"""Non-secret transaction records and outstanding restoration reservations."""
import copy
from .policy import require

RESTORE_BUCKET = 'restore_existing_and_readback'
PHASES = {'WRITING', 'VERIFIED', 'RESTORING', 'RESTORED', 'UNKNOWN'}


def hash_string(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def validate_receipt(receipt, operation_id, binding_hash):
    require(type(receipt) is dict and set(receipt) == {
        'operation_id', 'binding_hash', 'cipher_hash', 'plain_hash', 'cipher_size'}
        and receipt['operation_id'] == operation_id and receipt['binding_hash'] == binding_hash
        and all(hash_string(receipt[k]) for k in ('binding_hash', 'cipher_hash', 'plain_hash'))
        and type(receipt['cipher_size']) is int and 0 < receipt['cipher_size'] <= 2162688,
        'TRANSACTION_RECORD_INVALID')


def validate_transactions(data):
    from .docs_plan import canonical, digest
    require(type(data['transactions']) is dict, 'TRANSACTION_RECORD_INVALID')
    open_files = set()
    for key, tx in data['transactions'].items():
        require(type(key) is str and 1 <= len(key) <= 80 and type(tx) is dict and set(tx) == {
            'generation', 'file', 'resource', 'receipt', 'intent_hash', 'revision', 'phase',
            'held_api', 'held_write', 'retest_package', 'restore_receipt'}, 'TRANSACTION_RECORD_INVALID')
        require(type(tx['generation']) is int and str(tx['generation']) in data['generations']
                and type(tx['file']) is str and tx['file'] in {'A', 'B'}
                and tx['resource'] == data['stage'][tx['file'].lower() + '_id']
                and type(tx['phase']) is str and tx['phase'] in PHASES
                and type(tx['revision']) is str and bool(tx['revision']) and hash_string(tx['intent_hash'])
                and type(tx['held_api']) is int and 0 <= tx['held_api'] <= 5
                and type(tx['held_write']) is int and tx['held_write'] in {0, 1}
                and (tx['retest_package'] is None or type(tx['retest_package']) is int
                     and tx['retest_package'] in {1, 2}), 'TRANSACTION_RECORD_INVALID')
        bound_hash = digest(canonical(data['generations'][str(tx['generation'])]['binding']))
        validate_receipt(tx['receipt'], key, bound_hash)
        pin = data['operations'].get(key + '.pin')
        write = data['operations'].get(key + '.write')
        require(type(pin) is dict and pin.get('kind') == 'WRITE_INTENT'
                and (pin['generation'], pin['resource'], pin['revision'], pin['intent_hash'], pin['recovery_hash']) ==
                    (tx['generation'], tx['resource'], tx['revision'], tx['intent_hash'], digest(canonical(tx['receipt'])))
                and type(write) is dict and write.get('kind') == 'REQUEST'
                and write.get('intent_ref') == key + '.pin' and write.get('file') == tx['file']
                and not write.get('rollback'), 'TRANSACTION_RECORD_INVALID')
        if tx['restore_receipt'] is not None:
            validate_receipt(tx['restore_receipt'], key + '.restore', bound_hash)
            restore_pin = data['operations'].get(key + '.restore.pin')
            restore_write = data['operations'].get(key + '.restore.write')
            require(type(restore_pin) is dict and restore_pin.get('kind') == 'WRITE_INTENT'
                    and restore_pin['recovery_hash'] == digest(canonical(tx['restore_receipt']))
                    and type(restore_write) is dict and restore_write.get('intent_ref') == key + '.restore.pin'
                    and restore_write.get('rollback') is True and restore_write.get('file') == tx['file'],
                    'TRANSACTION_RECORD_INVALID')
        if tx['phase'] in {'WRITING', 'VERIFIED'}:
            require(tx['held_api'] == 5 and tx['held_write'] == 1
                    and tx['restore_receipt'] is None, 'TRANSACTION_RECORD_INVALID')
        if tx['phase'] == 'RESTORED':
            require(tx['held_api'] == tx['held_write'] == 0, 'TRANSACTION_RECORD_INVALID')
        else:
            require(tx['resource'] not in open_files, 'TRANSACTION_RECORD_INVALID')
            open_files.add(tx['resource'])


def spendable_totals(data):
    """Treat held restoration capacity as unavailable to every other request."""
    totals = copy.deepcopy(data['totals'])
    for tx in data['transactions'].values():
        count, write = tx['held_api'], tx['held_write']
        totals['api_total'] += count
        totals['api_safety'] += count
        totals['buckets'][RESTORE_BUCKET] += count
        totals['writes'][tx['file']] += write
        totals['rollbacks'][tx['file']] += write
        if tx['retest_package'] is not None:
            package = totals['retest'][str(tx['retest_package'])]
            package['api'] += count
            package['rollback'] += write
    return totals


def require_file_available(data, file):
    require(not any(tx['file'] == file and tx['phase'] != 'RESTORED'
                    for tx in data['transactions'].values()), 'FILE_RECOVERY_REQUIRED')
