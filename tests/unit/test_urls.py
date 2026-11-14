import pytest

from app.core.errors import DomainValidationError
from app.integrations.web.urls import normalize_site_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("coffee.ru", "https://coffee.ru/"),
        ("HTTP://Coffee.RU/menu#top", "http://coffee.ru/menu"),
        ("https://coffee.ru:443/about?lang=ru", "https://coffee.ru:443/about?lang=ru"),
        ("кофе.рф", "https://xn--e1ajmy.xn--p1ai/"),
        ("https://www.example.com.", "https://www.example.com/"),
    ],
)
def test_public_urls_are_normalised(raw: str, expected: str) -> None:
    assert normalize_site_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://coffee.ru",
        "https://user:pass@coffee.ru",
        "https://coffee.ru:8080",
        "http://localhost",
        "http://127.0.0.1",
        "http://10.0.0.5/admin",
        "http://192.168.1.1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://intranet",
        "http://printer.local",
        "http://0.0.0.0",
    ],
)
def test_internal_or_unsupported_urls_are_rejected(raw: str) -> None:
    with pytest.raises(DomainValidationError):
        normalize_site_url(raw)
