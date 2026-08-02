- [x] Improve README.md
- [x] Review and improve code quality
- [x] Enhance error handling
- [x] Add unit tests
- [x] Implement a proper logging mechanism
- [ ] Improve fact management and retrieval
- [ ] Explore alternative embedding models
- [ ] Containerize the application (Docker)
- [ ] Create a comprehensive analysis report
- [ ] Deliver findings to user

## Gemini Spark integration

### Stage 0 — Feasibility gate

- [ ] Confirm Spark's currently supported integration surface for Google Workspace (documented APIs, add-ons, Apps Script, webhooks, or another supported mechanism).
- [ ] Record authentication model, required OAuth scopes, event/trigger support, quotas, deployment constraints, and whether a stable programmatic handoff to Aion is possible.
- [ ] Stop here if the integration depends on undocumented UI automation, excessive scopes, or cannot enforce the privacy boundary below.

### Stage 1 — Responsibility and privacy boundary

- [ ] Use Spark only for user-approved Google Workspace workflows involving Gmail, Calendar, Drive, Docs, and Sheets.
- [ ] Keep Aion responsible for local-model reasoning, private PostgreSQL/ChatGPT memory, local files, and local tools.
- [ ] Define a narrow, allowlisted request/response schema between them. Never send raw Aion memory, archive conversations, credentials, or unrestricted local-file content to Spark.
- [ ] Require explicit user selection or approval before Workspace content crosses the boundary; minimize fields and redact logs.

### Stage 2 — Narrow prototype

- [ ] Implement one read-only, manually triggered workflow: retrieve a limited calendar agenda through Spark and pass only the approved event fields to Aion for local presentation.
- [ ] Use least-privilege OAuth, bounded result counts, timeouts, and clear unavailable/denied states.
- [ ] Do not add Workspace write actions during the prototype.

### Stage 3 — Validation

- [ ] Verify correct account and scope handling, token revocation, schema validation, privacy/log redaction, latency, retries, quota handling, and graceful degradation.
- [ ] Confirm Aion remains fully usable when Spark is disabled or unreachable and that no private-memory data appears in outbound requests.
- [ ] Add contract tests with sanitized fixtures plus an explicit end-to-end approval test.

### Stage 4 — Rollout and rollback

- [ ] Ship behind a default-off feature flag to one account and one allowlisted workflow.
- [ ] Promote read-only workflows first; require explicit confirmation and idempotency before any later write workflow.
- [ ] Roll out only if privacy checks pass, error/latency targets are acceptable, and audit logs contain no sensitive payloads.
- [ ] Roll back by disabling the feature flag and revoking Spark credentials/scopes; Aion's local model and memory path must remain unchanged.

