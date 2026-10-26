"""
Wikimedia Commons 作为库存素材的后备来源。

只在 Pexels/Pixabay 对某个关键词返回的候选过少时使用（见
``material.download_videos``）。这里负责三件事：

1. 调用公开的 MediaWiki API（无需 Key）按关键词搜索视频文件；
2. 严格按许可证白名单过滤：仅接受公有领域、CC0 与 CC BY（任意版本），
   拒绝 CC BY-SA、NC、ND、合理使用以及无法识别的许可证；
3. 只返回 ``https://upload.wikimedia.org`` 上由 API 给出的下载地址，避免
   把任意第三方 URL 交给下载器（SSRF 防护）。

CC BY 要求署名，因此每条结果都携带标题、作者、许可证与来源页，供
``material`` 写入任务清单和 ``credits.txt``。
"""

from __future__ import annotations

import html
import math
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from app.models.schema import MaterialInfo, VideoAspect

PROVIDER = "wikimedia"
API_URL = "https://commons.wikimedia.org/w/api.php"
UPLOAD_HOST = "upload.wikimedia.org"
# Wikimedia 的 User-Agent 政策要求可识别的客户端名称和联系方式。
USER_AGENT = (
    "MoneyPrinterTurbo/1.0 "
    "(https://github.com/harry0703/MoneyPrinterTurbo; stock-footage fallback) "
    "python-requests"
)
SEARCH_LIMIT = 20
# 原生竖屏素材在 Commons 很少见。优先选择能填满输出画布的最小清晰度；
# 没有时退而求其次，接受短边不低于 720 像素且方向一致的最大清晰度，
# 由视频合成阶段缩放，避免竖屏任务的后备来源几乎总是为空。
MIN_SHORT_SIDE = 720
VIDEO_MIME_PREFIXES = ("video/webm", "video/ogg", "video/mp4")
CREDITS_FILENAME = "credits.txt"

_DENY_TOKENS = re.compile(
    r"(?:^|[^a-z])(sa|nc|nd|share[- ]?alike|non[- ]?commercial|no[- ]?deriv\w*|"
    r"fair[- ]?use|non[- ]?free|gfdl|gpl|copyrighted free use|all rights reserved)"
    r"(?:$|[^a-z])"
)
_CC_BY_SHORT = re.compile(r"^cc[- ]by(?:[- ]\d(?:\.\d)?)?(?:[- ][a-z]{2,}(?:[- ][a-z]{2,})?)?$")
_CC_BY_TEMPLATE = re.compile(r"^cc-by(?:-\d(?:\.\d)?)?(?:-[a-z]{2,})*$")


def _meta_value(extmetadata: Any, key: str) -> str:
    if not isinstance(extmetadata, dict):
        return ""
    entry = extmetadata.get(key)
    if isinstance(entry, dict):
        entry = entry.get("value")
    if entry is None or isinstance(entry, (dict, list)):
        return ""
    return str(entry)


def strip_html(value: str) -> str:
    """Artist/Credit 字段是 HTML 片段，只保留可读文本。"""
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def classify_license(short_name: str, template: str = "", non_free: str = "") -> str | None:
    """
    返回规范化的许可证名称；不在白名单内时返回 ``None``。

    ``short_name`` 来自 ``LicenseShortName``（如 ``CC BY 4.0``、``Public domain``），
    ``template`` 来自 ``License``（如 ``cc-by-4.0``、``pd``、``cc0``）。任何一个字段
    出现 SA/NC/ND/合理使用等拒绝标记都会直接拒绝，未知许可证一律拒绝。
    """
    short = re.sub(r"\s+", " ", strip_html(short_name).lower()).strip()
    tmpl = (template or "").strip().lower()
    if str(non_free).strip().lower() in {"true", "1", "yes"}:
        return None
    if not short and not tmpl:
        return None
    for value in (short, tmpl):
        if value and _DENY_TOKENS.search(value):
            return None

    def from_short(value: str) -> str | None:
        if not value:
            return None
        if value in {"cc0", "cc-0", "cc zero", "cc0 1.0"} or value.startswith("cc0 "):
            return "CC0"
        if value in {"public domain", "pd"} or value.startswith("pd-") or value.startswith(
            "public domain "
        ):
            return "Public domain"
        if _CC_BY_SHORT.match(value):
            version = re.search(r"\d(?:\.\d)?", value)
            return f"CC BY {version.group(0)}" if version else "CC BY"
        return None

    def from_template(value: str) -> str | None:
        if not value:
            return None
        if value in {"cc0", "cc-zero"} or value.startswith("cc0-"):
            return "CC0"
        if value == "pd" or value.startswith("pd-") or value == "public domain":
            return "Public domain"
        if _CC_BY_TEMPLATE.match(value):
            version = re.search(r"\d(?:\.\d)?", value)
            return f"CC BY {version.group(0)}" if version else "CC BY"
        return None

    by_short = from_short(short)
    by_template = from_template(tmpl)
    if short and tmpl and (by_short is None) != (by_template is None):
        # 两个字段互相矛盾（例如一个可识别、一个是未知许可证）时保守拒绝。
        return None
    return by_short or by_template


def is_upload_url(value: Any) -> bool:
    """仅允许 API 返回的 upload.wikimedia.org HTTPS 地址。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == UPLOAD_HOST
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _clean_url(value: str) -> str:
    """去掉 utm 统计参数，保证同一文件命中同一个下载缓存。"""
    parsed = urlsplit(value)
    return f"https://{UPLOAD_HOST}{parsed.path}"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _matches_aspect(width: int, height: int, aspect: VideoAspect) -> bool:
    if aspect == VideoAspect.square:
        return True
    if aspect == VideoAspect.portrait:
        return height > width
    return width > height


def _candidate_renditions(info: dict, duration: float) -> Iterable[dict]:
    """原始文件与转码版本统一成 {url, width, height, mime, est_bytes}。"""
    mime = str(info.get("mime") or "").lower()
    if mime.startswith(VIDEO_MIME_PREFIXES):
        yield {
            "url": info.get("url"),
            "width": _int(info.get("width")),
            "height": _int(info.get("height")),
            "mime": mime,
            "est_bytes": _int(info.get("size")),
            "id": "original",
        }
    derivatives = info.get("derivatives")
    if not isinstance(derivatives, list):
        return
    for derivative in derivatives:
        if not isinstance(derivative, dict):
            continue
        dtype = str(derivative.get("type") or "").lower()
        key = str(derivative.get("transcodekey") or "")
        if not key or not dtype.startswith(VIDEO_MIME_PREFIXES):
            # 没有 transcodekey 的条目就是原始文件，已在上面处理。
            continue
        bandwidth = _int(derivative.get("bandwidth"))
        yield {
            "url": derivative.get("src"),
            "width": _int(derivative.get("width")),
            "height": _int(derivative.get("height")),
            "mime": dtype.split(";", 1)[0],
            "est_bytes": int(bandwidth * duration / 8) if bandwidth else None,
            "id": key,
        }


def pick_rendition(
    info: dict,
    duration: float,
    aspect: VideoAspect,
    max_bytes: int,
) -> dict | None:
    target_w, target_h = aspect.to_resolution()
    usable = []
    for rendition in _candidate_renditions(info, duration):
        width, height = rendition["width"], rendition["height"]
        if not width or not height or not is_upload_url(rendition["url"]):
            continue
        if rendition["est_bytes"] and rendition["est_bytes"] > max_bytes:
            continue
        if not _matches_aspect(width, height, aspect):
            continue
        if min(width, height) < MIN_SHORT_SIDE:
            continue
        usable.append(rendition)
    if not usable:
        return None
    # MP4 解码最稳妥，同等条件下优先 MP4。
    def mp4_rank(item: dict) -> int:
        return 0 if item["mime"] == "video/mp4" else 1

    filling = [
        r for r in usable
        if aspect == VideoAspect.square or (r["width"] >= target_w and r["height"] >= target_h)
    ]
    if filling:
        return min(filling, key=lambda r: (r["width"] * r["height"], mp4_rank(r)))
    return max(usable, key=lambda r: (r["width"] * r["height"], -mp4_rank(r)))


def build_search_params(search_term: str) -> dict:
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": f"{search_term} filetype:video",
        "gsrnamespace": "6",
        "gsrlimit": str(SEARCH_LIMIT),
        "prop": "videoinfo",
        "viprop": "url|size|mime|dimensions|extmetadata|derivatives",
        "viextmetadatafilter": (
            "LicenseShortName|License|LicenseUrl|Artist|Credit|ObjectName|NonFree"
        ),
    }


def parse_search_response(
    payload: Any,
    search_term: str,
    minimum_duration: int,
    aspect: VideoAspect,
    max_bytes: int,
) -> list[MaterialInfo]:
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list):
        return []

    pages = sorted(
        (p for p in pages if isinstance(p, dict)),
        key=lambda p: p.get("index") if isinstance(p.get("index"), int) else 10**9,
    )
    items: list[MaterialInfo] = []
    for page in pages:
        infos = page.get("videoinfo") or page.get("imageinfo")
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
            continue
        info = infos[0]
        duration = info.get("duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < max(minimum_duration, 1)
        ):
            # 没有时长的条目是静态图片或无法验证的文件，库存视频流程不接受。
            continue
        meta = info.get("extmetadata")
        license_name = classify_license(
            _meta_value(meta, "LicenseShortName"),
            _meta_value(meta, "License"),
            _meta_value(meta, "NonFree"),
        )
        if not license_name:
            continue
        rendition = pick_rendition(info, float(duration), aspect, max_bytes)
        if not rendition:
            continue

        title = str(page.get("title") or "")
        display_title = strip_html(_meta_value(meta, "ObjectName")) or re.sub(
            r"^File:", "", title
        )
        artist = strip_html(_meta_value(meta, "Artist"))
        credit = strip_html(_meta_value(meta, "Credit"))
        source_page = info.get("descriptionurl")
        license_url = _meta_value(meta, "LicenseUrl")
        if license_url.startswith("//"):
            license_url = "https:" + license_url

        item = MaterialInfo()
        item.provider = PROVIDER
        item.url = _clean_url(rendition["url"])
        item.duration = int(duration)
        item.source_info = {
            "provider": PROVIDER,
            "search_term": search_term,
            "asset_id": str(page.get("pageid")) if page.get("pageid") is not None else None,
            "source_page": source_page if isinstance(source_page, str) else None,
            "creator": {"name": artist[:200]} if artist else None,
            "title": display_title[:300] or title[:300],
            "license": license_name,
            "license_url": license_url[:300] or None,
            "credit": credit[:300] or None,
            "rendition": {
                "id": rendition["id"],
                "width": rendition["width"],
                "height": rendition["height"],
            },
        }
        items.append(item)
    return items


def search_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
    *,
    http_get: Callable[..., Any],
    max_bytes: int,
    proxies: Any = None,
    verify: bool = True,
) -> list[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    response = http_get(
        API_URL,
        params=build_search_params(search_term),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        proxies=proxies,
        verify=verify,
        timeout=(15, 30),
        allow_redirects=False,
    )
    response.raise_for_status()
    return parse_search_response(
        response.json(), search_term, minimum_duration, aspect, max_bytes
    )


def credit_line(record: dict) -> str | None:
    """把一条素材来源记录格式化为署名行；非 Wikimedia 记录返回 ``None``。"""
    if not isinstance(record, dict) or record.get("provider") != PROVIDER:
        return None
    title = record.get("title") or record.get("local_file") or "Untitled"
    creator = record.get("creator") if isinstance(record.get("creator"), dict) else {}
    author = creator.get("name") or "Unknown author"
    license_name = record.get("license") or "Unknown license"
    parts = [f'"{title}" by {author}', license_name]
    if record.get("license_url"):
        parts.append(str(record["license_url"]))
    if record.get("source_page"):
        parts.append(f"via Wikimedia Commons: {record['source_page']}")
    return " | ".join(parts)


def credit_lines(records: Iterable[dict]) -> list[str]:
    lines: list[str] = []
    seen = set()
    for record in records or []:
        line = credit_line(record)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def render_credits(lines: list[str]) -> str:
    header = "Video material credits (Wikimedia Commons)\n" + "=" * 42 + "\n"
    return header + "\n".join(f"- {line}" for line in lines) + "\n"
