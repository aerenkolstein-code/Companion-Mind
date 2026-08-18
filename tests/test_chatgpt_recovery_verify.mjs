import assert from "node:assert/strict";
import crypto from "node:crypto";
import { verifyArtifact } from "../tools/chatgpt_recovery_verify.mjs";

function exporterSortKeys(value) {
  if (Array.isArray(value)) return value.map(exporterSortKeys);
  if (!value || typeof value !== "object") return value;
  const output = {};
  for (const key of Object.keys(value).sort()) output[key] = exporterSortKeys(value[key]);
  return output;
}

function exporterHash(value) {
  const canonical = JSON.stringify(exporterSortKeys(value));
  return crypto.createHash("sha256").update(Buffer.from(canonical, "utf8")).digest("hex");
}

const body = {
  schema_version: "cm-c2-recovery/v0.1",
  adapter_version: "chatgpt-web-queryclient/v0.1",
  source: "chatgpt-web-queryclient-memory",
  conversation_id: "synthetic",
  exported_at: "2026-08-17T18:00:00.000Z",
  mapping_size: 4,
  active_path_size: 2,
  current_node: "a1",
  conversation_envelope: {
    tiny: 0.000001,
    smaller: 1e-7,
    numeric_keys: { "10": "ten", "2": "two", z: "last" }
  },
  ordered_nodes: [
    { path_index: 0, node_id: "root", parent_id: null, children_ids: ["a1"], role: null, content_type: null, create_time: null, message_id: null, content_parts: null, metadata: {} },
    { path_index: 1, node_id: "a1", parent_id: "root", children_ids: [], role: "assistant", content_type: "text", create_time: 1e-7, message_id: "m1", content_parts: ["ok"], metadata: { "10": "ten", "2": "two" } }
  ]
};
const artifact = { ...body, sha256: exporterHash(body) };
const receipt = verifyArtifact(artifact);
assert.equal(receipt.status, "PASS");
assert.equal(receipt.sha256, artifact.sha256);
assert.equal(receipt.canonicalization, "exact-exporter-js/v1");

const mutated = structuredClone(artifact);
mutated.ordered_nodes[1].role = "user";
assert.throws(() => verifyArtifact(mutated), /checksum mismatch/);

console.log("chatgpt_recovery_verify.mjs: PASS");
