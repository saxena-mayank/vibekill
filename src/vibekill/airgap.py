"""Runtime egress guard.

"Air-gapped" is usually a deployment promise nobody verifies. This module turns
it into a property of the running process: while the guard is engaged, any
attempt to open a socket to a host outside the allowlist raises
`EgressBlocked`, and name resolution for non-allowlisted hosts is refused too,
since a DNS query is itself traffic leaving the machine.

The guard is intentionally loud. If a future analyser, a plugin, or a stray
`requests.get` in someone's custom rule tries to phone home, the review fails
with a precise message instead of quietly exfiltrating a diff.

Scope and honest limits: this patches Python's `socket` layer, which covers
`urllib`, `http.client`, `ssl`, and anything built on them. It does not sandbox
subprocesses. Child processes (semgrep, eslint, ...) are separately constrained
by passing them offline flags and a scrubbed environment; see `tools/base.py`.
For a hard guarantee, run the whole thing in a container with no network
interface, which this guard complements rather than replaces.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["EgressBlocked", "AirgapGuard", "engage", "current_guard", "is_engaged"]


class EgressBlocked(RuntimeError):
    """Raised when code attempts network egress under an engaged air-gap."""


@dataclass
class Violation:
    host: str
    port: int | None
    kind: str  # "connect" | "resolve"
    detail: str = ""

    def __str__(self) -> str:
        target = f"{self.host}:{self.port}" if self.port else self.host
        return f"{self.kind} {target}" + (f" ({self.detail})" if self.detail else "")


_LOOPBACK_ALIASES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


@dataclass
class AirgapGuard:
    """Installs and removes the socket-level block.

    Not reentrant on purpose: nesting guards with different allowlists is a
    footgun, so `engage` returns the existing guard if one is already active.
    """

    allow_hosts: set[str] = field(default_factory=set)
    allow_unix_sockets: bool = True
    violations: list[Violation] = field(default_factory=list)
    _allowed_ips: set[str] = field(default_factory=set)
    _originals: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _engaged: bool = False

    # ------------------------------------------------------------------ #
    # Policy
    # ------------------------------------------------------------------ #

    def _normalise_allowlist(self) -> None:
        for entry in list(self.allow_hosts):
            name = entry.strip().lower()
            if not name:
                continue
            try:
                self._allowed_ips.add(str(ipaddress.ip_address(name)))
            except ValueError:
                if name in _LOOPBACK_ALIASES:
                    self._allowed_ips.update({"127.0.0.1", "::1"})
                # Non-loopback hostnames stay in allow_hosts and are resolved
                # lazily by the patched getaddrinfo, which then records the IPs.

    def host_allowed(self, host: str) -> bool:
        name = (host or "").strip().lower()
        if not name:
            return False
        if name in self.allow_hosts:
            return True
        if name in _LOOPBACK_ALIASES and self._loopback_allowed():
            return True
        try:
            addr = ipaddress.ip_address(name)
        except ValueError:
            return name in self.allow_hosts
        if str(addr) in self._allowed_ips:
            return True
        # Loopback is allowed whenever the operator allowlisted any loopback
        # spelling, which is the normal case for a local model runtime.
        return addr.is_loopback and self._loopback_allowed()

    def _loopback_allowed(self) -> bool:
        return bool(self._allowed_ips & {"127.0.0.1", "::1"}) or bool(
            self.allow_hosts & _LOOPBACK_ALIASES
        )

    def _record(self, violation: Violation) -> EgressBlocked:
        with self._lock:
            self.violations.append(violation)
        allowed = ", ".join(sorted(self.allow_hosts)) or "(nothing)"
        return EgressBlocked(
            f"air-gap: blocked {violation}. Allowed hosts: {allowed}. "
            "Run with --mode github or --no-enforce-airgap if egress is intended."
        )

    # ------------------------------------------------------------------ #
    # Install / remove
    # ------------------------------------------------------------------ #

    def engage(self) -> AirgapGuard:
        if self._engaged:
            return self
        self._normalise_allowlist()

        guard = self
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_create_connection = socket.create_connection
        real_getaddrinfo = socket.getaddrinfo

        self._originals = {
            "connect": real_connect,
            "connect_ex": real_connect_ex,
            "create_connection": real_create_connection,
            "getaddrinfo": real_getaddrinfo,
        }

        def _check_address(sock_family: int, address: Any, kind: str = "connect") -> None:
            if sock_family == getattr(socket, "AF_UNIX", object()):
                if guard.allow_unix_sockets:
                    return
                raise guard._record(Violation(str(address), None, kind, "unix socket"))
            if not isinstance(address, tuple) or not address:
                # Unusual families (AF_NETLINK, AF_PACKET...) are not egress in
                # the sense we care about, but they are not something a reviewer
                # should be doing either.
                raise guard._record(Violation(str(address), None, kind, f"family={sock_family}"))
            host = str(address[0])
            port = address[1] if len(address) > 1 and isinstance(address[1], int) else None
            if not guard.host_allowed(host):
                raise guard._record(Violation(host, port, kind))

        def guarded_connect(self_sock: socket.socket, address: Any):  # type: ignore[no-untyped-def]
            _check_address(self_sock.family, address)
            return real_connect(self_sock, address)

        def guarded_connect_ex(self_sock: socket.socket, address: Any):  # type: ignore[no-untyped-def]
            _check_address(self_sock.family, address)
            return real_connect_ex(self_sock, address)

        def guarded_create_connection(address: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if isinstance(address, tuple) and address:
                host = str(address[0])
                port = address[1] if len(address) > 1 else None
                if not guard.host_allowed(host):
                    raise guard._record(
                        Violation(host, port if isinstance(port, int) else None, "connect")
                    )
            return real_create_connection(address, *args, **kwargs)

        def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            name = str(host or "").strip().lower()
            if name in guard.allow_hosts and name not in _LOOPBACK_ALIASES:
                results = real_getaddrinfo(host, port, *args, **kwargs)
                for entry in results:
                    sockaddr = entry[4]
                    if sockaddr:
                        guard._allowed_ips.add(str(sockaddr[0]))
                return results
            if name and not guard.host_allowed(name):
                raise guard._record(
                    Violation(name, port if isinstance(port, int) else None, "resolve")
                )
            return real_getaddrinfo(host, port, *args, **kwargs)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

        self._engaged = True
        global _CURRENT
        _CURRENT = self
        return self

    def release(self) -> None:
        if not self._engaged:
            return
        socket.socket.connect = self._originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._originals["connect_ex"]  # type: ignore[method-assign]
        socket.create_connection = self._originals["create_connection"]  # type: ignore[assignment]
        socket.getaddrinfo = self._originals["getaddrinfo"]  # type: ignore[assignment]
        self._engaged = False
        global _CURRENT
        if _CURRENT is self:
            _CURRENT = None

    def __enter__(self) -> AirgapGuard:
        return self.engage()

    def __exit__(self, *exc: Any) -> None:
        self.release()


_CURRENT: AirgapGuard | None = None


def engage(allow_hosts: Iterable[str] = ()) -> AirgapGuard:
    """Engage the air-gap, or return the guard already in force."""
    global _CURRENT
    if _CURRENT is not None and _CURRENT._engaged:
        return _CURRENT
    guard = AirgapGuard(allow_hosts={h.strip().lower() for h in allow_hosts if h and h.strip()})
    return guard.engage()


def current_guard() -> AirgapGuard | None:
    return _CURRENT


def is_engaged() -> bool:
    return _CURRENT is not None and _CURRENT._engaged


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child analysers that discourages outbound calls.

    Proxy variables are pointed at a closed port and common telemetry opt-outs
    are set. This is belt-and-braces next to each tool's own offline flags: it
    will not stop a determined binary, but it stops the accidental case, which
    is the one that actually happens.
    """
    import os

    env = dict(base if base is not None else os.environ)
    env.update(
        {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "SEMGREP_SEND_METRICS": "off",
            "SEMGREP_ENABLE_VERSION_CHECK": "0",
            "DO_NOT_TRACK": "1",
            "DISABLE_TELEMETRY": "1",
            "SCARF_ANALYTICS": "false",
            "NEXT_TELEMETRY_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    # A stale token in the environment is the classic way an "air-gapped" tool
    # ends up authenticating to something.
    for leaky in ("GITHUB_TOKEN", "GH_TOKEN", "SEMGREP_APP_TOKEN", "OPENAI_API_KEY"):
        env.pop(leaky, None)
    return env
