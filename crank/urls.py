# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
URL configuration for crank project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.decorators.cache import cache_page

from crank.decorators import cache_page_if_anonymous_method
from crank.services.scores import SCORE_CACHE_KEY_VERSION
from crank.views.api import (
    account_whoami,
    organization_detail,
    organization_provenance,
    organization_scores,
)
from crank.views.assistant_status import assistant_status
from crank.views.company_requests import company_requests
from crank.views.fundinground import FundingRoundChoicesView
from crank.views.health import readiness
from crank.views.help import HelpView, PrivacyView
from crank.views.index import IndexView, algo_page
from crank.views.job_matches import (
    job_match_detail,
    job_match_dismiss,
    job_match_list,
    job_match_ranked,
    job_match_seen,
    job_match_status,
)
from crank.views.job_search import (
    agent_conversation_delete,
    agent_conversation_detail,
    agent_conversation_export,
    agent_conversation_list,
    agent_conversation_reset,
    agent_preference_apply,
    agent_preference_undo,
)
from crank.views.job_search_page import job_search_page
from crank.views.logout import CustomLogoutView
from crank.views.release_diagnostics import release_diagnostics
from crank.views.rtopolicy import RTOPolicyChoicesView

app_name = "crank"

urlpatterns = [
    path("healthz/ready/", readiness, name="readiness"),
    path("", IndexView.as_view(), name="index"),
    path("admin/", admin.site.urls),
    # The ``/algo/<id>/`` shell is served by the explicit ``algo_page`` view
    # which caches under the ``algorithm_{id}_page`` key — listed in
    # ``scores.affected_cache_keys()`` — so score publication clears the
    # rendered HTML together with the result keys. The shell is auth-neutral
    # (see ``_navigation.html``), so one entry serves every account safely
    # (addresses issue #487 review MINOR-1: the old ``cache_page`` entry
    # was not auth-aware).
    path("algo/<int:algorithm_id>/", algo_page, name="index"),
    path('api/funding-round-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(FundingRoundChoicesView.as_view()), name='funding_round_choices'),
    path('api/rto-policy-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(RTOPolicyChoicesView.as_view()), name='rto_policy_choices'),
    path('api/organizations/<int:pk>/', organization_detail, name='organization-detail'),
    path('api/account/whoami/', account_whoami, name='account-whoami'),
    path('api/organizations/<int:pk>/provenance/', organization_provenance, name='organization-provenance'),
    path('api/organizations/<int:pk>/scores/', organization_scores, name='organization-scores'),
    path('api/company-requests/', company_requests, name='company-request-list'),
    path('api/company-requests/<int:pk>/', company_requests, name='company-request-detail'),
    path('chat/', job_search_page, name='job_search'),
    path('api/agent/assistant-status/', assistant_status, name='agent-assistant-status'),
    path('api/agent/conversations/', agent_conversation_list, name='agent-conversation-list'),
    path('api/agent/conversations/<int:conversation_id>/', agent_conversation_detail, name='agent-conversation-detail'),
    path('api/agent/conversations/<int:conversation_id>/export/', agent_conversation_export, name='agent-conversation-export'),
    path('api/agent/conversations/<int:conversation_id>/reset/', agent_conversation_reset, name='agent-conversation-reset'),
    path('api/agent/conversations/<int:conversation_id>/delete/', agent_conversation_delete, name='agent-conversation-delete'),
    path('api/agent/preferences/apply/', agent_preference_apply, name='agent-preference-apply'),
    path('api/agent/preferences/undo/', agent_preference_undo, name='agent-preference-undo'),
    path('api/job-matches/', job_match_list, name='job-match-list'),
    path('api/job-matches/<int:match_id>/', job_match_detail, name='job-match-detail'),
    path('api/job-matches/<int:match_id>/seen/', job_match_seen, name='job-match-seen'),
    path('api/job-matches/<int:match_id>/dismiss/', job_match_dismiss, name='job-match-dismiss'),
    path('api/job-matches/status/', job_match_status, name='job-match-status'),
    path('api/job-matches/ranked/', job_match_ranked, name='job-match-ranked'),
    path('help/', HelpView.as_view(), name='help'),
    path('privacy/', PrivacyView.as_view(), name='privacy'),
    path('staff/release-diagnostics/', release_diagnostics, name='release-diagnostics'),
    path('api-auth/', include('rest_framework.urls')),
    path('accounts/logout/', CustomLogoutView.as_view(), name='account_logout'),
    path('accounts/', include('allauth.urls')),
]
