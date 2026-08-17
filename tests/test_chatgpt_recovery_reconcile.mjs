import assert from 'node:assert/strict';
import {
  RECOVERY_SCHEMA, LEDGER_SCHEMA, reconcileArtifacts, sha256Hex
} from '../tools/chatgpt_recovery_reconcile.mjs';

function seal(value) {
  const copy = structuredClone(value);
  copy.sha256 = sha256Hex(copy);
  return copy;
}

function baseRecovery() {
  return seal({
    schema_version: RECOVERY_SCHEMA,
    conversation_id: 'c1',
    mapping_size: 6,
    active_path_size: 6,
    ordered_nodes: [
      { path_index: 0, node_id: 'root', role: null, create_time: null },
      { path_index: 1, node_id: 'u1', role: 'user', create_time: 1000.0 },
      { path_index: 2, node_id: 'tool1', role: 'tool', create_time: 1000.1 },
      { path_index: 3, node_id: 'a1-internal', role: 'assistant', create_time: 1000.2 },
      { path_index: 4, node_id: 'a1', role: 'assistant', create_time: 1001.0 },
      { path_index: 5, node_id: 'u-extra', role: 'user', create_time: 1002.0 },
    ]
  });
}

function baseLedger() {
  return seal({
    schema_version: LEDGER_SCHEMA,
    conversation_id: 'c1',
    ledger_size: 3,
    entries: [
      { renderable_index: 0, role: 'root', turn_date: null },
      { renderable_index: 1, role: 'user', turn_date: new Date(1000_000).toISOString() },
      { renderable_index: 2, role: 'assistant', turn_date: new Date(1001_000).toISOString() },
    ]
  });
}

{
  const { report, gapLedger } = reconcileArtifacts(baseRecovery(), baseLedger());
  assert.deepEqual(report.summary, { MATCHED: 3, MISSING_FROM_BULK: 0, EXTRA_GRAPH_NODE: 3, AMBIGUOUS_MAPPING: 0 });
  assert.equal(report.unresolved_gap_count, 0);
  assert.equal(gapLedger.gaps.length, 0);
}

{
  const ledger = baseLedger();
  ledger.entries[2].turn_date = new Date(9999_000).toISOString();
  delete ledger.sha256;
  ledger.sha256 = sha256Hex(ledger);
  const { report } = reconcileArtifacts(baseRecovery(), ledger);
  assert.equal(report.summary.MISSING_FROM_BULK, 1);
}

{
  const recovery = baseRecovery();
  recovery.ordered_nodes.splice(4, 0, { path_index: 4, node_id: 'a1dup', role: 'assistant', create_time: 1001.001 });
  recovery.active_path_size = recovery.ordered_nodes.length;
  recovery.ordered_nodes.forEach((x, i) => { x.path_index = i; });
  delete recovery.sha256;
  recovery.sha256 = sha256Hex(recovery);
  const { report } = reconcileArtifacts(recovery, baseLedger());
  assert.equal(report.summary.AMBIGUOUS_MAPPING, 1);
}

{
  const ledger = baseLedger();
  ledger.conversation_id = 'other';
  delete ledger.sha256;
  ledger.sha256 = sha256Hex(ledger);
  assert.throws(() => reconcileArtifacts(baseRecovery(), ledger), /conversation_id mismatch/);
}

{
  const recovery = baseRecovery();
  recovery.ordered_nodes[1].role = 'assistant';
  assert.throws(() => reconcileArtifacts(recovery, baseLedger()), /checksum mismatch/);
}

console.log('C2 reconciliation synthetic tests: PASS');
