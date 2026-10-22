# Model output deployment — 2026-10-21

Status: deployed and verified. No real OR or JEV model calls were made during deployment.

## Active releases

- estomago: `/Users/owner/Projects/estomago-releases/model-whitelist-20261021`.
  Built from the previous production release, applying only approved commits `05e05c5`, `af4fb6f`, `bd677ed`. The unrelated uncommitted background-task changes in the working tree were excluded.
- Mariposa: `/Users/owner/Projects/mariposa-releases/model-whitelist-20261021`, exported from `904b1a3` (includes MCP fix `7dca70b` and cache compatibility `4541656`). No drafts were included.
- Both launchd jobs report running and their working directories match these pinned releases. Both local health endpoints returned HTTP 200. Authenticated estomago read and same-bucket detail route passed.

## Production verification

- Default MCP success response contains one complete JSON text block, no structuredContent duplicate. Explicit `content_envelope=single` remains compatible.
- Verification used the existing credential's allowed, read-only `memory.mood.vocab` capability. Its lifecycle allowlist does not permit `time.now` or `memory.get`; rejected probes were not worked around by expanding permissions.
- A real stored memory was checked through the pinned pure reading projection against the read-only production database: event body byte content and occurrence date preserved. This was not claimed as a successful live memory.get call.
- Chat messages/revisions, windows/trajectories, provider request records and runtime settings had identical before/after table fingerprints.
- Formal memories, versions, quoted words, recollections and relations had identical fingerprints. JEV feature/rerank caches also had identical fingerprints.
- Provider request count delta: 0. No synthetic chat turn or paid model call was created.
- Existing configuration bytes were unchanged. Surfacing remains enabled with its existing 15000ms deadline. The existing provider eligibility gate remains unchanged; this deployment does not claim OR surfacing is newly supported.
- Only additive estomago migration 32 was applied (window_toolsets). On a private backup copy, restoring the active window preserved all 38 previously sent tool definitions and the trajectory. New windows advertise the three deferred-tool entry points. No forced rotation or old-prefix rewrite occurred.

## Startup incident and recovery

Launchd processes stalled when started under Documents. Node sampling captured node::Cwd → uv_cwd → __getcwd → __open_nocancel before application startup. The exact OS-level cause was not asserted. Moving the same code snapshots under Projects restored startup and health. Final deployment and verification used Projects for both current and rollback releases. No database was restored or overwritten during the retries.

The first unbounded database backup stalled. It was stopped, then replaced with a bounded SQLite backup using an explicit read snapshot. Final backups completed successfully before cutover.

## Rollback

Private backups: `/Users/owner/Documents/Codex/2026-10-21/jev/work/deploy-backups/model-whitelist-20261021` (directory 0700, files 0600).

The saved `estomago-start-rollback.sh` points to the previous production release copied under Projects. `mariposa-start-rollback.sh` points to the last previously verified production code `af2dbbe`, also under Projects. To roll back, atomically replace each service's start.sh with its corresponding rollback wrapper, then restart its launchd job and verify health. Restore code only; do not overwrite live databases with old backups. The additive toolset table can remain unused by old code.

## Evidence

Full deployment/fingerprint record: `/Users/owner/Documents/Codex/2026-10-21/jev/outputs/model-production-deployment.json`.
Cache-copy dry run: `/Users/owner/Documents/Codex/2026-10-21/jev/outputs/model-deploy-cache-dryrun.json`.
Production credentials and memory/chat bodies are not included in these reports or Git commits.
