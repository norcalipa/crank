# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from allauth.account.views import LogoutView as AllauthLogoutView
from django.shortcuts import redirect, render


@method_decorator(csrf_exempt, name='dispatch')
class CustomLogoutView(AllauthLogoutView):
    def post(self, request, *args, **kwargs):
        _ = super().post(request, *args, **kwargs)
        return redirect('index')  # Redirect to home page after logout