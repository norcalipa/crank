# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Unit tests for the provider gateway contract."""
from crank.agents.job_search.gateway import GatewayResponse, ProviderGateway


class TestGatewayResponse:
    def test_output_tokens(self):
        resp = GatewayResponse(text="hi", usage={"output_tokens": 42})
        assert resp.output_tokens == 42

    def test_default_usage_zero(self):
        assert GatewayResponse(text="hi").output_tokens == 0


class TestProviderGatewayBase:
    def test_abstract_complete_raises(self):
        class DirectGateway(ProviderGateway):
            def complete(self, request):  # noqa: D102
                return super().complete(request)

        try:
            DirectGateway().complete(None)
        except NotImplementedError:
            pass
        else:
            raise AssertionError("expected NotImplementedError")

    def test_context_manager(self):
        class NoopGateway(ProviderGateway):
            def complete(self, request):
                return GatewayResponse(text="ok")

        with NoopGateway() as gw:
            assert gw.complete(None).text == "ok"
