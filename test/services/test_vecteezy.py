"""Vecteezy stock video source and Coverr attribution: mocked HTTP only."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import material, material_cache, vecteezy

FAKE_KEY = "vz-test-key-0123456789"
SIGNED_URL = "https://files.vecteezy.com/signed/abc.mp4?Expires=1&Signature=xyz"


def _response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _account_info(general=(0, 1000), download=(0, 500)):
    return {
        "current": {
            "general": {"call_count": general[0], "call_limit": general[1], "hard_limit": 0},
            "download": {"call_count": download[0], "call_limit": download[1], "hard_limit": 0},
        }
    }


def _resource(rid, title, *, width=None, height=None, tags=None, content_type="video",
              extensions=("mp4",)):
    item = {
        "id": rid,
        "content_type": content_type,
        "title": title,
        "file_metadata": {
            "available_file_types": [{"extension": ext, "size_in_bytes": 1} for ext in extensions],
            "available_download_sizes": [],
        },
    }
    if width and height:
        item["dimensions"] = {"width": width, "height": height}
    if tags is not None:
        item["tags"] = tags
    return item


SEARCH_PAYLOAD = {
    "page": 1,
    "last_page": 1,
    "per_page": 50,
    "total_resources": 6,
    "resources": [
        _resource(1, "Driveway crack repair", width=1080, height=1920),
        _resource(2, "Wide driveway", width=1920, height=1080),          # wrong aspect
        _resource(3, "Asphalt sealing"),                                 # free tier: no dims
        _resource(4, "Car crash on highway", width=1080, height=1920),   # blocked title
        _resource(5, "Road work", width=1080, height=1920, tags=["corpse"]),  # blocked tag
        _resource(6, "Logo pack", content_type="vector"),
        _resource(7, "AE template", extensions=("aep",)),
    ],
}


@pytest.fixture
def vz_config():
    with patch.dict(config.app, {
        "vecteezy_api_keys": [FAKE_KEY],
        "vecteezy_account_id": "",
        "vecteezy_allow_paid_downloads": False,
    }):
        yield


def _route(responses):
    """Fake requests.get that answers by URL suffix and records calls."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                return payload if isinstance(payload, MagicMock) else _response(payload)
        raise AssertionError(f"unexpected url {url}")

    return fake_get, calls


# --- parsing ---------------------------------------------------------------

def test_parse_filters_type_aspect_blocklist_and_keeps_unknown_dims():
    content_filter = material._wikimedia_content_filter()
    candidates = vecteezy.parse_search_response(
        SEARCH_PAYLOAD, "driveway", 5,
        lambda w, h: material._matches_video_aspect(w, h, VideoAspect.portrait),
        content_filter,
    )
    assert [c["resource_id"] for c in candidates] == [1, 3]
    assert candidates[0]["width"] == 1080 and candidates[0]["height"] == 1920
    assert candidates[1]["width"] is None  # orientation verified after download
    assert all(c["duration"] == 5 for c in candidates)


def test_dimensions_fall_back_to_largest_download_size():
    resource = {"file_sizes": [{"id": 1, "width": 640, "height": 360},
                               {"id": 2, "width": 1920, "height": 1080}]}
    assert vecteezy.resource_dimensions(resource) == (1920, 1080)


def test_search_params_request_commercial_family_friendly_videos():
    params = vecteezy.build_search_params("roof", 4)
    assert params["content_type"] == "video"
    assert params["license_type"] == "commercial"
    assert params["family_friendly"] == "true"
    assert params["duration"] == "4_3600"


def test_api_prefix_v1_without_account_and_v2_with_numeric_account():
    assert vecteezy.api_prefix("") == "https://api.vecteezy.com/v1"
    assert vecteezy.api_prefix("123") == "https://api.vecteezy.com/v2/123"
    with pytest.raises(ValueError):
        vecteezy.api_prefix("../evil")


# --- search ----------------------------------------------------------------

def test_search_uses_bearer_key_quota_check_and_returns_references(vz_config):
    fake_get, calls = _route({"/account/info": _account_info(), "/resources": SEARCH_PAYLOAD})
    with (
        patch.dict(config.app, {"vecteezy_account_id": "42"}),
        patch.object(material.requests, "get", side_effect=fake_get),
    ):
        items = material.search_videos_vecteezy("driveway", 5, VideoAspect.portrait)

    assert [c[0] for c in calls] == [
        "https://api.vecteezy.com/v2/42/account/info",
        "https://api.vecteezy.com/v2/42/resources",
    ]
    for _, kwargs in calls:
        assert kwargs["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert FAKE_KEY not in str(kwargs.get("params"))
    assert calls[1][1]["params"]["content_type"] == "video"
    assert [i.url for i in items] == ["vecteezy:resource/1", "vecteezy:resource/3"]
    assert all(i.provider == "vecteezy" for i in items)
    assert items[0].source_info["title"] == "Driveway crack repair"
    assert items[0].source_info["target_aspect"] == VideoAspect.portrait.value


def test_free_plan_unmetered_search_calls_are_allowed():
    # Live free accounts report general.call_limit == 0 (calls are not capped).
    vecteezy.ensure_quota(lambda: _account_info(general=(7, 0)), "general", False)
    with pytest.raises(vecteezy.VecteezyQuotaError):
        vecteezy.ensure_quota(lambda: _account_info(download=(0, 0)), "download", False)


def test_search_is_refused_when_free_general_allowance_is_used(vz_config):
    fake_get, calls = _route({"/account/info": _account_info(general=(1000, 1000))})
    with patch.object(material.requests, "get", side_effect=fake_get):
        assert material.search_videos_vecteezy("driveway", 5, VideoAspect.portrait) == []
    assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["info"]


def test_search_errors_never_log_the_key(vz_config):
    messages = []
    sink = material.logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        with patch.object(
            material.requests, "get",
            side_effect=ConnectionError(f"boom Authorization: Bearer {FAKE_KEY}"),
        ):
            with patch.dict(config.app, {"vecteezy_allow_paid_downloads": True}):
                assert material.search_videos_vecteezy("x", 5, VideoAspect.portrait) == []
    finally:
        material.logger.remove(sink)
    assert messages and not any(FAKE_KEY in m for m in messages)


def test_http_401_and_402_are_reported_without_body(vz_config):
    with patch.object(material.requests, "get", return_value=_response({}, 401)):
        with pytest.raises(PermissionError):
            material._vecteezy_get_json("https://api.vecteezy.com/v1/resources", FAKE_KEY)
    with patch.object(material.requests, "get", return_value=_response({}, 402)):
        with pytest.raises(vecteezy.VecteezyQuotaError):
            material._vecteezy_get_json("https://api.vecteezy.com/v1/resources", FAKE_KEY)


# --- paid guard --------------------------------------------------------------

def test_quota_guard_fails_closed_and_can_be_overridden():
    def broken():
        raise RuntimeError("network")

    with pytest.raises(vecteezy.VecteezyQuotaError):
        vecteezy.ensure_quota(broken, "download", allow_paid=False)
    with pytest.raises(vecteezy.VecteezyQuotaError):
        vecteezy.ensure_quota(lambda: {"current": {}}, "download", allow_paid=False)
    vecteezy.ensure_quota(lambda: _account_info(download=(10, 500)), "download", False)
    vecteezy.ensure_quota(broken, "download", allow_paid=True)  # no check at all


def test_allow_paid_flag_defaults_off_and_only_accepts_true_values():
    with patch.dict(config.app, {}, clear=False):
        config.app.pop("vecteezy_allow_paid_downloads", None)
        assert material._vecteezy_allow_paid() is False
    for value, expected in ((True, True), ("true", True), ("no", False), (1, False), (False, False)):
        with patch.dict(config.app, {"vecteezy_allow_paid_downloads": value}):
            assert material._vecteezy_allow_paid() is expected


# --- lazy download -----------------------------------------------------------

def _item(rid=1, width=None, height=None, aspect=VideoAspect.portrait):
    item = MaterialInfo()
    item.provider = "vecteezy"
    item.url = vecteezy.reference(rid)
    item.duration = 5
    item.source_info = {
        "provider": "vecteezy", "asset_id": str(rid), "title": "Driveway crack repair",
        "file_type": "mp4", "target_aspect": aspect.value,
        "rendition": {"id": "mp4", "width": width, "height": height},
    }
    return item


def test_download_resolves_signed_url_once_and_records_attribution(vz_config, tmp_path):
    fake_get, calls = _route({
        "/account/info": _account_info(download=(3, 500)),
        "/resources/1/download": {
            "url": SIGNED_URL,
            "requires_attribution": True,
            "required_attribution_url": "https://www.vecteezy.com/free-videos/driveway?x=1",
        },
    })
    item = _item()
    saved = str(tmp_path / "vid.mp4")
    with (
        patch.object(material.requests, "get", side_effect=fake_get),
        patch.object(material, "save_video", return_value=saved) as save,
        patch.object(material, "_probe_video_size", return_value=(1080, 1920, 12.4)),
    ):
        assert material._save_vecteezy_video(item, str(tmp_path)) == saved

    save.assert_called_once_with(SIGNED_URL, str(tmp_path), cache_key="vecteezy:resource/1")
    assert calls[1][1]["params"] == {"file_type": "mp4"}
    assert item.duration == 12
    assert item.url == "vecteezy:resource/1"  # signed URL is never stored
    record = material._material_source_record(item, saved)
    assert record["license"] == vecteezy.FREE_LICENSE
    assert record["credit"] == (
        "Stock Videos by Vecteezy.com: https://www.vecteezy.com/free-videos/driveway"
    )
    assert SIGNED_URL not in str(record)


def test_download_blocked_when_free_download_allowance_is_used(vz_config, tmp_path):
    fake_get, calls = _route({"/account/info": _account_info(download=(500, 500))})
    with (
        patch.object(material.requests, "get", side_effect=fake_get),
        patch.object(material, "save_video") as save,
    ):
        assert material._save_vecteezy_video(_item(), str(tmp_path)) == ""
    save.assert_not_called()
    assert len(calls) == 1 and calls[0][0].endswith("/account/info")


def test_paid_flag_skips_quota_check(vz_config, tmp_path):
    fake_get, calls = _route({"/resources/1/download": {"url": SIGNED_URL, "requires_attribution": False}})
    item = _item()
    with (
        patch.dict(config.app, {"vecteezy_allow_paid_downloads": True}),
        patch.object(material.requests, "get", side_effect=fake_get),
        patch.object(material, "save_video", return_value=str(tmp_path / "v.mp4")),
        patch.object(material, "_probe_video_size", return_value=(1080, 1920, 8)),
    ):
        assert material._save_vecteezy_video(item, str(tmp_path))
    assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["download"]
    assert item.source_info["license"] == vecteezy.PAID_LICENSE
    assert vecteezy.credit_lines([material._material_source_record(item, "v.mp4")]) == []


def test_cached_clip_is_reused_without_api_calls(vz_config, tmp_path):
    item = _item()
    _, cached = material._cached_video_path(item.url, str(tmp_path))
    Path(cached).write_bytes(b"x")
    with patch.object(material.requests, "get") as get:
        assert material._save_vecteezy_video(item, str(tmp_path)) == cached
    get.assert_not_called()
    assert item.source_info["license"] == vecteezy.FREE_LICENSE


def test_wrong_orientation_after_download_is_deleted(vz_config, tmp_path):
    fake_get, _ = _route({
        "/account/info": _account_info(),
        "/resources/1/download": {"url": SIGNED_URL, "requires_attribution": True},
    })
    saved = tmp_path / "vid.mp4"
    saved.write_bytes(b"x")
    with (
        patch.object(material.requests, "get", side_effect=fake_get),
        patch.object(material, "save_video", return_value=str(saved)),
        patch.object(material, "_probe_video_size", return_value=(1920, 1080, 10)),
    ):
        assert material._save_vecteezy_video(_item(), str(tmp_path)) == ""
    assert not saved.exists()


def test_attribution_url_must_be_https_vecteezy():
    assert vecteezy.safe_attribution_url("https://evil.example/x") == vecteezy.DEFAULT_ATTRIBUTION_URL
    assert vecteezy.safe_attribution_url("http://www.vecteezy.com/x") == vecteezy.DEFAULT_ATTRIBUTION_URL
    assert vecteezy.safe_attribution_url("https://www.vecteezy.com/a?b=c") == "https://www.vecteezy.com/a"


# --- pipeline ----------------------------------------------------------------

def test_download_videos_vecteezy_writes_credits(vz_config, tmp_path):
    items = [_item(1), _item(2)]
    fake_get, _ = _route({
        "/account/info": _account_info(),
        "/resources/1/download": {"url": SIGNED_URL, "requires_attribution": True},
        "/resources/2/download": {"url": SIGNED_URL + "2", "requires_attribution": True},
    })
    saved_records = {}

    def fake_save(url, save_dir="", cache_key=None):
        path = tmp_path / f"{cache_key.rsplit('/', 1)[-1]}.mp4"
        path.write_bytes(b"x")
        return str(path)

    with (
        patch.dict(config.app, {"material_directory": "", "enable_wikimedia_fallback": False}),
        patch.object(material, "search_videos_vecteezy", return_value=items),
        patch.object(material.requests, "get", side_effect=fake_get),
        patch.object(material, "save_video", side_effect=fake_save),
        patch.object(material, "_probe_video_size", return_value=(1080, 1920, 6)),
        patch.object(material.task_artifacts, "patch_script_data",
                     side_effect=lambda task_id, **u: saved_records.update(u) or True),
        patch.object(material.utils, "task_dir", return_value=str(tmp_path)),
    ):
        result = material.download_videos(
            task_id="vz-task", search_terms=["driveway"], source="vecteezy",
            video_aspect=VideoAspect.portrait, audio_duration=8, max_clip_duration=5,
        )

    assert len(result) == 2
    assert {r["provider"] for r in saved_records["material_sources"]} == {"vecteezy"}
    credits = (tmp_path / "credits.txt").read_text(encoding="utf-8")
    assert "Video material credits (Vecteezy)" in credits
    assert '"Driveway crack repair" (Vecteezy #1) | Stock Videos by Vecteezy.com' in credits
    assert 'add "Vecteezy.com" to the final credits' in credits
    assert SIGNED_URL not in credits


def test_vecteezy_search_results_are_never_disk_cached(tmp_path):
    with patch.object(material_cache, "_cache_dir", return_value=str(tmp_path), create=True):
        assert material_cache.save_material_search_cache(
            "vecteezy", "driveway", 5, VideoAspect.portrait, [_item()]
        ) is False
    assert "vecteezy" in material_cache.UNCACHED_PROVIDERS
    assert "vecteezy" in material.WIKIMEDIA_FALLBACK_PROVIDERS


def test_source_is_registered_everywhere():
    import cli
    from app.services import task

    assert "vecteezy" in cli._CLI_VIDEO_SOURCES
    assert "vecteezy" in task._SUPPORTED_VIDEO_SOURCES
    args = cli.parse_args(["--video-subject", "t", "--video-source", "vecteezy"])
    assert cli.build_video_params(args).video_source == "vecteezy"


# --- Coverr --------------------------------------------------------------------

def test_coverr_search_uses_bearer_key_and_builds_page_url():
    payload = {"hits": [{
        "id": "abc", "title": "Waves", "duration": "8.5", "max_width": 1080,
        "max_height": 1920, "is_vertical": True, "slug": "waves-abc",
        "urls": {"mp4_download": "https://cdn.coverr.co/videos/abc/download?token=t"},
    }]}
    with (
        patch.dict(config.app, {"coverr_api_keys": ["cv-key"]}),
        patch.object(material.requests, "get", return_value=_response(payload)) as get,
    ):
        items = material.search_videos_coverr("waves", 5, VideoAspect.portrait)

    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer cv-key"}
    assert "cv-key" not in get.call_args.args[0]
    assert "filter=is_vertical%3Atrue" in get.call_args.args[0]
    (item,) = items
    assert item.source_info["source_page"] == "https://coverr.co/videos/waves-abc"
    assert item.source_info["title"] == "Waves"


def test_coverr_clips_get_credit_lines():
    records = [
        {"provider": "coverr", "asset_id": "abc", "title": "Waves",
         "source_page": "https://coverr.co/videos/waves-abc"},
        {"provider": "pexels", "asset_id": "1"},
    ]
    assert material._material_credit_lines(records) == [
        '"Waves" | Video from Coverr: https://coverr.co | https://coverr.co/videos/waves-abc'
    ]
    assert material._render_material_credits(records).startswith("Video material credits (Coverr)")


def test_api_host_attribution_url_is_rewritten_to_public_site():
    # Observed live: required_attribution_url uses api.vecteezy.com (404 there).
    assert (
        vecteezy.safe_attribution_url("https://api.vecteezy.com/free-videos/surface?x=1")
        == "https://www.vecteezy.com/free-videos/surface"
    )
