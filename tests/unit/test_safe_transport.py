from typing import Any, cast

import httpcore
import pytest

from app.integrations.web.safe_transport import BlockedAddressError, SafeNetworkBackend, is_public_ip


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("2a00:1450:4010:c05::71", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.0.10", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 — проверяем, что адрес считается внутренним
        ("::1", False),
        ("fe80::1%en0", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:93.184.216.34", True),
        ("224.0.0.1", False),
    ],
)
def test_public_ip_classification(ip: str, public: bool) -> None:
    assert is_public_ip(ip) is public


def resolver(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


class RecordingBackend:
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, **_: Any) -> str:
        self.connected.append((host, port))
        return "stream"


async def test_dns_pointing_to_internal_address_is_blocked() -> None:
    backend = SafeNetworkBackend(resolver("127.0.0.1"))
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("evil.example", 443)


async def test_any_internal_address_among_answers_blocks_connection() -> None:
    backend = SafeNetworkBackend(resolver("93.184.216.34", "10.0.0.1"))
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("mixed.example", 443)


async def test_non_standard_port_is_blocked() -> None:
    backend = SafeNetworkBackend(resolver("93.184.216.34"))
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("example.com", 8080)


async def test_connection_goes_to_the_verified_ip_not_the_hostname() -> None:
    backend = SafeNetworkBackend(resolver("93.184.216.34"))
    recording = RecordingBackend()
    backend._inner = recording  # type: ignore[assignment]

    stream = await backend.connect_tcp("example.com", 443)

    assert cast(object, stream) == "stream"
    assert recording.connected == [("93.184.216.34", 443)]  # защита от DNS rebinding


async def test_unresolvable_host_is_a_connect_error() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("nodename nor servname provided")

    with pytest.raises(httpcore.ConnectError):
        await SafeNetworkBackend(failing).connect_tcp("no-such-host.example", 443)
