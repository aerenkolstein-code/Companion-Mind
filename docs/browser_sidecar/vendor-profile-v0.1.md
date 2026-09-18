# ChatGPT-Web-v0.1 synthetic VendorProfile

The JSON in `companion_mind/browser_sidecar/profiles/chatgpt_web_v0_1.json`
is the single supported fixture contract. A fingerprint is SHA256 of sorted,
compact, UTF-8 JSON with `profile_fingerprint` omitted. The loader compares the
declared digest, recomputed digest and the frozen digest in `vendor_profile.py`.
Recomputing an edited profile's digest does not authorize that change/version.

| Surface | Frozen synthetic rule |
|---|---|
| Root | Exactly one visible `data-c1-conversation="synthetic-chatgpt"` |
| Scope | `data-c1-scope` opaque local alias; never account email |
| Conversation | Exactly one of `data-c1-id` and `data-c1-temp-id` |
| Message | `article` with `data-c1-message`; stable safe ID required |
| Role | `data-c1-role` matches exactly one visible `data-c1-role-label` |
| Order | Nonnegative integer `data-c1-order`; ambiguous positions rejected |
| Payload | Exactly one visible `data-c1-content`; paragraph/code/list normalization |
| Stream | `data-c1-state="streaming"` + `data-c1-streaming="true"`; no terminal |
| Terminal | State matches `data-c1-terminal`; fixture stability ≥500 ms |
| Attachment | `data-c1-attachment`, safe visible name/type, message association; no URL retained |
| Modes | desktop-light-en, desktop-dark-zh, desktop-compact-en |
| Errors | PROFILE_DRIFT, AMBIGUOUS_ROLE/IDENTITY/TERMINAL, CONFLICT; no partial output |

The profile describes one future vendor family, ChatGPT Official Web on desktop
Chromium-family, in single-user, user-controlled conversations. The selector
spelling is **synthetic** and production validity is **NOT_VERIFIED**. Grok,
Gemini, mobile, hidden endpoints, unknown layouts, real capture and universal
browser support are excluded. Browser execution/permissions are not part of S0.

The JSON is loaded from a source checkout/editable installation, matching this
repository's current CI. A standalone browser extension/wheel distribution is
not shipped or proven; later packaging must include its frozen assets without
silently changing this profile or pyproject.toml under the S0 authorization.
