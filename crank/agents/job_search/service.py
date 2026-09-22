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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from crank.agents.job_search import context as ctx
from crank.agents.job_search import quality
from crank.agents.job_search import system_prompt as prompt
from crank.agents.job_search import tools
from crank.agents.job_search.errors import (
    ConversationClosedError,
    CostLimitError,
    EchoReplyError,
    InvalidJobListingReferenceError,
    InvalidOrganizationReferenceError,
    InvalidRequirementReferenceError,
    InvalidPreferencePatchError,
    JobSearchError,
    ProviderError,
    ProviderTimeoutError,
    is_database_locked,
)
from crank.agents.job_search.gateway import GatewayResponse, ModelRequest, ProviderGateway
from crank.agents.job_search.types import (
    AssistantCompletion,
    JobResult,
    OrganizationResult,
    StructuredResults,
)
from crank.services import monitoring

#: Requirement paths the assistant may reference only when the match tool
#: actually exposed them (issue #467 AC-11). Matches the dotted paths and the
#: three bare top-level paths (``industry``, ``funding_stage``, ``culture``)
#: that the earlier regex overlooked.
_REQUIREMENT_PATH_RE = re.compile(
    r"\b(?:compensation|work_location|geography|vesting)\.[a-z_]+\b"
    r"|\b(?:industry|funding_stage|culture)\b"
)
#: A bounded "evidence #<id>" reference form. Accepts the ``id`` wording
#: ("evidence id 999") the earlier regex silently let through unvalidated.
_EVIDENCE_REF_RE = re.compile(r"\bevidence\s*(?:id\s+)?[#:]?\s*(\d+)", re.IGNORECASE)

logger = logging.getLogger("crank.agents.job_search")

OrganizationDatasource = Callable[[dict[str, Any], int], list[Any]]
ScoreDatasource = Callable[[list[int], list[str] | None, int], list[Any]]
JobListingDatasource = Callable[[dict[str, Any], int], list[Any]]
JobListingDetailDatasource = Callable[[int], Any | None]
AvailabilityDatasource = Callable[[Any], dict[str, Any] | None]


def _default_availability_service(user) -> dict[str, Any] | None:
    """Derive the canonical availability state for *user* (issue #476).

    Read-only; staff-only details are never included in model context.
    Returns ``None`` when the user cannot be resolved (e.g. a stand-in object
    in tests), so availability stays absent rather than failing the turn.
    """
    if getattr(user, "pk", None) is None:
        return None
    try:
        from crank.empty_state import derive_state

        return derive_state(user=user).to_dict(include_staff=False)
    except Exception:  # pragma: no cover - availability is best-effort
        logger.exception("availability derivation failed; continuing without it")
        return None


class PreferenceService(Protocol):
    """Port for the preference lifecycle service (issue #306).

    ``validate_patch`` raises :class:`InvalidPreferencePatchError` for a
    malformed or schema-violating patch; ``apply_patch`` applies an accepted
    patch transactionally and returns whether preferences changed.
    """

    def validate_patch(self, patch: dict[str, Any]) -> None:
        ...  # pragma: no cover - structural Protocol stub, never called directly

    def propose_patch(self, patch: dict[str, Any], scope: str = "account") -> dict:
        """Return a read-only proposal for an accepted patch; never writes.

        The chat turn never persists a model-proposed patch (issue #466
        review): the orchestrator turns it into a proposal — field-level
        ``changes``, ``base_revision``, ``scope``, and (for
        ``scope="search"``) the in-memory ``effective_document`` — which the
        user later applies or dismisses through a dedicated endpoint. Returns
        the proposal payload; raises :class:`InvalidPreferencePatchError` for
        a patch the canonical validator rejects.
        """
        ...  # pragma: no cover - structural Protocol stub, never called directly


@dataclass(frozen=True)
class OrchestratorResult:
    """The validated, safe result of a single orchestrated turn."""

    message: str
    cited_organization_ids: tuple = ()
    cited_job_listing_ids: tuple = ()
    preference_patch: dict[str, Any] | None = None
    preferences_changed: bool = False
    preference_proposal: dict[str, Any] | None = None
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
        availability_service: AvailabilityDatasource | None = None,
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
        self._availability_service = availability_service or _default_availability_service
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
        lifecycle_guard: Callable[[], None] | None = None,
        persist_reply: Callable[..., None] | None = None,
        token_budget: int | None = None,
        max_tokens: int | None = None,
    ) -> OrchestratorResult:
        """Run one turn and emit only bounded interactive-call telemetry.

        A model-proposed preference patch is never applied in-turn (issue
        #466 review): the turn returns a read-only proposal the user applies
        or dismisses afterwards, so no version baseline is needed here.
        ``lifecycle_guard``, when given, runs inside the same transaction as
        the reply persistence and raises :class:`ConversationClosedError` to
        abort when the conversation was reset or deleted mid-turn (issue #487
        review, MAJOR-2). ``persist_reply``, when given, is invoked inside
        that SAME transaction, so the guard claim and the assistant reply
        share one commit boundary (issue #487 review round 2, MAJOR-1); it
        raises :class:`ConversationClosedError` to discard the whole turn.
        """
        started = time.monotonic()
        try:
            result = self._run(
                user_prompt=user_prompt,
                conversation=conversation,
                preference_markdown=preference_markdown,
                lifecycle_guard=lifecycle_guard,
                persist_reply=persist_reply,
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
        lifecycle_guard: Callable[[], None] | None = None,
        persist_reply: Callable[..., None] | None = None,
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
        if match_enabled:
            self._validate_match_references(completion.message, match_data)

        # Anti-echo guard: never serve a reply that merely restates the user's
        # message when server data was available and no tool-grounded work was
        # done (guards against a demo/echo provider leaking into production).
        inventory_nonempty = bool(known_ids or known_listing_ids)
        self._guard_echo(
            user_prompt=user_prompt,
            completion=completion,
            inventory_nonempty=inventory_nonempty,
        )

        # 6. Build citation-validated structured results BEFORE the guarded
        # commit: the reply persistence hook serializes them inside the same
        # transaction as the patch write. Pure computation over server-
        # controlled rows loaded this turn — no I/O, no model data.
        structured_results = self._build_results(
            completion.cited_organization_ids,
            org_rows,
            completion.cited_job_listing_ids,
            listing_rows,
        )

        # 7. A model-proposed preference patch is NEVER applied in-turn
        # (issue #466 review): the turn produces a read-only proposal —
        # field-level diff, base revision, scope — which the user reviews and
        # applies or dismisses through a dedicated endpoint. The reply itself
        # still persists inside the guarded single transaction (issue #487
        # review round 2, MAJOR-1): the lifecycle guard's write-first
        # conversation-row claim and the reply persistence share one commit
        # boundary, so a mid-turn reset/delete can never attach a reply to a
        # closed conversation.
        preference_proposal: dict[str, Any] | None = None
        if completion.has_preference_patch:
            preference_proposal = self._propose_preference_patch(
                completion.preference_patch, completion.preference_scope
            )
        if persist_reply is not None:
            self._commit_turn_writes(
                lifecycle_guard=lifecycle_guard,
                persist_reply=persist_reply,
                reply_text=completion.message,
                results=structured_results,
            )

        tools_used = tuple(tools_used)
        result_counts = tuple(result_counts)
        cited_ids_count = len(completion.cited_organization_ids) + len(
            completion.cited_job_listing_ids
        )

        logger.info(
            "job_search_complete prompt_id=%s cited_orgs=%s cited_listings=%s "
            "preference_proposal=%s",
            model_context.prompt_id,
            completion.cited_organization_ids,
            completion.cited_job_listing_ids,
            preference_proposal is not None,
        )
        return OrchestratorResult(
            message=completion.message,
            cited_organization_ids=completion.cited_organization_ids,
            cited_job_listing_ids=completion.cited_job_listing_ids,
            preference_patch=(
                completion.preference_patch if completion.has_preference_patch else None
            ),
            preferences_changed=False,
            preference_proposal=preference_proposal,
            results=structured_results,
            tools_used=tools_used,
            result_counts=result_counts,
            cited_ids_count=cited_ids_count,
            empty_result=cited_ids_count == 0,
            inventory_nonempty=inventory_nonempty,
        )

    def _propose_preference_patch(
        self, patch: dict[str, Any], scope: str
    ) -> dict[str, Any]:
        """Build the read-only proposal for a model-proposed patch.

        Validation goes through the same canonical validator the apply path
        uses (no chat-only branch). The proposal carries a fresh proposal id,
        the field-level diff, the base revision the eventual apply must be
        preconditioned on, the scope, and an opaque client-held token the
        apply endpoint re-validates (owner-scoped, revision-guarded — the
        same contract as the undo token). For ``scope="search"`` the
        in-memory effective document is computed here and used to drive one
        match reload via ``preferences_override`` — never persisted (AC-9).
        """
        import uuid

        # Schema validation gates before any proposal work, preserving the
        # stable InvalidPreferencePatchError precedence.
        try:
            self._preference_service.validate_patch(patch)
        except InvalidPreferencePatchError:
            raise
        except Exception as exc:  # defensive: port must raise typed error
            raise InvalidPreferencePatchError(str(exc)) from exc
        try:
            proposal = self._preference_service.propose_patch(patch, scope=scope)
        except InvalidPreferencePatchError:
            raise
        except Exception as exc:  # defensive: port must raise typed error
            raise InvalidPreferencePatchError(str(exc)) from exc
        token = {
            "patch": patch,
            "scope": scope,
            "base_revision": proposal.get("base_revision"),
        }
        result = {
            "id": uuid.uuid4().hex,
            "scope": scope,
            "changes": proposal.get("changes") or [],
            "change_count": proposal.get("change_count", 0),
            "base_revision": proposal.get("base_revision"),
            "unsupported_criteria": proposal.get("unsupported_criteria") or [],
            "token": token,
        }
        if scope == "search":
            # This-search-only filter: run one match reload against the
            # in-memory effective document (never saved) so the proposal can
            # show what the temporary filter would match (issue #466 review,
            # AC-9).
            effective = proposal.get("effective_document")
            if effective is not None:
                result["matches"] = self._load_matches(preferences_override=effective)
        return result

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

    def _load_matches(self, preferences_override: Any = None) -> dict[str, Any]:
        """Load preference-grounded matches for the current user.

        ``preferences_override`` (issue #466 review) supplies an in-memory
        effective document for a this-search-only proposal: the match service
        evaluates it without any preference store write. Ports that do not
        declare the parameter are called without it (legacy doubles).
        """
        if self._match_service is None or self._user is None:
            return {"job_matches": [], "organization_matches": []}
        capped = tools.clamp_result_limit(
            self._max_match_results, maximum=tools.MAX_MATCH_RESULTS
        )
        if preferences_override is not None:
            import inspect

            try:
                params = inspect.signature(self._match_service).parameters
            except (TypeError, ValueError):  # pragma: no cover - builtins etc.
                params = {}
            if "preferences_override" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                return self._match_service(
                    user=self._user, limit=capped,
                    preferences_override=preferences_override,
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
        # Availability state for honesty about inventory/matches (issue #476).
        # Only derived for a persisted user; stand-in objects in tests keep it
        # absent so no database is touched.
        availability = kwargs.get("availability")
        if availability is None and self._user is not None:
            availability = self._availability_service(self._user)
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
            availability=availability,
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
    def _validate_match_references(message: str, match_data: dict[str, Any]) -> None:
        """Reject a reply that references an unexposed requirement or evidence id.

        The match tool returns a bounded requirement structure (requirement
        paths and evidence ids). A reply naming a requirement path or evidence
        id outside that set did not come from server-controlled output and
        fails the turn with the existing typed reference-error path (AC-11).
        """
        if not match_data:
            return
        exposed_paths: set[str] = set()
        exposed_evidence_ids: set[int] = set()
        for key in ("job_matches", "organization_matches"):
            for row in match_data.get(key, []) or []:
                if not isinstance(row, dict):
                    continue
                for req in row.get("requirements", []) or []:
                    if isinstance(req, dict) and req.get("path"):
                        exposed_paths.add(req["path"])
                    # Evidence ids are tied to the individual outcome that
                    # cited them, not just the row-level list.
                    if isinstance(req, dict) and req.get("source_kind") == "evidence":
                        src_id = req.get("source_id")
                        if isinstance(src_id, int) and not isinstance(src_id, bool):
                            exposed_evidence_ids.add(src_id)
                for eid in row.get("evidence_ids", []) or []:
                    if isinstance(eid, bool):
                        continue
                    if isinstance(eid, int):
                        exposed_evidence_ids.add(eid)
        for token in _REQUIREMENT_PATH_RE.findall(message or ""):
            if token not in exposed_paths:
                raise InvalidRequirementReferenceError(
                    f"model referenced a requirement not exposed by the match tool: {token}"
                )
        for m in _EVIDENCE_REF_RE.finditer(message or ""):
            if int(m.group(1)) not in exposed_evidence_ids:
                raise InvalidRequirementReferenceError(
                    f"model referenced an evidence id not exposed by the match tool: {m.group(1)}"
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

    def _commit_turn_writes(
        self,
        *,
        lifecycle_guard: Callable[[], None] | None,
        persist_reply: Callable[..., None] | None,
        reply_text: str,
        results: Any,
    ) -> None:
        """Commit the turn's reply persistence in ONE guarded transaction.

        Issue #487 review round 2 (MAJOR-1/MAJOR-2): the lifecycle guard's
        write-first conversation-row claim and the reply persistence share a
        single transaction and therefore ONE commit boundary. A reset/delete
        landing after the claim blocks on the row lock until that commit; one
        landing before the claim makes the claim match no active row and the
        whole turn aborts with :class:`ConversationClosedError`, nothing
        persisted. (The turn no longer writes preferences here — issue #466
        review moved preference persistence to the user-driven apply
        endpoint — so the reply is the only write domain left to serialize.)

        SQLite contention (MAJOR-2): the claim is write-first, so this
        transaction's first statement takes SQLite's writer reservation — no
        read→write upgrade can raise ``database is locked`` — and any
        residual lock contention with a concurrent writer is mapped to the
        documented retryable 409 ``conversation_closed`` envelope instead of
        surfacing as a 500 ``invalid_output``.
        """
        # Lazy import keeps this module importable without Django configured;
        # every runtime path (including tests) runs under Django settings.
        from django.db import IntegrityError, transaction

        try:
            with transaction.atomic():
                if lifecycle_guard is not None:
                    # Guard failures abort the transaction (and the whole
                    # turn); the guard is trusted server-side code, so its
                    # typed error (ConversationClosedError) propagates
                    # unwrapped. Contention with a concurrent lifecycle
                    # writer is mapped to the same retryable 409 envelope.
                    try:
                        lifecycle_guard()
                    except ConversationClosedError:
                        raise
                    except Exception as exc:
                        if is_database_locked(exc):
                            raise ConversationClosedError(
                                "conversation lifecycle write contended with "
                                "the guarded turn; retry"
                            ) from exc
                        raise
                if persist_reply is not None:
                    # The reply persists inside the guarded transaction
                    # (MAJOR-1): the hook claims the conversation row
                    # (already held from the guard claim), inserts the
                    # assistant message, and re-verifies lifecycle state
                    # before the single commit.
                    try:
                        persist_reply(
                            reply_text=reply_text,
                            results=results,
                            preferences_changed=False,
                        )
                    except ConversationClosedError:
                        raise
                    except Exception as exc:
                        if is_database_locked(exc) or isinstance(exc, IntegrityError):
                            # The conversation row was reset/deleted in the
                            # window (FK failure) or the lifecycle write
                            # contended: discard the whole turn.
                            raise ConversationClosedError(
                                "conversation was reset or deleted while the "
                                "assistant was responding"
                            ) from exc
                        raise
        except ConversationClosedError:
            raise
        except Exception as exc:  # defensive: port must raise typed error
            if is_database_locked(exc):
                # An ``OperationalError("database is locked")`` raised by the
                # guarded transaction's own COMMIT lands here AFTER every
                # inner guarded block has run and been mapped — the outer
                # boundary bypasses the inner checks. The atomic block has
                # already rolled back, so nothing persisted; map the residual
                # contention to the documented retryable 409 envelope instead
                # of the 500 ``invalid_output`` path. Narrow by construction:
                # only ``is_database_locked`` matches (SQLite "database is
                # locked", MySQL lock-wait/deadlock); genuine validation
                # errors and non-contention backend failures keep their
                # existing typed paths.
                raise ConversationClosedError(
                    "turn commit contended with a concurrent writer; the "
                    "turn rolled back and is retryable"
                ) from exc
            raise InvalidPreferencePatchError(str(exc)) from exc
