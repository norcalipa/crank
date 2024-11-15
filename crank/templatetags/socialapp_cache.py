# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django import template
from django.core.cache import cache
from allauth.socialaccount.models import SocialApp
from django.conf import settings

register = template.Library()

@register.simple_tag
def get_cached_social_app(provider):
    cache_key = f'social_app_{provider}'
    social_app = cache.get(cache_key)
    if not social_app:
        social_app = SocialApp.objects.filter(provider=provider).prefetch_related('sites').first()
        cache.set(cache_key, social_app, timeout=settings.CACHE_MIDDLEWARE_SECONDS)
    return social_app