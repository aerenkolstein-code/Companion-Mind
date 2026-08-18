/*
 * C2 read-only ChatGPT Web recovery adapter.
 *
 * Paste this file into DevTools on a ChatGPT conversation you own, then run:
 *   await CM_C2_RECOVERY.run({ download: true })
 *
 * It performs no fetch/XHR and never calls QueryClient mutation methods.
 */
(function installC2Recovery(global) {
  "use strict";

  const VERSION = "chatgpt-web-queryclient/v0.1";
  const SCHEMA_VERSION = "cm-c2-recovery/v0.1";
  const TURN_SELECTOR = "[data-turn-id-container], [data-message-author-role], [data-turn]";
  const FORBIDDEN_KEY_NAMES = new Set([
    "authorization", "proxy_authorization", "cookie", "set_cookie", "csrf", "csrf_token",
    "x_csrf_token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "password", "passwd", "otp"
  ]);

  class C2RecoveryError extends Error {
    constructor(message) {
      super(message);
      this.name = "C2RecoveryError";
    }
  }

  function ownReactFiber(element) {
    for (let current = element; current; current = current.parentElement) {
      for (const key of Object.getOwnPropertyNames(current)) {
        if (key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$")) {
          return current[key];
        }
      }
    }
    throw new C2RecoveryError("no React Fiber handle found on a materialized turn or ancestor");
  }

  function looksLikeQueryClient(value) {
    return !!value && typeof value === "object" &&
      typeof value.getQueryCache === "function" &&
      typeof value.getQueryData === "function" &&
      typeof value.getQueriesData === "function" &&
      typeof value.getQueryState === "function";
  }

  function queryClientCandidatesFromFiber(fiber) {
    const candidates = [];
    for (let node = fiber; node; node = node.return) {
      const props = [node.memoizedProps, node.pendingProps];
      for (const candidate of props) {
        if (looksLikeQueryClient(candidate)) candidates.push(candidate);
        if (candidate && typeof candidate === "object" && looksLikeQueryClient(candidate.client)) {
          candidates.push(candidate.client);
        }
      }

      let dep = node.dependencies && node.dependencies.firstContext;
      let depCount = 0;
      while (dep && depCount < 64) {
        const context = dep.context;
        if (context && typeof context === "object") {
          for (const value of [context._currentValue, context._currentValue2]) {
            if (looksLikeQueryClient(value)) candidates.push(value);
            if (value && typeof value === "object" && looksLikeQueryClient(value.client)) {
              candidates.push(value.client);
            }
          }
        }
        dep = dep.next;
        depCount += 1;
      }
    }
    return [...new Set(candidates)];
  }

  function discoverQueryClient() {
    const turn = document.querySelector(TURN_SELECTOR);
    if (!turn) throw new C2RecoveryError("no materialized turn found; open a conversation and materialize at least one turn");
    const fiber = ownReactFiber(turn);
    const candidates = queryClientCandidatesFromFiber(fiber);
    if (candidates.length === 0) throw new C2RecoveryError("QueryClient not found from React Fiber/context chain");
    if (candidates.length > 1) {
      const signatures = candidates.map((client) => {
        try { return client.getQueryCache().getAll().length; } catch (_) { return -1; }
      });
      const max = Math.max(...signatures);
      const best = candidates.filter((_, index) => signatures[index] === max);
      if (best.length !== 1) throw new C2RecoveryError(`multiple QueryClient candidates remain ambiguous (${candidates.length})`);
      return best[0];
    }
    return candidates[0];
  }

  function conversationIdFromLocation() {
    const match = global.location && global.location.pathname.match(/\/c\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function isConversationPayload(data) {
    return !!data && typeof data === "object" && data.mapping && typeof data.mapping === "object" &&
      typeof data.current_node === "string" && data.current_node.length > 0;
  }

  function findExactConversationQuery(queryClient) {
    const currentId = conversationIdFromLocation();
    const all = queryClient.getQueryCache().getAll();
    const candidates = all.filter((query) => query && query.state && query.state.status === "success" && isConversationPayload(query.state.data));
    if (candidates.length === 0) throw new C2RecoveryError("no successful exact-conversation query with mapping/current_node found");

    if (currentId) {
      const exact = candidates.filter((query) => {
        const key = Array.isArray(query.queryKey) ? query.queryKey : [];
        const data = query.state.data;
        return key.includes(currentId) || data.id === currentId || data.conversation_id === currentId;
      });
      if (exact.length === 1) return exact[0];
      if (exact.length > 1) throw new C2RecoveryError(`multiple successful conversation queries match current URL id (${exact.length})`);
    }
    if (candidates.length === 1) return candidates[0];
    throw new C2RecoveryError(`conversation query is ambiguous (${candidates.length}); refusing to guess`);
  }

  function cloneJson(value, label) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (error) {
      throw new C2RecoveryError(`${label} is not JSON serializable: ${error && error.message ? error.message : error}`);
    }
  }

  function normalizeKey(key) {
    return String(key).trim().toLowerCase().replaceAll("-", "_");
  }

  function credentialSurfacePaths(value, path = "$") {
    const hits = [];
    if (Array.isArray(value)) {
      value.forEach((child, index) => hits.push(...credentialSurfacePaths(child, `${path}[${index}]`)));
      return hits;
    }
    if (!value || typeof value !== "object") return hits;
    for (const [key, child] of Object.entries(value)) {
      if (FORBIDDEN_KEY_NAMES.has(normalizeKey(key))) hits.push(`${path}.${key}`);
      hits.push(...credentialSurfacePaths(child, `${path}.${key}`));
    }
    return hits;
  }

  function reconstructActivePath(mapping, currentNode) {
    if (!mapping || typeof mapping !== "object") throw new C2RecoveryError("mapping is absent or invalid");
    if (typeof currentNode !== "string" || !currentNode) throw new C2RecoveryError("current_node is absent or invalid");
    const reversed = [];
    const seen = new Set();
    let nodeId = currentNode;
    while (nodeId !== null) {
      if (seen.has(nodeId)) throw new C2RecoveryError(`cycle detected at ${nodeId}`);
      seen.add(nodeId);
      const node = mapping[nodeId];
      if (!node || typeof node !== "object") throw new C2RecoveryError(`missing parent/node ${nodeId}`);
      reversed.push([nodeId, node]);
      const parent = node.parent;
      if (parent === null || parent === undefined || parent === "") nodeId = null;
      else if (typeof parent === "string") nodeId = parent;
      else throw new C2RecoveryError(`node ${nodeId} has a non-string parent`);
    }
    return reversed.reverse();
  }

  function nodeRecord(pathIndex, nodeId, node) {
    const message = node.message && typeof node.message === "object" ? node.message : {};
    const author = message.author && typeof message.author === "object" ? message.author : {};
    const content = message.content && typeof message.content === "object" ? message.content : {};
    const children = node.children == null ? [] : node.children;
    if (!Array.isArray(children) || children.some((item) => typeof item !== "string")) {
      throw new C2RecoveryError(`node ${nodeId} has invalid children`);
    }
    return cloneJson({
      path_index: pathIndex,
      node_id: nodeId,
      parent_id: node.parent ?? null,
      children_ids: children,
      role: author.role ?? null,
      content_type: content.content_type ?? null,
      create_time: message.create_time ?? null,
      message_id: message.id ?? null,
      content_parts: content.parts ?? null,
      metadata: message.metadata ?? {}
    }, `node ${nodeId}`);
  }

  function envelope(data) {
    const value = {};
    for (const [key, item] of Object.entries(data)) {
      if (key === "mapping" || key === "current_node") continue;
      value[key] = item;
    }
    const cloned = cloneJson(value, "conversation envelope");
    const hits = credentialSurfacePaths(cloned);
    if (hits.length) throw new C2RecoveryError(`credential/header-shaped surface in envelope: ${hits.slice(0, 5).join(", ")}`);
    return cloned;
  }

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

  async function sha256Hex(value) {
    const bytes = new TextEncoder().encode(canonicalString(value));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function downloadJson(filename, value) {
    const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
  }

  function safeFilenamePart(value) {
    return String(value || "unknown").replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 80);
  }

  async function buildArtifact() {
    const queryClient = discoverQueryClient();
    const query = findExactConversationQuery(queryClient);
    const data = query.state.data;
    const path = reconstructActivePath(data.mapping, data.current_node);
    const orderedNodes = path.map(([nodeId, node], index) => nodeRecord(index, nodeId, node));
    const conversationId = conversationIdFromLocation() || data.id || data.conversation_id || null;
    const artifact = {
      schema_version: SCHEMA_VERSION,
      adapter_version: VERSION,
      source: "chatgpt-web-queryclient-memory",
      conversation_id: conversationId,
      exported_at: new Date().toISOString(),
      mapping_size: Object.keys(data.mapping).length,
      active_path_size: orderedNodes.length,
      current_node: data.current_node,
      conversation_envelope: envelope(data),
      ordered_nodes: orderedNodes
    };
    const hits = credentialSurfacePaths(artifact);
    if (hits.length) throw new C2RecoveryError(`credential/header-shaped surface detected: ${hits.slice(0, 5).join(", ")}`);
    artifact.sha256 = await sha256Hex(artifact);
    return artifact;
  }

  async function run(options = {}) {
    const resourceCountBefore = performance.getEntriesByType("resource").length;
    const artifact = await buildArtifact();
    const resourceCountAfter = performance.getEntriesByType("resource").length;
    const receipt = {
      status: "PASS",
      adapter_version: VERSION,
      mapping_size: artifact.mapping_size,
      active_path_size: artifact.active_path_size,
      content_payload_nodes: artifact.ordered_nodes.filter((node) => Array.isArray(node.content_parts) && node.content_parts.length > 0).length,
      sha256: artifact.sha256,
      credential_surface_count: 0,
      queryclient_mutation_calls: 0,
      network_requests_required: 0,
      resource_entries_delta: resourceCountAfter - resourceCountBefore
    };
    if (options.download !== false) {
      const stamp = artifact.exported_at.replace(/[:.]/g, "-");
      downloadJson(`chatgpt-c2-recovery-${safeFilenamePart(artifact.conversation_id)}-${stamp}.json`, artifact);
    }
    console.info("CM C2 recovery receipt (no bodies):", receipt);
    return { artifact, receipt };
  }

  global.CM_C2_RECOVERY = Object.freeze({
    version: VERSION,
    run,
    buildArtifact,
    discoverQueryClient,
    findExactConversationQuery,
    reconstructActivePath,
    credentialSurfacePaths
  });
})(typeof window !== "undefined" ? window : globalThis);
