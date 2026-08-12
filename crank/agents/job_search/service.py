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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from crank.agents.job_search import context as ctx
from crank.agents.job_search import system_prompt as prompt
from crank.agents.job_search import tools
from crank.agents.job_search.errors import (
    InvalidModelOutputError,
    InvalidOrganizationReferenceError,
    InvalidPreferencePatchError,
    JobSearchError,
    ProviderError,
    ProviderTimeoutError,
    CostLimitError,
)
from crank.agents.job_search.gateway import GatewayResponse, ModelRequest, ProviderGateway
from crank.agents.job_search.types import AssistantCompletion

logger = logging.getLogger("crank.agents.job_search")

OrganizationDatasource = Callable[[Dict[str, Any], int], List[Any]]
ScoreDatasource = Callable[[List[int], Optional[List[str]], int], List[Any]]


class PreferenceService(Protocol):
    """Port for the preference lifecycle service (issue #306).

    ``validate_patch`` raises :class:`InvalidPreferencePatchError` for a
    malformed or schema-violating patch; ``apply_patch`` applies an accepted
    patch transactionally and returns whether preferences changed.
    """

    def validate_patch(self, patch: Dict[str, Any]) -> None:
        ...  # pragma: no cover - structural Protocol stub, never called directly

    def apply_patch(self, patch: Dict[str, Any]) -> bool:
        ...  # pragma: no cover - structural Protocol stub, never called directly


@dataclass(frozen=True)
class OrchestratorResult:
    """The validated, safe result of a single orchestrated turn."""

    message: str
    cited_organization_ids: tuple = ()
    preference_patch: Optional[Dict[str, Any]] = None
    preferences_changed: bool = False
    prompt_id: str = prompt.prompt_id()


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
        org_datasource: Optional[OrganizationDatasource] = None,
        score_datasource: Optional[ScoreDatasource] = None,
        max_organization_results: int = tools.MAX_ORGANIZATION_RESULTS,
        max_score_summary_results: int = tools.MAX_SCORE_SUMMARY_RESULTS,
        max_preference_characters: int = 2000,
        max_conversation_characters: int = 8000,
        max_conversation_messages: Optional[int] = None,
        system_prompt_version: int = prompt.SYSTEM_PROMPT_VERSION,
    ) -> None:
        self._gateway = gateway
        self._preference_service = preference_service
        self._org_datasource = org_datasource or tools.default_organization_datasource
        self._score_datasource = score_datasource or tools.default_score_summary_datasource
        self._max_organization_results = max_organization_results
        self._max_score_summary_results = max_score_summary_results
        self._max_preference_characters = max_preference_characters
        self._max_conversation_characters = max_conversation_characters
        self._max_conversation_messages = max_conversation_messages
        self._system_prompt_version = system_prompt_version

    def run(
        self,
        *,
        user_prompt: str,
        conversation: List[Dict[str, str]],
        preference_markdown: str,
        token_budget: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> OrchestratorResult:
        """Execute one orchestrated turn and return its validated result.

        Raises typed ``JobSearchError`` subclasses on provider timeout/failure,
        cost-limit, malformed output, hallucinated organization IDs, and invalid
        preference patches. Preference patches are applied only after every
        validation gate passes.
        """
        # 1. Bounded, server-controlled dataset (active/public only).
        org_rows = self._load_organization_catalog()
        known_ids = tools.union_server_controlled_ids(org_rows)
        score_rows: List[Dict[str, Any]] = []
        if known_ids:
            score_rows = self._load_score_summaries(known_ids)

        # 2. Deterministic, truncated model context.
        model_context = self._build_model_context(
            user_prompt=user_prompt,
            conversation=conversation,
            preference_markdown=preference_markdown,
            organization_catalog=org_rows,
            score_summaries=score_rows,
        )

        # 3. Provider call (maps provider failures to typed errors).
        response = self._invoke_gateway(model_context, token_budget, max_tokens)

        # 4. Schema-validate the raw output.
        completion = AssistantCompletion.from_json(response.text)

        # 5. Reject any cited ID the server did not expose.
        self._validate_citations(completion.cited_organization_ids, frozenset(known_ids))

        # 6. Validate + apply the optional preference patch.
        preferences_changed = False
        applied_patch: Optional[Dict[str, Any]] = None
        if completion.has_preference_patch:
            applied_patch, preferences_changed = self._apply_preference_patch(completion.preference_patch)

        logger.info(
            "job_search_complete prompt_id=%s cited=%s preferences_changed=%s",
            model_context.prompt_id,
            completion.cited_organization_ids,
            preferences_changed,
        )
        return OrchestratorResult(
            message=completion.message,
            cited_organization_ids=completion.cited_organization_ids,
            preference_patch=applied_patch,
            preferences_changed=preferences_changed,
        )

    # -- internals ------------------------------------------------------------

    def _load_organization_catalog(self) -> List[Dict[str, Any]]:
        capped = tools.clamp_result_limit(
            self._max_organization_results, maximum=tools.MAX_ORGANIZATION_RESULTS
        )
        filters = tools.validate_organization_filters({})
        rows = self._org_datasource(filters, capped)
        return tools.normalize_organization_rows(rows)

    def _load_score_summaries(self, known_ids: List[int]) -> List[Dict[str, Any]]:
        capped = tools.clamp_result_limit(
            self._max_score_summary_results, maximum=tools.MAX_SCORE_SUMMARY_RESULTS
        )
        rows = self._score_datasource(known_ids, None, capped)
        # Normalize in the service layer so a mis-shapen/injected datasource
        # surfaces as InvalidScoreSummaryRowError instead of a bare KeyError
        # when the context renderer formats the rows.
        return tools.normalize_score_summary_rows(rows)

    def _build_model_context(self, **kwargs: Any) -> ctx.ModelContext:
        version = self._system_prompt_version
        system = prompt.build_system_prompt(
            version=version,
            max_organizations=self._max_organization_results,
            max_score_rows=self._max_score_summary_results,
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
        )

    def _invoke_gateway(
        self,
        model_context: ctx.ModelContext,
        token_budget: Optional[int],
        max_tokens: Optional[int],
    ) -> GatewayResponse:
        request = ModelRequest(
            prompt_id=model_context.prompt_id,
            system=model_context.system,
            messages=model_context.to_messages(),
            max_tokens=max_tokens,
            token_budget=token_budget,
        )
        try:
            return self._gateway.complete(request)
        except (ProviderTimeoutError, CostLimitError, ProviderError) as exc:
            # Already typed; propagate so callers can distinguish outcomes.
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
                "model cited organization IDs not exposed by server tools: %s"
                % ", ".join(str(i) for i in unknown)
            )

    def _apply_preference_patch(
        self, patch: Dict[str, Any]
    ) -> tuple:
        try:
            self._preference_service.validate_patch(patch)
        except InvalidPreferencePatchError:
            raise
        except Exception as exc:  # defensive: port must raise typed error
            raise InvalidPreferencePatchError(str(exc)) from exc
        changed = self._preference_service.apply_patch(patch)
        return patch, bool(changed)
