"""Air-gap guard tests.

The whole point of the guard is that the air-gap is verifiable rather than
promised, so these tests are the proof. They make no outbound connections: the
guard must refuse before any packet leaves.
"""

from __future__ import annotations

import socket

import pytest

from vibekill.airgap import AirgapGuard, EgressBlocked, engage, is_engaged, subprocess_env


@pytest.fixture
def guard():
    g = AirgapGuard(allow_hosts={"127.0.0.1", "localhost", "::1"})
    g.engage()
    yield g
    g.release()


class TestPolicy:
    def test_loopback_is_allowed(self, guard):
        assert guard.host_allowed("127.0.0.1")
        assert guard.host_allowed("localhost")
        assert guard.host_allowed("::1")

    def test_public_addresses_are_not_allowed(self, guard):
        assert not guard.host_allowed("93.184.216.34")
        assert not guard.host_allowed("example.com")

    def test_private_ranges_are_not_allowed_by_default(self, guard):
        """A corporate LAN is still egress; only loopback is implicitly trusted."""
        assert not guard.host_allowed("10.0.0.5")
        assert not guard.host_allowed("192.168.1.10")


class TestBlocking:
    def test_connection_to_a_public_host_is_refused(self, guard):
        with pytest.raises(EgressBlocked):
            socket.create_connection(("example.com", 443), timeout=1)

    def test_name_resolution_is_refused(self, guard):
        """DNS is itself traffic leaving the machine, so it is blocked too."""
        with pytest.raises(EgressBlocked):
            socket.getaddrinfo("example.com", 443)

    def test_socket_connect_is_refused(self, guard):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(EgressBlocked):
                sock.connect(("8.8.8.8", 53))
        finally:
            sock.close()

    def test_loopback_resolution_still_works(self, guard):
        assert socket.getaddrinfo("127.0.0.1", 8080)

    def test_violations_are_recorded_for_the_report(self, guard):
        with pytest.raises(EgressBlocked):
            socket.getaddrinfo("telemetry.example.net", 443)
        assert len(guard.violations) == 1
        assert "telemetry.example.net" in str(guard.violations[0])

    def test_error_message_names_the_host_and_the_way_out(self, guard):
        with pytest.raises(EgressBlocked) as excinfo:
            socket.create_connection(("api.github.com", 443), timeout=1)
        message = str(excinfo.value)
        assert "api.github.com" in message
        assert "--mode github" in message


class TestLifecycle:
    def test_release_restores_normal_behaviour(self):
        g = AirgapGuard(allow_hosts={"127.0.0.1"})
        g.engage()
        assert is_engaged()
        g.release()
        assert not is_engaged()
        # The real getaddrinfo is back; resolving loopback must not raise.
        socket.getaddrinfo("127.0.0.1", 80)

    def test_engage_is_idempotent(self):
        first = engage(["127.0.0.1"])
        try:
            assert engage(["127.0.0.1"]) is first
        finally:
            first.release()

    def test_context_manager_releases_on_exit(self):
        with AirgapGuard(allow_hosts={"127.0.0.1"}):
            assert is_engaged()
        assert not is_engaged()


class TestSubprocessEnv:
    def test_credentials_are_stripped(self):
        """A stale token is how an "air-gapped" tool ends up authenticating."""
        env = subprocess_env({"GITHUB_TOKEN": "ghp_secret", "PATH": "/usr/bin"})
        assert "GITHUB_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"

    def test_proxies_point_nowhere(self):
        env = subprocess_env({})
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:1"

    def test_telemetry_is_disabled(self):
        env = subprocess_env({})
        assert env["SEMGREP_SEND_METRICS"] == "off"
        assert env["DO_NOT_TRACK"] == "1"


def test_verify_airgap_proves_enforcement():
    """`vibekill doctor` relies on this actively demonstrating the block."""
    from vibekill.pipeline import verify_airgap
    from vibekill.settings import Settings

    ok, detail = verify_airgap(Settings(enforce_airgap=True))
    assert ok, detail
