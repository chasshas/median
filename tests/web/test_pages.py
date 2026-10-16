import httpx
import pytest

from app.web.pages import NAV


async def test_dashboard_renders_with_navigation(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "<h1>Dashboard</h1>" in html
    assert 'id="db-status">reachable<' in html
    for path in ["/", "/upload", "/review", "/library", "/search", "/tags", "/sources"]:
        assert f'href="{path}"' in html
    for path in ["/jobs", "/settings"]:
        assert f'href="{path}"' in html
    assert "vendor/htmx-2.0.11.min.js" in html


@pytest.mark.parametrize("item", [i for i in NAV if i.phase is not None], ids=lambda i: i.path)
async def test_unbuilt_pages_show_coming_soon(client: httpx.AsyncClient, item) -> None:
    response = await client.get(item.path)

    assert response.status_code == 200
    assert f"Coming in phase {item.phase}" in response.text
    assert 'aria-current="page"' in response.text


async def test_unknown_file_page_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/files/999999")

    assert response.status_code == 404


async def test_htmx_is_vendored(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/vendor/htmx-2.0.11.min.js")

    assert response.status_code == 200
    assert 'version:"2.0.11"' in response.text
