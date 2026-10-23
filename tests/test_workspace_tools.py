import httpx
import pytest

from app.config import Settings
from app.server import create_app
from app.workspace_tools import tools_configuration


def test_standalone_companion_does_not_advertise_missing_tools():
    assert not tools_configuration({})["clipping"]["enabled"]


def test_only_public_navigation_values_are_returned():
    values = {"CLIPPER_ENABLED": "1", "CLIPPER_PORT": "8766", "MODEL_API_KEY": "private-example",
              "BILIBILI_COOKIE": "private-cookie", "CLIPPER_DATA_DIR": "/private/path"}
    assert tools_configuration(values) == {"clipping": {"enabled": True, "port": 8766, "public_url": ""}}


def test_root_proxy_and_ipv6_public_urls_are_kept():
    for url in ["https://clips.example.org:8766", "https://clips.example.org/", "http://[::1]:8766"]:
        value = tools_configuration({"CLIPPER_ENABLED": "1", "CLIPPER_PUBLIC_URL": url})["clipping"]
        assert value["enabled"] is True
        assert value["public_url"] == url.rstrip("/") + "/"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//example.org", "http://name:password@example.org",
                                  "https://example.org/?token=secret", "https://example.org/#token", "http://example.org:invalid",
                                  "https://example.org/xx/", "https://example.org/proxy/8766", "https://example.org//"])
def test_invalid_or_credential_bearing_navigation_is_disabled(url):
    value = tools_configuration({"CLIPPER_ENABLED": "1", "CLIPPER_PUBLIC_URL": url})
    assert value["clipping"]["enabled"] is False
    assert value["clipping"]["public_url"] == ""


@pytest.mark.asyncio
async def test_tools_endpoint_does_not_start_a_session_or_change_send_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_ENABLED", "1")
    monkeypatch.setenv("CLIPPER_PORT", "8766")
    monkeypatch.setenv("CLIPPER_PUBLIC_URL", "https://clips.example.org:8766/")
    app = create_app(Settings(tmp_path, "ffmpeg"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            before = (await client.get("/api/state")).json()
            response = await client.get("/api/tools")
            after = (await client.get("/api/state")).json()
        assert response.json() == {"clipping": {"enabled": True, "port": 8766, "public_url": "https://clips.example.org:8766/"}}
        assert after["session"] == before["session"]
        assert after["danmaku_send"] == before["danmaku_send"]
    finally:
        await app.state.workspace.close()
