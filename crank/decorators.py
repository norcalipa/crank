# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from functools import wraps

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

def cache_page_if_anonymous_method(timeout, view_func=None):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_authenticated:
                return view_func(request, *args, **kwargs)
            return cache_page(timeout)(view_func)(request, *args, **kwargs)
        return _wrapped_view

    def class_decorator(cls):
        cls.dispatch = method_decorator(decorator, name='dispatch')(cls.dispatch)
        return cls

    return class_decorator if isinstance(view_func, type) else decorator