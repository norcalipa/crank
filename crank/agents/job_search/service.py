# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Provider-independent job-search conversation orchestration.

This is the application-service entry point that a chat view (out of scope for
this issue) delegates to. It wires the versioned prompt, bounded tools,
deterministic context builder, schema-validated model output, and the injected
preference service (issue #306) into a single safe turn.

The service never talks to a provider directly; it always goes through a
:class:`~crank.agents.job_search.gateway.ProviderGateway`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from crank.agents.job_search import context as ctx
from crank.agents.job_search import quality
from crank.agents.job_search import system_prompt as prompt
from crank.agents.job_search import tools
from crank.agents.job_search.errors import (
    CostLimitError,
    EchoReplyError,
    InvalidJobListingReferenceError,
    InvalidOrganizationReferenceError,
    InvalidPreferencePatchError,
    JobSearchError,
    ProviderError,
    ProviderTimeoutError,
)
from crank.agents.job_search.gateway import GatewayResponse, ModelRequest, ProviderGateway
from crank.agents.job_search.types import (
    AssistantCompletion,
    JobResult,
    OrganizationResult,
    StructuredResults,
)
from crank.services import monitoring

logger = logging.getLogger("crank.agents.job_search")

OrganizationDatasource = Callable[[dict[str, Any], int], list[Any]]
ScoreDatasource = Callable[[list[int], list[str] | None, int], list[Any]]
JobListingDatasource = Callable[[dict[str, Any], int], list[Any]]
JobListingDetailDatasource = Callable[[int], Any | None]


class PreferenceService(Protocol):
    """Port for the preference lifecycle service (issue #306).

    ``validate_patch`` raises :class:`InvalidPreferencePatchError` for a
    malformed or schema-violating patch; ``apply_patch`` applies an accepted
    patch transactionally and returns whether preferences changed.
    """

    def validate_patch(self, patch: dict[str, Any]) -> None:
        ...  # pragma: no cover - structural Protocol stub, never called directly

    def apply_patch(self, patch: dict[str, Any]) -> bool:
        ...  # pragma: no cover - structural Protocol stub, never called directly


@dataclass(frozen=True)
class OrchestratorResult:
    """The validated, safe result of a single orchestrated turn."""

    message: str
    cited_organization_ids: tuple = ()
    cited_job_listing_ids: tuple = ()
    preference_patch: dict[str, Any] | None = None
    preferences_changed: bool = False
    prompt_id: str = prompt.prompt_id()
    results: Optional[StructuredResults] = None
    # Bounded operator telemetry (issue #397). Counts/names only, never content.
    tools_used: tuple = ()
    result_counts: tuple = ()
    cited_ids_count: int = 0
    empty_result: bool = True
    inventory_nonempty: bool = False


class JobSearchOrchestrator:
    """Bounded job-search conversation orchestration.

    Parameters
    ----------
    gateway:
        Provider-independent completion gateway (issue #304).
    preference_service:
        Preference lifecycle service implementing :class:`PreferenceService`.
    org_datasource:
        Bounded organization query; defaults to the active/public ORM query.
    score_datasource:
        Bounded score-summary query; defaults to the ORM average-query.
    """

    def __init__(
        self,
        *,
        gateway: ProviderGateway,
        preference_service: PreferenceService,
        user: Any = None,
        org_datasource: OrganizationDatasource | None = None,
        score_datasource: ScoreDatasource | None = None,
        job_listing_datasource: JobListingDatasource | None = None,
        job_listing_detail_datasource: JobListingDetailDatasource | None = None,
        match_service: callable | None = None,
        max_organization_results: int = tools.MAX_ORGANIZATION_RESULTS,
        max_score_summary_results: int = tools.MAX_SCORE_SUMMARY_RESULTS,
        max_job_listing_results: int = tools.MAX_JOB_LISTING_RESULTS,
        max_match_results: int = tools.MAX_MATCH_RESULTS,
        max_preference_characters: int = 2000,
        max_conversation_characters: int = 8000,
        max_conversation_messages: int | None = None,
        system_prompt_version: int = prompt.SYSTEM_PROMPT_VERSION,
    ) -> None:
        self._gateway = gateway
        self._preference_service = preference_service
        self._user = user
        self._org_datasource = org_datasource or tools.default_organization_datasource
        self._score_datasource = score_datasource or tools.default_score_summary_datasource
        self._job_listing_datasource = job_listing_datasource or tools.default_job_listing_datasource
        self._job_listing_detail_datasource = (
            job_listing_detail_datasource or tools.default_job_listing_detail_datasource
        )
        self._match_service = match_service
        self._max_organization_results = max_organization_results
        self._max_score_summary_results = max_score_summary_results
        self._max_job_listing_results = max_job_listing_results
        self._max_match_results = max_match_results
        self._max_preference_characters = max_preference_characters
        self._max_conversation_characters = max_conversation_characters
        self._max_conversation_messages = max_conversation_messages
        self._system_prompt_version = system_prompt_version

    def run(
        self,
        *,
        user_prompt: str,
        conversation: list[dict[str, str]],
        preference_markdown: str,
        token_budget: int | None = None,
        max_tokens: int | None = None,
    ) -> OrchestratorResult:
        """Run one turn and emit only bounded interactive-call telemetry."""
        started = time.monotonic()
        try:
            result = self._run(
                user_prompt=user_prompt,
                conversation=conversation,
                preference_markdown=preference_markdown,
                token_budget=token_budget,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            monitoring.record_event(
                "interactive_call",
                {
                    "status": "failed",
                    "reason_code": monitoring.failure_reason(exc),
                    "provider_error_class": type(exc).__name__,
                    "latency_ms": latency_ms,
                    "latency_bucket": monitoring.latency_bucket(latency_ms),
                },
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        monitoring.record_event(
            "interactive_call",
            {
                "status": "succeeded",
                "latency_ms": latency_ms,
                "latency_bucket": monitoring.latency_bucket(latency_ms),
            },
        )
        # Per-turn helpfulness telemetry: how many tools fired, how many rows
        # and citations resulted, whether the turn produced any result card.
        monitoring.record_event(
            "job_search_turn",
            {
                "tools_called": len(result.tools_used),
                "result_count": sum(result.result_counts),
                "cited_ids_count": result.cited_ids_count,
                "empty_result": result.empty_result,
                "inventory_nonempty": result.inventory_nonempty,
                "latency_ms": latency_ms,
                "latency_bucket": monitoring.latency_bucket(latency_ms),
            },
        )
        return result

    def _run(
        self,
        *,
        user_prompt: str,
        conversation: list[dict[str, str]],
        preference_markdown: str,
        token_budget: int | None = None,
        max_tokens: int | None = None,
    ) -> OrchestratorResult:
        """Execute one orchestrated turn and return its validated result.

        Raises typed ``JobSearchError`` subclasses on provider timeout/failure,
        cost-limit, malformed output, hallucinated organization or listing IDs,
        and invalid preference patches. Preference patches are applied only
        after every validation gate passes.
        """
        # 1. Bounded, server-controlled dataset (active/public only).
        org_rows = self._load_organization_catalog()
        known_ids = tools.union_server_controlled_ids(org_rows)
        score_rows: list[dict[str, Any]] = []
        if known_ids:
            score_rows = self._load_score_summaries(known_ids)

        # 1b. Bounded, server-controlled job listings (active/open only).
        listing_rows = self._load_job_listings()
        known_listing_ids = tools.union_server_controlled_listing_ids(listing_rows)

        # 1c. Preference-grounded matches (issue #395). Only invoked when a
        # match service and a user are actually wired up.
        match_data = self._load_matches()
        match_enabled = self._match_service is not None and self._user is not None

        # Telemetry: count only the datasources actually invoked this turn,
        # and surface the same set in ``tools_used``/``result_counts`` so the
        # two can never drift (issue #423). Counts/sizes only, no payloads.
        tools_used: list[str] = []
        result_counts: list[int] = []

        tools_used.append("query_active_organizations")
        result_counts.append(len(org_rows))
        monitoring.record_event(
            "job_search_tool_invocation",
            {"tool": "query_active_organizations", "result_count": len(org_rows)},
        )

        if known_ids:
            tools_used.append("query_score_summaries")
            result_counts.append(len(score_rows))
            monitoring.record_event(
                "job_search_tool_invocation",
                {"tool": "query_score_summaries", "result_count": len(score_rows)},
            )

        tools_used.append("search_job_listings")
        result_counts.append(len(listing_rows))
        monitoring.record_event(
            "job_search_tool_invocation",
            {"tool": "search_job_listings", "result_count": len(listing_rows)},
        )

        if match_enabled:
            tools_used.append("get_matches_for_user")
            result_counts.append(
                len(match_data.get("job_matches", []))
                + len(match_data.get("organization_matches", []))
            )
            monitoring.record_event(
                "job_search_tool_invocation",
                {
                    "tool": "get_matches_for_user",
                    "job_match_count": len(match_data.get("job_matches", [])),
                    "organization_match_count": len(match_data.get("organization_matches", [])),
                },
            )

        # 2. Deterministic, truncated model context.
        model_context = self._build_model_context(
            user_prompt=user_prompt,
            conversation=conversation,
            preference_markdown=preference_markdown,
            organization_catalog=org_rows,
            score_summaries=score_rows,
            job_listings=listing_rows,
            matches=match_data,
        )

        # 3. Provider call (maps provider failures to typed errors).
        response = self._invoke_gateway(model_context, token_budget, max_tokens)

        # 4. Schema-validate the raw output.
        completion = AssistantCompletion.from_json(response.text)

        # 5. Reject any cited IDs the server did not expose.
        self._validate_citations(
            completion.cited_organization_ids, frozenset(known_ids)
        )
        self._validate_listing_citations(
            completion.cited_job_listing_ids, frozenset(known_listing_ids)
        )

        # Anti-echo guard: never serve a reply that merely restates the user's
        # message when server data was available and no tool-grounded work was
        # done (guards against a demo/echo provider leaking into production).
        inventory_nonempty = bool(known_ids or known_listing_ids)
        self._guard_echo(
            user_prompt=user_prompt,
            completion=completion,
            inventory_nonempty=inventory_nonempty,
        )

        # 6. Validate + apply the optional preference patch.
        preferences_changed = False
        applied_patch: dict[str, Any] | None = None
        if completion.has_preference_patch:
            applied_patch, preferences_changed = self._apply_preference_patch(completion.preference_patch)

        # 7. Build citation-validated structured results from server data.
        structured_results = self._build_results(
            completion.cited_organization_ids,
            org_rows,
            completion.cited_job_listing_ids,
            listing_rows,
        )

        tools_used = tuple(tools_used)
        result_counts = tuple(result_counts)
        cited_ids_count = len(completion.cited_organization_ids) + len(
            completion.cited_job_listing_ids
        )

        logger.info(
            "job_search_complete prompt_id=%s cited_orgs=%s cited_listings=%s "
            "preferences_changed=%s",
            model_context.prompt_id,
            completion.cited_organization_ids,
            completion.cited_job_listing_ids,
            preferences_changed,
        )
        return OrchestratorResult(
            message=completion.message,
            cited_organization_ids=completion.cited_organization_ids,
            cited_job_listing_ids=completion.cited_job_listing_ids,
            preference_patch=applied_patch,
            preferences_changed=preferences_changed,
            results=structured_results,
            tools_used=tools_used,
            result_counts=result_counts,
            cited_ids_count=cited_ids_count,
            empty_result=cited_ids_count == 0,
            inventory_nonempty=inventory_nonempty,
        )

    # -- internals ------------------------------------------------------------

    def _load_organization_catalog(self) -> list[dict[str, Any]]:
        capped = tools.clamp_result_limit(
            self._max_organization_results, maximum=tools.MAX_ORGANIZATION_RESULTS
        )
        filters = tools.validate_organization_filters({})
        rows = self._org_datasource(filters, capped)
        return tools.normalize_organization_rows(rows)

    def _load_score_summaries(self, known_ids: list[int]) -> list[dict[str, Any]]:
        capped = tools.clamp_result_limit(
            self._max_score_summary_results, maximum=tools.MAX_SCORE_SUMMARY_RESULTS
        )
        rows = self._score_datasource(known_ids, None, capped)
        # Normalize in the service layer so a mis-shapen/injected datasource
        # surfaces as InvalidScoreSummaryRowError instead of a bare KeyError
        # when the context renderer formats the rows.
        return tools.normalize_score_summary_rows(rows)

    def _load_job_listings(self) -> list[dict[str, Any]]:
        capped = tools.clamp_result_limit(
            self._max_job_listing_results, maximum=tools.MAX_JOB_LISTING_RESULTS
        )
        filters = tools.validate_job_listing_filters({})
        rows = self._job_listing_datasource(filters, capped)
        return tools.normalize_job_listing_rows(rows)

    def _load_matches(self) -> dict[str, Any]:
        """Load preference-grounded matches for the current user."""
        if self._match_service is None or self._user is None:
            return {"job_matches": [], "organization_matches": []}
        capped = tools.clamp_result_limit(
            self._max_match_results, maximum=tools.MAX_MATCH_RESULTS
        )
        return self._match_service(user=self._user, limit=capped)

    def _build_model_context(self, **kwargs: Any) -> ctx.ModelContext:
        version = self._system_prompt_version
        system = prompt.build_system_prompt(
            version=version,
            max_organizations=self._max_organization_results,
            max_score_rows=self._max_score_summary_results,
            max_job_listings=self._max_job_listing_results,
            max_match_results=self._max_match_results,
        )
        return ctx.build_model_context(
            prompt_id=prompt.prompt_id(version),
            system=system,
            conversation=kwargs["conversation"],
            user_prompt=kwargs["user_prompt"],
            preference_markdown=kwargs["preference_markdown"],
            organization_catalog=kwargs["organization_catalog"],
            score_summaries=kwargs["score_summaries"],
            max_preference_characters=self._max_preference_characters,
            max_conversation_characters=self._max_conversation_characters,
            max_conversation_messages=self._max_conversation_messages,
            job_listings=kwargs.get("job_listings", []),
            max_job_listing_rows=self._max_job_listing_results,
            matches=kwargs.get("matches"),
        )

    def _invoke_gateway(
        self,
        model_context: ctx.ModelContext,
        token_budget: int | None,
        max_tokens: int | None,
    ) -> GatewayResponse:
        request = ModelRequest(
            prompt_id=model_context.prompt_id,
            system=model_context.system,
            messages=model_context.to_messages(),
            max_tokens=max_tokens,
            token_budget=token_budget,
        )
        try:
            response = self._gateway.complete(request)
            usage = response.usage or {}
            monitoring.record_event(
                "interactive_call",
                {
                    "status": "provider_succeeded",
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
                    "total_tokens": usage.get("total_tokens", 0),
                    "estimated_cost_usd": usage.get("estimated_cost_usd", 0),
                },
            )
            return response
        except (ProviderTimeoutError, CostLimitError, ProviderError) as exc:
            # Already typed; propagate so callers can distinguish outcomes.
            monitoring.record_event(
                "interactive_call",
                {"status": "provider_failed", "reason_code": monitoring.failure_reason(exc)},
            )
            raise
        except TimeoutError as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except JobSearchError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            # Provider exceptions may contain request bodies, credentials, or
            # response text. Keep both the log and the public error generic;
            # callers can still classify the typed boundary error.
            logger.error(
                "job_search provider failure error_type=%s",
                type(exc).__name__,
            )
            raise ProviderError("provider failed to produce a response") from exc

    @staticmethod
    def _validate_citations(
        cited_ids: tuple, known_ids: frozenset
    ) -> None:
        unknown = [i for i in cited_ids if i not in known_ids]
        if unknown:
            raise InvalidOrganizationReferenceError(
                f"model cited organization IDs not exposed by server tools: {', '.join(str(i) for i in unknown)}"
            )

    @staticmethod
    def _validate_listing_citations(
        cited_ids: tuple, known_ids: frozenset
    ) -> None:
        unknown = [i for i in cited_ids if i not in known_ids]
        if unknown:
            raise InvalidJobListingReferenceError(
                f"model cited job listing IDs not exposed by server tools: {', '.join(str(i) for i in unknown)}"
            )

    @staticmethod
    def _guard_echo(
        *, user_prompt: str, completion: AssistantCompletion, inventory_nonempty: bool
    ) -> None:
        """Reject an unrooted echo of the user turn when inventory is non-empty.

        The classic demo-provider regression is a reply that restates the
        user's message and produces no citation, no result card, and no
        preference patch even though server data was available for grounding.
        Fail the turn closed (raise :class:`EchoReplyError`) so the defective
        reply surfaces instead of silently degrading the chat.
        """
        if not inventory_nonempty:
            return
        if completion.cited_organization_ids or completion.cited_job_listing_ids:
            return
        if completion.has_preference_patch:
            return  # preference elicitation / patch turns are legitimate.
        if quality.is_echo(user_prompt, completion.message):
            raise EchoReplyError(
                "assistant reply only restates the user message without any tool-grounded result"
            )

    @staticmethod
    def _build_results(
        cited_org_ids: tuple,
        org_rows: List[Dict[str, Any]],
        cited_listing_ids: tuple,
        listing_rows: List[Dict[str, Any]],
    ) -> Optional[StructuredResults]:
        """Build structured results from cited IDs and server-controlled rows.

        Only rows whose IDs appear in the corresponding cited-id lists are
        included.  This ensures the model cannot inject URLs or data the
        server did not expose.
        """
        cited_org_set = set(cited_org_ids)
        cited_listing_set = set(cited_listing_ids)
        if not cited_org_set and not cited_listing_set:
            return None

        org_results: List[OrganizationResult] = []
        for row in org_rows:
            row_id = row.get("id")
            if row_id in cited_org_set:
                org_results.append(
                    OrganizationResult(
                        id=int(row["id"]),
                        name=str(row.get("name", "")),
                        url=str(row.get("url", "")),
                        funding_round=str(row.get("funding_round", "")),
                        rto_policy=str(row.get("rto_policy", "")),
                    )
                )

        job_results: List[JobResult] = []
        for row in listing_rows:
            row_id = row.get("id")
            if row_id in cited_listing_set:
                job_results.append(
                    JobResult(
                        id=int(row["id"]),
                        title=str(row.get("title", "")),
                        organization_name=str(row.get("organization_name", "")),
                        location=str(row.get("location", "")),
                        remote=bool(row.get("remote", False)),
                        compensation=row.get("compensation"),
                        canonical_url=str(row.get("canonical_url", "")),
                        observed_at=row.get("observed_at"),
                        updated_at=row.get("updated_at"),
                    )
                )

        return StructuredResults(
            jobs=tuple(job_results),
            organizations=tuple(org_results),
        )

    def _apply_preference_patch(
        self, patch: dict[str, Any]
    ) -> tuple:
        try:
            self._preference_service.validate_patch(patch)
        except InvalidPreferencePatchError:
            raise
        except Exception as exc:  # defensive: port must raise typed error
            raise InvalidPreferencePatchError(str(exc)) from exc
        changed = self._preference_service.apply_patch(patch)
        return patch, bool(changed)
