import pytest
from django.test import RequestFactory

from apps.core.http import client_ip

SPOOFED = "10.0.0.1, 192.168.0.5"
REAL = "203.0.113.9"


@pytest.fixture
def request_from():
    """Build a request as it would arrive, with an optional X-Forwarded-For."""

    def build(forwarded_for: str | None = None, remote_addr: str = "172.16.0.2"):
        request = RequestFactory().get("/api/auth/login/")
        request.META["REMOTE_ADDR"] = remote_addr
        if forwarded_for is not None:
            request.META["HTTP_X_FORWARDED_FOR"] = forwarded_for
        return request

    return build


class TestWithOneProxyInFront:
    """The deployed arrangement: Route 53 to a load balancer to this process."""

    @pytest.fixture(autouse=True)
    def one_proxy(self, settings):
        settings.TRUSTED_PROXY_COUNT = 1

    def test_the_last_entry_is_the_caller(self, request_from):
        assert client_ip(request_from(f"{SPOOFED}, {REAL}")) == REAL

    def test_a_caller_cannot_choose_their_own_address(self, request_from):
        """The whole finding: whatever they put in the header, they are still counted as REAL."""
        first = client_ip(request_from(f"10.0.0.1, {REAL}"))
        second = client_ip(request_from(f"10.0.0.2, {REAL}"))
        third = client_ip(request_from(f"8.8.8.8, 1.1.1.1, 9.9.9.9, {REAL}"))

        assert first == second == third == REAL

    def test_the_header_the_proxy_wrote_on_its_own_is_the_caller(self, request_from):
        """Nothing sent by the caller, so the proxy's single entry is all there is."""
        assert client_ip(request_from(REAL)) == REAL

    def test_padding_around_entries_is_ignored(self, request_from):
        """Whitespace would otherwise make "203.0.113.9" and " 203.0.113.9" two identities."""
        assert client_ip(request_from(f"10.0.0.1,\t{REAL}  ")) == REAL

    def test_empty_entries_are_ignored(self, request_from):
        """A caller sending a trailing comma must not push the real address out of position."""
        assert client_ip(request_from(f"10.0.0.1,, {REAL},")) == REAL

    def test_no_header_at_all_falls_back_to_the_socket(self, request_from):
        """Shortening the header is the only move a caller has, and it leads somewhere fixed."""
        assert client_ip(request_from(None, remote_addr="172.16.0.2")) == "172.16.0.2"

    def test_an_empty_header_falls_back_to_the_socket(self, request_from):
        assert client_ip(request_from("", remote_addr="172.16.0.2")) == "172.16.0.2"


class TestWithTwoProxiesInFront:
    """What the configuration would become if a CDN were put in front of the API itself."""

    @pytest.fixture(autouse=True)
    def two_proxies(self, settings):
        settings.TRUSTED_PROXY_COUNT = 2

    def test_the_second_entry_from_the_right_is_the_caller(self, request_from):
        assert client_ip(request_from(f"10.0.0.1, {REAL}, 192.168.0.5")) == REAL

    def test_a_header_shorter_than_the_chain_falls_back_to_the_socket(self, request_from):
        """Two proxies should mean two entries. One means the chain is not what we think it is,
        so the entry present may have come from the caller and must not be believed."""
        assert client_ip(request_from("10.0.0.1", remote_addr="172.16.0.2")) == "172.16.0.2"


class TestWithNoProxy:
    """A developer machine and the test suite, where the header is only ever caller-supplied."""

    @pytest.fixture(autouse=True)
    def no_proxy(self, settings):
        settings.TRUSTED_PROXY_COUNT = 0

    def test_the_header_is_ignored_entirely(self, request_from):
        assert client_ip(request_from(f"{SPOOFED}, {REAL}", remote_addr="127.0.0.1")) == "127.0.0.1"
