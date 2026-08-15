# Gen1 online audit and migration ledger

Audit date: 2026-08-15. Repository: `aerenkolstein-code/Companion-Mind`. Audited default branch head: `0e32b9dff61360e6789f0932df5ebee716c7636e`.

The public repository contained four files and three commits. No secret, credential, private Raw/L0, family record, financial record or unpublished manuscript was found in the current tree or commit patches.

| Gen1 item | Decision | Reason |
|---|---|---|
| `README.md` | REWRITE | The two-line description did not explain the current runtime or evidence. |
| `hippocampus.py` | MOVE-TO-HISTORY | The 19-line preview script is a valid ignition artifact, but not the current architecture. It remains recoverable in commit `139a214`. |
| `test.md` | MOVE-TO-HISTORY | It records the first smoke-test input and remains in Git history. |
| `output.json` | DELETE-WITH-REASON | It is generated output rather than a maintained source artifact; it remains in Git history. |
| three-commit history | KEEP | The two root commits and merge are untidy but authentic provenance. History is not rewritten. |

The original branch is additionally preserved as `history/gen1-ignition`. Unknown old code was not overwritten: every item was read and classified before this migration.

