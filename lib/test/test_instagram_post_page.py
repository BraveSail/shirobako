"""Public image posts can be read from the existing bootstrap response."""

import asyncio
import json
import traceback

import pytest
from _fakes import FakeResponse

from parsehub import ParseHub
from parsehub.errors import ParseError
from parsehub.provider_api.instagram import InstagramAPI, InstagramAPIError
from parsehub.utils.http import AsyncClient

CODE = "Example_abc"
PERMALINK = f"https://www.instagram.com/p/{CODE}/"
IMAGE = "https://scontent.example.cdninstagram.com/photo.jpg"
FALLBACK_IMAGE = "https://scontent.example.cdninstagram.com/from-api.jpg"
THUMBNAIL = {"url": IMAGE + "?stp=dst-webp_s320x320", "width": 320, "height": 320}


def _image(**overrides):
    return {
        "code": CODE,
        "__typename": "XIGPolarisImageMedia",
        "media_type": 1,
        "caption": {"text": "原帖正文"},
        "user": {"username": "example", "full_name": "作者"},
        "image_versions2": {"candidates": [{"url": IMAGE}, {"url": "https://example.com/small.jpg"}]},
        "display_uri": "https://example.com/preview.jpg",
        "original_width": 1440,
        "original_height": 1920,
        "taken_at": 1_750_000_000,
        "like_count": 0,
        **overrides,
    }


def _post_page(media, *, code=CODE):
    post = {"code": code, "if_not_gated_logged_out": media}
    bbox = {"__bbox": {"result": {"data": {"xig_polaris_media": post}}}}
    relay = {"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [], ["query", bbox]]]}}
    script = {"require": [["ScheduledServerJS", "handle", [], [relay]]]}
    return '<script type="application/json" data-sjs>{}</script>'.format(json.dumps(script))


@pytest.fixture
def transport(monkeypatch):
    def install(page, *, page_status=200, page_has_csrf=True):
        calls = []

        async def get(client, url, **kwargs):
            calls.append(("GET", url))
            if url == PERMALINK:
                assert not list(client.cookies.jar)
                assert "content-type" not in client.headers
                assert "referer" not in client.headers
            if url == PERMALINK and not page_has_csrf:
                return FakeResponse(status_code=page_status, text=page)
            client.cookies.set("csrftoken", "bootstrap-test-value", domain="www.instagram.com")
            return FakeResponse(text=page)

        async def post(client, url, *, data, **kwargs):
            calls.append(("POST", url))
            assert client.headers["content-type"] == "application/x-www-form-urlencoded"
            assert client.headers["referer"] == InstagramAPI.INSTAGRAM_URL
            assert client.cookies.get("ig_pr") == InstagramAPI.DEFAULT_COOKIES["ig_pr"]
            expected_csrf = "configured-test-value" if client.cookies.get("sessionid") else "bootstrap-test-value"
            assert client.headers["x-csrftoken"] == expected_csrf
            assert json.loads(data["variables"])["shortcode"] == CODE
            return FakeResponse(json_data={"data": {"xdt_shortcode_media": {
                "shortcode": CODE,
                "__typename": "XDTGraphImage",
                "is_video": False,
                "display_url": FALLBACK_IMAGE,
            }}})

        monkeypatch.setattr(AsyncClient, "get", get)
        monkeypatch.setattr(AsyncClient, "post", post)
        return calls

    return install


def test_public_post_uses_one_request_for_body_and_full_size_photo(transport):
    calls = transport(_post_page(_image()))
    result = asyncio.run(ParseHub().parse(PERMALINK + "?gwpx=example", cookie={"sessionid": "unused-by-public-path"}))
    assert calls == [("GET", PERMALINK)]
    assert result.raw_url == PERMALINK
    assert result.content == "原帖正文"
    assert result.author_name == "作者"
    assert int(result.published_at.timestamp()) == 1_750_000_000
    assert result.like_count == 0 and result.view_count is None
    assert [(photo.url, photo.width, photo.height) for photo in result.media] == [(IMAGE, 1440, 1920)]


def test_missing_like_count_is_not_invented(transport):
    transport(_post_page(_image(like_count=None)))
    result = asyncio.run(ParseHub().parse(PERMALINK))
    assert result.media[0].url == IMAGE
    assert result.like_count is None


def test_explicit_full_size_candidate_wins_over_thumbnail(transport):
    original = {"url": IMAGE, "width": 1440, "height": 1920}
    transport(_post_page(_image(image_versions2={"candidates": [THUMBNAIL, original]})))
    result = asyncio.run(ParseHub().parse(PERMALINK))
    assert [(p.url, p.width, p.height) for p in result.media] == [(IMAGE, 1440, 1920)]


def test_image_conversion_ignores_unexpected_non_image_fields(transport):
    calls = transport(_post_page(_image(video_versions={"invalid": True})))
    result = asyncio.run(ParseHub().parse(PERMALINK))
    assert result.media[0].url == IMAGE
    assert calls == [("GET", PERMALINK)]


@pytest.mark.parametrize("page", [
    pytest.param('<script type="application/json" data-sjs>{broken</script>', id="bad-json"),
    pytest.param('<script type="application/json" data-sjs>{"require":null}</script>', id="bad-envelope"),
    pytest.param(_post_page(_image(), code="Other"), id="unrelated-post"),
    pytest.param(_post_page(_image(code="Other")), id="unrelated-image"),
    pytest.param(_post_page(_image(media_type=8, __typename="XIGPolarisCarouselMedia")), id="carousel-cover"),
    pytest.param(_post_page(_image(media_type=2, __typename="XIGPolarisVideoMedia")), id="video-cover"),
    pytest.param(_post_page(_image(image_versions2={"candidates": []})), id="no-image"),
    pytest.param(_post_page(_image(original_width=None)), id="no-width"),
    pytest.param(_post_page(_image(image_versions2={"candidates": [THUMBNAIL]})), id="small-image"),
    pytest.param(
        _post_page(_image(image_versions2={"candidates": [{"url": THUMBNAIL["url"]}]})),
        id="dimensionless-transformed-image",
    ),
    pytest.param(
        _post_page(_image(image_versions2={"candidates": [{"url": "https://example.org/photo.jpg"}]})),
        id="other-origin",
    ),
    pytest.param(_post_page(None), id="no-public-content"),
])
def test_missing_complete_target_media_keeps_graphql_path(transport, page):
    calls = transport(page)
    result = asyncio.run(ParseHub().parse(PERMALINK))
    assert result.media[0].url == FALLBACK_IMAGE
    assert calls == [("GET", PERMALINK), ("POST", InstagramAPI.GRAPHQL_URL)]


def test_reel_keeps_the_original_bootstrap(transport):
    calls = transport(_post_page(_image()))
    result = asyncio.run(ParseHub().parse(f"https://www.instagram.com/reel/{CODE}/"))
    assert result.media[0].url == FALLBACK_IMAGE
    assert calls == [("GET", InstagramAPI.INSTAGRAM_URL), ("POST", InstagramAPI.GRAPHQL_URL)]


def test_failed_public_page_without_csrf_keeps_original_fallback(transport):
    calls = transport(_post_page(_image()), page_status=403, page_has_csrf=False)
    result = asyncio.run(ParseHub().parse(PERMALINK))
    assert result.media[0].url == FALLBACK_IMAGE
    assert calls == [
        ("GET", PERMALINK), ("GET", InstagramAPI.INSTAGRAM_URL), ("POST", InstagramAPI.GRAPHQL_URL),
    ]


def test_authenticated_api_does_not_fetch_the_public_page(transport):
    calls = transport(_post_page(_image()))
    api = InstagramAPI(cookie={"sessionid": "example", "csrftoken": "configured-test-value"})
    post = asyncio.run(api.get_post(CODE))
    assert post.url == FALLBACK_IMAGE
    assert calls == [("POST", InstagramAPI.GRAPHQL_URL)]


def test_unexpected_authenticated_failure_does_not_expose_credentials(monkeypatch):
    secret = "synthetic-session-for-test"

    async def get_post(api, shortcode, **kwargs):
        if not api.cookie:
            raise InstagramAPIError("Fetching Post metadata failed.")
        raise RuntimeError(f"unexpected failure with {secret}")

    monkeypatch.setattr(InstagramAPI, "get_post", get_post)
    with pytest.raises(ParseError, match="无法获取帖子内容") as exc:
        asyncio.run(ParseHub().parse(PERMALINK, cookie={"sessionid": secret}))
    assert secret not in str(exc.value)
    assert secret not in "".join(traceback.format_exception(exc.value))
