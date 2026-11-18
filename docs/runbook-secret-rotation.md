<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operator Runbook: Secret Rotation

**Owner:** CRank maintainers
**Last reviewed:** 2026-08-12
**Version/change process:** Changes follow the [architecture documentation](./readme.md) and must pass CI gates.

## Purpose

Rotate LLM API keys and source credentials without downtime. All secrets are environment-backed; none are checked into source control.

## LLM API Key Rotation

1. Replace the key in `crank-capability-secrets` and restart the web
   Deployment by following "Populate or rotate a credential" in
   [capability-config-contract.md](./capability-config-contract.md). That
   procedure changes one key without dropping the others.
2. Wait for the rollout (the procedure ends with `rollout status`).
3. Verify the agent still responds:
   ```sh
   kubectl exec deployment/crank -- python manage.py agent_noop
   ```
4. Revoke the old key in the provider console.

## Source Credential Rotation

Source credentials (e.g., `SCORE_RESOLUTION_SOURCE_KEY`) follow the same pattern:

1. Update the key in `crank-capability-secrets` with the same procedure.
2. Restart the deployment (part of that procedure).
3. Verify source ingestion with a manual run:
   ```sh
   kubectl exec deployment/crank -- python manage.py gather_scores --source <source-name>
   ```
4. Confirm the run succeeded in Django admin (`AgentRun` records).

## Verification

After rotation, verify that:
- `GET /healthz/ready/` returns 200.
- A no-op agent run completes successfully.
- No errors appear in New Relic logs related to authentication.
