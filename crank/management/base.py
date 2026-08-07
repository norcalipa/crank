# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Base management command for idempotent, scheduled agent runs.

Subclasses set ``run_type`` and implement :meth:`run_payload`. The base class
orchestrates the shared lifecycle:

1. If the command is disabled by its environment setting, exit ``0`` without
   creating any work.
2. Otherwise, claim the run slot via the database-backed overlap guard.
3. If another run of this type is already active, record this invocation as
   ``skipped`` and exit ``0`` (not an error).
4. Run the payload, finalize as ``succeeded``, and emit a New Relic event.
5. On exception, finalize as ``failed`` with a sanitized summary and exit ``1``.
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from crank.services import agent_runs

logger = logging.getLogger("agent_runs")


class AgentRunCommand(BaseCommand):
    """A management command that runs as an idempotent scheduled agent run."""

    #: ``AgentRun.RunType`` value this command represents.
    run_type = None
    #: Setting name (Django settings) that gates whether this command may work.
    #: Disabled by default; enable it per environment.
    enabled_setting = "AGENT_RUN_ENABLED"

    def get_enabled(self):
        """Whether this command may perform work for the current environment.

        The master ``AGENT_RUN_ENABLED`` switch gates everything; the
        per-command setting (e.g. ``AGENT_NOOP_ENABLED``) must also be on.
        """
        if not getattr(settings, "AGENT_RUN_ENABLED", False):
            return False
        return bool(getattr(settings, self.enabled_setting, False))

    def run_payload(self, run, **options):  # pragma: no cover - overridden
        """Execute the command's actual work.

        Should return an outcome-count dictionary recorded on the run.
        """
        raise NotImplementedError

    def handle(self, *args, **options):
        enabled = self.get_enabled()
        self.stdout.write(
            "%s: %s"
            % (
                self.run_type,
                f"enabled ({self.enabled_setting}=True)"
                if enabled
                else "disabled; no work performed",
            )
        )

        if not enabled:
            return 0

        try:
            # A nested atomic block runs the claim as a savepoint so the
            # IntegrityError (constraint hit) rolls back cleanly and leaves the
            # outer transaction usable on MySQL/PostgreSQL.
            with transaction.atomic():
                run = agent_runs.claim_run(self.run_type)
        except IntegrityError:
            # Another invocation won the slot first; record and exit cleanly.
            with transaction.atomic():
                agent_runs.record_skipped(self.run_type)
            self.stdout.write(
                self.style.WARNING(
                    f"{self.run_type}: another run is active; recorded as skipped"
                )
            )
            return 0

        try:
            counts = self.run_payload(run, **options)
            agent_runs.finalize_success(run, counts=counts)
        except Exception as exc:  # noqa: BLE001 - finalize as failed and exit 1
            logger.exception("agent run failed with exception")
            agent_runs.finalize_failure(run, exc)
            # CommandError gives the process a non-zero exit code on failure,
            # which Kubernetes needs to detect/rety the CronJob pod.
            raise CommandError(
                f"{self.run_type}: failed - {agent_runs.sanitize_error(exc)}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{self.run_type}: succeeded (correlation_id={run.correlation_id})"
            )
        )
        return 0