"""Wikimedia Commons 后备素材源：许可证过滤、结果解析、触发条件与署名清单。"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import material, wikimedia_commons

UPLOAD = "https://upload.wikimedia.org/wikipedia/commons"


@pytest.mark.parametrize(
    ("short_name", "template", "expected"),
    [
        ("CC0", "cc0", "CC0"),
        ("Public domain", "pd", "Public domain"),
        ("Public domain", "", "Public domain"),
        ("PD-USGov", "pd-usgov", "Public domain"),
        ("CC BY 4.0", "cc-by-4.0", "CC BY 4.0"),
        ("CC BY 2.0", "cc-by-2.0", "CC BY 2.0"),
        ("CC BY 3.0 us", "cc-by-3.0-us", "CC BY 3.0"),
        ("CC BY", "", "CC BY"),
        ("", "cc-by-2.5", "CC BY 2.5"),
    ],
)
def test_license_filter_accepts_public_domain_cc0_and_cc_by(short_name, template, expected):
    assert wikimedia_commons.classify_license(short_name, template) == expected


@pytest.mark.parametrize(
    ("short_name", "template", "non_free"),
    [
        ("CC BY-SA 4.0", "cc-by-sa-4.0", ""),
        ("CC BY-SA 3.0", "", ""),
        ("", "cc-by-sa-3.0-migrated", ""),
        ("CC BY-NC 4.0", "cc-by-nc-4.0", ""),
        ("CC BY-ND 2.0", "cc-by-nd-2.0", ""),
        ("CC BY-NC-SA 2.0", "cc-by-nc-sa-2.0", ""),
        ("Fair use", "", ""),
        ("GFDL", "gfdl", ""),
        ("", "", ""),
        ("Some custom license", "", ""),
        ("CC BY 4.0", "cc-by-4.0", "true"),
        # 两个字段矛盾时保守拒绝。
        ("CC BY 4.0", "custom-terms", ""),
    ],
)
def test_license_filter_rejects_share_alike_nc_nd_fair_use_and_unknown(
    short_name, template, non_free
):
    assert wikimedia_commons.classify_license(short_name, template, non_free) is None


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (f"{UPLOAD}/a/ab/Clip.webm", True),
        ("http://upload.wikimedia.org/a.webm", False),
        ("https://upload.wikimedia.org.evil.example/a.webm", False),
        ("https://user:pw@upload.wikimedia.org/a.webm", False),
        ("https://upload.wikimedia.org:8443/a.webm", False),
        ("https://commons.wikimedia.org/wiki/File:A.webm", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("", False),
        (None, False),
    ],
)
def test_only_upload_wikimedia_https_urls_are_allowed(url, allowed):
    assert wikimedia_commons.is_upload_url(url) is allowed


def _page(
    index,
    title,
    *,
    short="CC BY 4.0",
    template="cc-by-4.0",
    duration=12.0,
    width=1920,
    height=1080,
    mime="video/webm",
    url=None,
    size=5_000_000,
    derivatives=None,
    artist='<a href="//commons.wikimedia.org/wiki/User:Jane">Jane Doe</a>',
):
    name = title.replace(" ", "_")
    info = {
        "size": size,
        "width": width,
        "height": height,
        "mime": mime,
        "url": url or f"{UPLOAD}/1/12/{name}?utm_source=commons.wikimedia.org",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
        "extmetadata": {
            "LicenseShortName": {"value": short},
            "License": {"value": template},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0"},
            "Artist": {"value": artist},
            "Credit": {"value": "<span>Own work</span>"},
            "ObjectName": {"value": title.rsplit(".", 1)[0]},
        },
    }
    if duration is not None:
        info["duration"] = duration
    if derivatives is not None:
        info["derivatives"] = derivatives
    return {"pageid": 1000 + index, "index": index, "title": f"File:{title}", "videoinfo": [info]}


def _payload(*pages):
    return {"batchcomplete": True, "query": {"pages": list(pages)}}


def _parse(payload, minimum_duration=5, aspect=VideoAspect.landscape, max_bytes=512 * 1024 * 1024):
    return wikimedia_commons.parse_search_response(
        payload, "ocean", minimum_duration, aspect, max_bytes
    )


def test_parse_keeps_only_allowed_licenses_and_records_attribution():
    payload = _payload(
        _page(1, "Ocean waves.webm"),
        _page(2, "Share alike.webm", short="CC BY-SA 4.0", template="cc-by-sa-4.0"),
        _page(3, "Noncommercial.webm", short="CC BY-NC 4.0", template="cc-by-nc-4.0"),
        _page(4, "Unknown.webm", short="", template=""),
        _page(5, "Zero.webm", short="CC0", template="cc0"),
    )

    items = _parse(payload)

    assert [item.source_info["title"] for item in items] == ["Ocean waves", "Zero"]
    first = items[0]
    assert first.provider == "wikimedia"
    assert first.url == f"{UPLOAD}/1/12/Ocean_waves.webm"  # 去掉 utm 参数
    assert first.duration == 12
    assert first.source_info["license"] == "CC BY 4.0"
    assert first.source_info["creator"] == {"name": "Jane Doe"}
    assert first.source_info["source_page"].endswith("File:Ocean_waves.webm")
    assert first.source_info["credit"] == "Own work"
    assert items[1].source_info["license"] == "CC0"


def test_parse_skips_stills_short_oversized_offsite_and_wrong_aspect_files():
    payload = _payload(
        _page(1, "Still.jpg", mime="image/jpeg", duration=None),
        _page(2, "Too short.webm", duration=2.0),
        _page(3, "Huge.webm", size=600 * 1024 * 1024),
        _page(4, "Offsite.webm", url="https://evil.example/Offsite.webm"),
        _page(5, "Portrait.webm", width=1080, height=1920),
        _page(6, "Tiny.webm", width=640, height=360),
        _page(7, "Good.webm"),
    )

    items = _parse(payload, minimum_duration=5)

    assert [item.source_info["title"] for item in items] == ["Good"]


def test_parse_prefers_smallest_transcode_that_fills_canvas():
    derivatives = [
        {"src": f"{UPLOAD}/a/ab/Big.webm", "type": 'video/webm; codecs="vp9"',
         "width": 3840, "height": 2160, "bandwidth": 20_000_000},
        {"src": f"{UPLOAD}/transcoded/a/ab/Big.webm/Big.webm.1080p.vp9.webm",
         "type": 'video/webm; codecs="vp9"', "transcodekey": "1080p.vp9.webm",
         "width": 1920, "height": 1080, "bandwidth": 4_000_000},
        {"src": f"{UPLOAD}/transcoded/a/ab/Big.webm/Big.webm.720p.vp9.webm",
         "type": 'video/webm; codecs="vp9"', "transcodekey": "720p.vp9.webm",
         "width": 1280, "height": 720, "bandwidth": 2_000_000},
    ]
    payload = _payload(
        _page(1, "Big.webm", width=3840, height=2160, size=400_000_000, derivatives=derivatives)
    )

    (item,) = _parse(payload)

    assert item.url.endswith("Big.webm.1080p.vp9.webm")
    assert item.source_info["rendition"] == {"id": "1080p.vp9.webm", "width": 1920, "height": 1080}


def test_search_sends_descriptive_user_agent_and_disables_redirects():
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return _payload(_page(1, "Ocean waves.webm"))

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    items = wikimedia_commons.search_videos(
        "ocean", 5, VideoAspect.landscape, http_get=fake_get, max_bytes=10**9
    )

    assert len(items) == 1
    url, kwargs = calls[0]
    assert url == "https://commons.wikimedia.org/w/api.php"
    assert "MoneyPrinterTurbo" in kwargs["headers"]["User-Agent"]
    assert "Mozilla" not in kwargs["headers"]["User-Agent"]
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"]["gsrsearch"] == "ocean filetype:video"
    assert kwargs["params"]["gsrnamespace"] == "6"


def _item(provider, url, license_name=None):
    source = {"provider": provider, "asset_id": url.rsplit("/", 1)[-1]}
    if license_name:
        source.update(title="Clip", license=license_name)
    return MaterialInfo(provider=provider, url=url, duration=10, source_info=source)


@pytest.fixture
def fallback_items():
    return [
        _item("wikimedia", f"{UPLOAD}/a/aa/Good.webm", "CC BY 4.0"),
        # 缓存被篡改或来自旧版本时，仍要拒绝站外地址和缺失许可证的条目。
        _item("wikimedia", "https://evil.example/Bad.webm", "CC0"),
        _item("wikimedia", f"{UPLOAD}/b/bb/NoLicense.webm"),
        _item("wikimedia", f"{UPLOAD}/c/cc/SA.webm", "CC BY-SA 4.0"),
    ]


def test_fallback_triggers_when_primary_returns_too_few(fallback_items):
    primary = [_item("pexels", "https://videos.pexels.com/1.mp4")]
    with (
        patch.dict(config.app, {"enable_wikimedia_fallback": True,
                                "wikimedia_fallback_min_results": 3}),
        patch.object(material, "_search_videos_with_cache", return_value=fallback_items) as search,
    ):
        result = material._with_wikimedia_fallback(
            "pexels", primary, "ocean", 5, VideoAspect.portrait
        )

    assert [item.url for item in result] == [
        "https://videos.pexels.com/1.mp4",
        f"{UPLOAD}/a/aa/Good.webm",
    ]
    assert search.call_args.kwargs["provider"] == "wikimedia"
    assert search.call_args.kwargs["search_videos"] is material.search_videos_wikimedia


@pytest.mark.parametrize(
    ("provider", "primary_count", "settings"),
    [
        ("pexels", 3, {"enable_wikimedia_fallback": True, "wikimedia_fallback_min_results": 3}),
        ("pixabay", 1, {"enable_wikimedia_fallback": False}),
        ("pixabay", 1, {"enable_wikimedia_fallback": "false"}),
        ("coverr", 0, {"enable_wikimedia_fallback": True}),
    ],
)
def test_fallback_not_triggered(provider, primary_count, settings):
    primary = [_item(provider, f"https://cdn.example/{i}.mp4") for i in range(primary_count)]
    with (
        patch.dict(config.app, settings),
        patch.object(material, "_search_videos_with_cache") as search,
    ):
        result = material._with_wikimedia_fallback(
            provider, primary, "ocean", 5, VideoAspect.portrait
        )

    assert result == primary
    search.assert_not_called()


def test_fallback_is_enabled_by_default():
    with patch.dict(config.app, {}, clear=False):
        config.app.pop("enable_wikimedia_fallback", None)
        assert material._wikimedia_fallback_enabled() is True


def test_fallback_failure_keeps_primary_results():
    primary = [_item("pixabay", "https://cdn.pixabay.com/1.mp4")]
    with (
        patch.dict(config.app, {"enable_wikimedia_fallback": True}),
        patch.object(material, "_search_videos_with_cache", side_effect=RuntimeError("down")),
    ):
        result = material._with_wikimedia_fallback(
            "pixabay", primary, "ocean", 5, VideoAspect.portrait
        )

    assert result == primary


def test_download_videos_uses_fallback_and_writes_credits(tmp_path):
    commons = MaterialInfo(
        provider="wikimedia",
        url=f"{UPLOAD}/5/5d/Ocean_in_Sommaroy.webm",
        duration=15,
        source_info={
            "provider": "wikimedia",
            "asset_id": "81916645",
            "source_page": "https://commons.wikimedia.org/wiki/File:Ocean_in_Sommaroy.webm",
            "creator": {"name": "User: Netha Hussain"},
            "title": "Ocean in Sommaroy",
            "license": "CC0",
            "license_url": "http://creativecommons.org/publicdomain/zero/1.0/deed.en",
            "rendition": {"id": "original", "width": 1920, "height": 1080},
        },
    )
    saved_records = {}

    def fake_patch_script_data(task_id, **updates):
        saved_records.update(updates)
        return True

    with (
        patch.dict(config.app, {"material_directory": "", "enable_wikimedia_fallback": True}),
        patch.object(material, "search_videos_pexels", return_value=[]),
        patch.object(material, "search_videos_wikimedia", return_value=[commons]) as wiki,
        patch.object(material, "save_video", return_value=str(tmp_path / "vid-1.mp4")),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data",
                     side_effect=fake_patch_script_data),
        patch.object(material.utils, "task_dir", return_value=str(tmp_path)),
    ):
        result = material.download_videos(
            task_id="wikimedia-task",
            search_terms=["ocean"],
            source="pexels",
            video_aspect=VideoAspect.landscape,
            audio_duration=5,
            max_clip_duration=5,
        )

    assert result == [str(tmp_path / "vid-1.mp4")]
    wiki.assert_called_once()
    (record,) = saved_records["material_sources"]
    assert record["provider"] == "wikimedia"
    assert record["license"] == "CC0"
    assert record["title"] == "Ocean in Sommaroy"
    credits = (tmp_path / "credits.txt").read_text(encoding="utf-8")
    assert '"Ocean in Sommaroy" by User: Netha Hussain | CC0' in credits
    assert "https://commons.wikimedia.org/wiki/File:Ocean_in_Sommaroy.webm" in credits


def test_read_material_credits_from_task_manifest(tmp_path):
    task_dir = tmp_path / "storage" / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "script.json").write_text(
        json.dumps({"material_sources": [
            {"provider": "pexels", "local_file": "a.mp4"},
            {"provider": "wikimedia", "title": "Clip", "license": "CC BY 4.0",
             "creator": {"name": "Jane"},
             "source_page": "https://commons.wikimedia.org/wiki/File:Clip.webm"},
        ]}),
        encoding="utf-8",
    )
    with patch.object(material.utils, "storage_dir", return_value=str(tmp_path / "storage")):
        lines = material.read_material_credits("t1")

    assert lines == [
        '"Clip" by Jane | CC BY 4.0 | via Wikimedia Commons: '
        "https://commons.wikimedia.org/wiki/File:Clip.webm"
    ]


def test_cache_round_trip_keeps_license_fields():
    item = _item("wikimedia", f"{UPLOAD}/a/aa/Good.webm", "CC BY 4.0")
    item.source_info["license_url"] = "https://creativecommons.org/licenses/by/4.0?x=1"
    cached = material.material_cache._cached_source_info(item)
    assert cached["license"] == "CC BY 4.0"
    assert cached["title"] == "Clip"
    assert cached["license_url"] == "https://creativecommons.org/licenses/by/4.0"
