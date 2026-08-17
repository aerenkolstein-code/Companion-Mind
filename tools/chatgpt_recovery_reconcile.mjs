import crypto from 'node:crypto';

export const RECOVERY_SCHEMA = 'cm-c2-recovery/v0.1';
export const LEDGER_SCHEMA = 'cm-c2-renderable-ledger/v0.1';
export const RECON_SCHEMA = 'cm-c2-reconciliation/v0.2';
export const GAP_SCHEMA = 'cm-c2-gap-ledger/v0.2';
export const STRICT_TOLERANCE_MS = 2;
export const NEAR_TOLERANCE_MS = 1000;

export class ReconcileError extends Error {}

export function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (!value || typeof value !== 'object') return value;
  const output = {};
  for (const key of Object.keys(value).sort()) output[key] = sortKeys(value[key]);
  return output;
}

export function canonicalString(value) {
  return JSON.stringify(sortKeys(value));
}

export function sha256Hex(value) {
  return crypto.createHash('sha256').update(Buffer.from(canonicalString(value), 'utf8')).digest('hex');
}

export function verifyHashedArtifact(artifact, schemaVersion) {
  if (!artifact || typeof artifact !== 'object' || Array.isArray(artifact)) throw new ReconcileError('artifact root must be an object');
  if (artifact.schema_version !== schemaVersion) throw new ReconcileError(`unsupported schema_version: ${JSON.stringify(artifact.schema_version)}`);
  const stored = artifact.sha256;
  if (typeof stored !== 'string' || !/^[0-9a-f]{64}$/.test(stored)) throw new ReconcileError('artifact sha256 is missing or malformed');
  const body = { ...artifact };
  delete body.sha256;
  const computed = sha256Hex(body);
  if (computed !== stored) throw new ReconcileError(`artifact checksum mismatch (stored=${stored}, computed=${computed})`);
  return stored;
}

export function toEpochMillis(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.abs(value) < 1e11 ? value * 1000 : value;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const asNumber = Number(trimmed);
    if (Number.isFinite(asNumber)) return Math.abs(asNumber) < 1e11 ? asNumber * 1000 : asNumber;
    const parsed = Date.parse(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function bulkRole(node) { return node?.role ?? null; }
function roleMatches(ledgerRole, nodeRole) { return ledgerRole === 'root' ? nodeRole == null : ledgerRole === nodeRole; }

function rootCandidate(nodes) {
  if (nodes.length && nodes[0] && nodes[0].path_index === 0 && bulkRole(nodes[0]) == null) return 0;
  const candidates = [];
  for (let i = 0; i < nodes.length; i += 1) if (bulkRole(nodes[i]) == null && nodes[i]?.create_time == null) candidates.push(i);
  return candidates.length === 1 ? candidates[0] : null;
}

export function reconcileArtifacts(recovery, ledger, { strictToleranceMs = STRICT_TOLERANCE_MS, nearToleranceMs = NEAR_TOLERANCE_MS } = {}) {
  verifyHashedArtifact(recovery, RECOVERY_SCHEMA);
  verifyHashedArtifact(ledger, LEDGER_SCHEMA);
  if (!Array.isArray(recovery.ordered_nodes)) throw new ReconcileError('recovery ordered_nodes must be a list');
  if (!Array.isArray(ledger.entries)) throw new ReconcileError('ledger entries must be a list');
  if (recovery.active_path_size !== recovery.ordered_nodes.length) throw new ReconcileError('recovery active_path_size mismatch');
  if (ledger.ledger_size !== ledger.entries.length) throw new ReconcileError('ledger_size mismatch');
  if (recovery.conversation_id && ledger.conversation_id && recovery.conversation_id !== ledger.conversation_id) throw new ReconcileError('conversation_id mismatch between recovery and renderable ledger');

  const nodes = recovery.ordered_nodes;
  const entries = ledger.entries;
  const matchedNodeIndexes = new Set();
  const resultEntries = [];
  const gaps = [];
  let lastMatchedNodeIndex = -1;

  for (let li = 0; li < entries.length; li += 1) {
    const le = entries[li] || {};
    const role = le.role ?? null;
    const ledgerMs = toEpochMillis(le.turn_date);
    let disposition = 'AMBIGUOUS_MAPPING';
    let reason = '';
    let matchedIndex = null;
    let deltaMs = null;
    let candidateIndexes = [];

    if (role === 'root' && li === 0) {
      const rc = rootCandidate(nodes);
      if (rc !== null && rc > lastMatchedNodeIndex && !matchedNodeIndexes.has(rc)) {
        matchedIndex = rc;
        disposition = 'MATCHED';
        reason = 'root ledger entry matched unique root/no-message graph node';
      } else reason = 'root ledger entry could not be mapped uniquely';
    } else if (ledgerMs === null) {
      reason = 'ledger turn_date is not parseable; refusing role/order-only match';
    } else {
      const strict = [];
      const near = [];
      for (let ni = lastMatchedNodeIndex + 1; ni < nodes.length; ni += 1) {
        if (matchedNodeIndexes.has(ni)) continue;
        const node = nodes[ni];
        if (!roleMatches(role, bulkRole(node))) continue;
        const nodeMs = toEpochMillis(node?.create_time);
        if (nodeMs === null) continue;
        const delta = Math.abs(nodeMs - ledgerMs);
        if (delta <= strictToleranceMs) strict.push({ ni, delta });
        else if (delta <= nearToleranceMs) near.push({ ni, delta });
      }
      candidateIndexes = strict.map((x) => x.ni);
      if (strict.length === 1) {
        matchedIndex = strict[0].ni;
        deltaMs = strict[0].delta;
        disposition = 'MATCHED';
        reason = `same-role timestamp matched within ${strictToleranceMs} ms and preserved order`;
      } else if (strict.length > 1) reason = `multiple same-role timestamp candidates within ${strictToleranceMs} ms`;
      else if (near.length === 0) {
        const fullStrict = [];
        for (let ni = 0; ni < nodes.length; ni += 1) {
          const node = nodes[ni];
          if (!roleMatches(role, bulkRole(node))) continue;
          const nodeMs = toEpochMillis(node?.create_time);
          if (nodeMs === null) continue;
          const delta = Math.abs(nodeMs - ledgerMs);
          if (delta <= strictToleranceMs) fullStrict.push({ ni, delta });
        }
        if (fullStrict.length > 0) {
          candidateIndexes = fullStrict.map((x) => x.ni);
          reason = 'same-role timestamp exists in bulk but violates monotonic order/uniqueness';
        } else {
          disposition = 'MISSING_FROM_BULK';
          reason = `no same-role bulk timestamp within ${nearToleranceMs} ms`;
        }
      } else {
        candidateIndexes = near.map((x) => x.ni);
        reason = `no strict match; ${near.length} same-role candidate(s) exist only within ${nearToleranceMs} ms`;
      }
    }

    if (matchedIndex !== null) {
      matchedNodeIndexes.add(matchedIndex);
      lastMatchedNodeIndex = matchedIndex;
    }
    const nearestBulkNodeIds = [];
    const focus = matchedIndex ?? candidateIndexes[0] ?? Math.max(0, lastMatchedNodeIndex);
    if (nodes.length) {
      const lo = Math.max(0, focus - 1);
      const hi = Math.min(nodes.length, focus + 2);
      for (let i = lo; i < hi; i += 1) nearestBulkNodeIds.push(nodes[i]?.node_id ?? null);
    }
    const out = {
      renderable_index: le.renderable_index ?? li,
      expected_role: role,
      ledger_turn_date: le.turn_date ?? null,
      matched_bulk_path_index: matchedIndex,
      matched_bulk_node_id: matchedIndex !== null ? (nodes[matchedIndex]?.node_id ?? null) : null,
      timestamp_delta_ms: deltaMs,
      candidate_bulk_path_indexes: candidateIndexes.slice(0, 8),
      nearest_bulk_node_ids: nearestBulkNodeIds,
      disposition,
      reason,
    };
    resultEntries.push(out);
    if (disposition === 'MISSING_FROM_BULK' || disposition === 'AMBIGUOUS_MAPPING') gaps.push({ ...out, recommended_backfill_action: disposition === 'MISSING_FROM_BULK' ? 'sidebar_random_access' : 'inspect_stable_identity_or_timestamp_collision' });
  }

  for (let ni = 0; ni < nodes.length; ni += 1) {
    if (matchedNodeIndexes.has(ni)) continue;
    resultEntries.push({ renderable_index: null, expected_role: null, ledger_turn_date: null, matched_bulk_path_index: ni, matched_bulk_node_id: nodes[ni]?.node_id ?? null, timestamp_delta_ms: null, candidate_bulk_path_indexes: [], nearest_bulk_node_ids: [nodes[ni]?.node_id ?? null], disposition: 'EXTRA_GRAPH_NODE', reason: 'active-path graph node has no matched renderable-ledger entry' });
  }

  const summary = { MATCHED: 0, MISSING_FROM_BULK: 0, EXTRA_GRAPH_NODE: 0, AMBIGUOUS_MAPPING: 0 };
  for (const item of resultEntries) summary[item.disposition] += 1;
  const unresolvedGapCount = summary.MISSING_FROM_BULK + summary.AMBIGUOUS_MAPPING;
  const report = {
    schema_version: RECON_SCHEMA,
    method: 'role+timestamp+monotonic-order/v1',
    strict_tolerance_ms: strictToleranceMs,
    near_tolerance_ms: nearToleranceMs,
    recovery_sha256: recovery.sha256,
    ledger_sha256: ledger.sha256,
    conversation_id_match: !recovery.conversation_id || !ledger.conversation_id || recovery.conversation_id === ledger.conversation_id,
    recovery_active_path_size: nodes.length,
    renderable_ledger_size: entries.length,
    summary,
    unresolved_gap_count: unresolvedGapCount,
    entries: resultEntries,
  };
  report.sha256 = sha256Hex(report);
  const gapLedger = { schema_version: GAP_SCHEMA, reconciliation_sha256: report.sha256, gaps };
  gapLedger.sha256 = sha256Hex(gapLedger);
  return { report, gapLedger };
}
