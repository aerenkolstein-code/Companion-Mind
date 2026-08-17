#!/usr/bin/env node
import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCHEMA_VERSION = "cm-c2-recovery/v0.1";
const FORBIDDEN_KEY_NAMES = new Set([
  "authorization", "proxy_authorization", "cookie", "set_cookie", "csrf", "csrf_token",
  "x_csrf_token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
  "password", "passwd", "otp"
]);

class VerificationError extends Error {}

function normalizeKey(key) {
  return String(key).trim().toLowerCase().replaceAll("-", "_");
}

function credentialSurfacePaths(value, pathValue = "$") {
  const hits = [];
  if (Array.isArray(value)) {
    value.forEach((child, index) => hits.push(...credentialSurfacePaths(child, `${pathValue}[${index}]`)));
    return hits;
  }
  if (!value || typeof value !== "object") return hits;
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_KEY_NAMES.has(normalizeKey(key))) hits.push(`${pathValue}.${key}`);
    hits.push(...credentialSurfacePaths(child, `${pathValue}.${key}`));
  }
  return hits;
}

// IMPORTANT: intentionally identical to the exporter canonicalization.
function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (!value || typeof value !== "object") return value;
  const output = {};
  for (const key of Object.keys(value).sort()) output[key] = sortKeys(value[key]);
  return output;
}

function canonicalString(value) {
  return JSON.stringify(sortKeys(value));
}

function sha256Hex(value) {
  return crypto.createHash("sha256").update(Buffer.from(canonicalString(value), "utf8")).digest("hex");
}

export function verifyArtifact(artifact) {
  if (!artifact || typeof artifact !== "object" || Array.isArray(artifact)) {
    throw new VerificationError("artifact root must be an object");
  }
  if (artifact.schema_version !== SCHEMA_VERSION) {
    throw new VerificationError(`unsupported schema_version: ${JSON.stringify(artifact.schema_version)}`);
  }
  const storedHash = artifact.sha256;
  if (typeof storedHash !== "string" || !/^[0-9a-f]{64}$/.test(storedHash)) {
    throw new VerificationError("artifact sha256 is missing or malformed");
  }

  const body = { ...artifact };
  delete body.sha256;
  const computedHash = sha256Hex(body);
  if (computedHash !== storedHash) {
    throw new VerificationError(`artifact checksum mismatch under exact exporter JS canonicalization (stored=${storedHash}, computed=${computedHash})`);
  }

  if (!Array.isArray(artifact.ordered_nodes)) {
    throw new VerificationError("artifact ordered_nodes must be a list");
  }
  if (artifact.active_path_size !== artifact.ordered_nodes.length) {
    throw new VerificationError("active_path_size does not match ordered_nodes");
  }
  const hits = credentialSurfacePaths(artifact);
  if (hits.length) {
    throw new VerificationError(`credential/header-shaped surface detected: ${hits.slice(0, 5).join(", ")}`);
  }

  return {
    status: "PASS",
    schema_version: artifact.schema_version,
    mapping_size: artifact.mapping_size,
    active_path_size: artifact.ordered_nodes.length,
    sha256: storedHash,
    credential_surface_count: 0,
    canonicalization: "exact-exporter-js/v1"
  };
}

function main(argv = process.argv.slice(2)) {
  if (argv.length !== 1) {
    console.log(JSON.stringify({ status: "FAIL_CLOSED", error: "usage: node chatgpt_recovery_verify.mjs <artifact.json>" }));
    return 2;
  }
  try {
    const text = fs.readFileSync(argv[0], "utf8");
    const artifact = JSON.parse(text);
    console.log(JSON.stringify(verifyArtifact(artifact)));
    return 0;
  } catch (error) {
    console.log(JSON.stringify({ status: "FAIL_CLOSED", error: error?.message || String(error) }));
    return 2;
  }
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isMain) process.exitCode = main();
