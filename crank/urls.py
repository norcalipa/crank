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
from allauth.account.views import LogoutView
from django.contrib import admin
from django.urls import path, include
from django.views.decorators.cache import cache_page
from django.conf import settings

from crank.views.fundinground import FundingRoundChoicesView
from crank.views.rtopolicy import RTOPolicyChoicesView
from crank.views.index import IndexView
from crank.views.organization import OrganizationView
from crank.views.logout import CustomLogoutView

app_name = "crank"

urlpatterns = [
    path("", IndexView.as_view(), name="index"),
    path("admin/", admin.site.urls),
    path("algo/<int:algorithm_id>/", cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(IndexView.as_view()), name="index"),
    path("organization/<int:pk>/", cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(OrganizationView.as_view()), name="organization"),
    path('api/funding-round-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(FundingRoundChoicesView.as_view()), name='funding_round_choices'),
    path('api/rto-policy-choices/', cache_page(settings.CACHE_MIDDLEWARE_SECONDS)(RTOPolicyChoicesView.as_view()), name='rto_policy_choices'),
    path('api-auth/', include('rest_framework.urls')),
    path('accounts/logout/', CustomLogoutView.as_view(), name='account_logout'),
    path('accounts/', include('allauth.urls')),
]
