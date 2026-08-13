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
from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView
from django.views.decorators.cache import cache_page
from django.conf import settings

from crank.views.fundinground import FundingRoundChoicesView
from crank.views.rtopolicy import RTOPolicyChoicesView
from crank.views.index import IndexView
from crank.views.logout import CustomLogoutView
from crank.views.api import organization_detail, organization_scores, organization_provenance
from crank.views.company_requests import company_requests
from crank.views.job_search import (
    agent_conversation_list,
    agent_conversation_detail,
    agent_conversation_export,
    agent_conversation_reset,
    agent_conversation_delete,
)
from crank.views.job_matches import (
    job_match_list,
    job_match_detail,
    job_match_seen,
    job_match_dismiss,
)
from crank.views.health import readiness
from crank.views.help import HelpView, PrivacyView
from crank.auth import login_required_with_expiry

app_name = "crank"

urlpatterns = [
    path("healthz/ready/", readiness, name="readiness"),
    path("", IndexView.as_view(), name="index"),
    path("admin/", admin.site.urls),
    path("algo/<int:algorithm_id>/", cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(IndexView.as_view()), name="index"),
    path('api/funding-round-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(FundingRoundChoicesView.as_view()), name='funding_round_choices'),
    path('api/rto-policy-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(RTOPolicyChoicesView.as_view()), name='rto_policy_choices'),
    path('api/organizations/<int:pk>/', organization_detail, name='organization-detail'),
    path('api/organizations/<int:pk>/provenance/', organization_provenance, name='organization-provenance'),
    path('api/organizations/<int:pk>/scores/', organization_scores, name='organization-scores'),
    path('api/company-requests/', company_requests, name='company-request-list'),
    path('api/company-requests/<int:pk>/', company_requests, name='company-request-detail'),
    path('chat/', login_required_with_expiry(TemplateView.as_view(template_name='crank/job_search.html')), name='job_search'),
    path('api/agent/conversations/', agent_conversation_list, name='agent-conversation-list'),
    path('api/agent/conversations/<int:conversation_id>/', agent_conversation_detail, name='agent-conversation-detail'),
    path('api/agent/conversations/<int:conversation_id>/export/', agent_conversation_export, name='agent-conversation-export'),
    path('api/agent/conversations/<int:conversation_id>/reset/', agent_conversation_reset, name='agent-conversation-reset'),
    path('api/agent/conversations/<int:conversation_id>/delete/', agent_conversation_delete, name='agent-conversation-delete'),
    path('api/job-matches/', job_match_list, name='job-match-list'),
    path('api/job-matches/<int:match_id>/', job_match_detail, name='job-match-detail'),
    path('api/job-matches/<int:match_id>/seen/', job_match_seen, name='job-match-seen'),
    path('api/job-matches/<int:match_id>/dismiss/', job_match_dismiss, name='job-match-dismiss'),
    path('help/', HelpView.as_view(), name='help'),
    path('privacy/', PrivacyView.as_view(), name='privacy'),
    path('api-auth/', include('rest_framework.urls')),
    path('accounts/logout/', CustomLogoutView.as_view(), name='account_logout'),
    path('accounts/', include('allauth.urls')),
]
