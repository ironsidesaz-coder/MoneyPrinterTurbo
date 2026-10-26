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

import hashlib
import html
import math
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from loguru import logger

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


# ---------------------------------------------------------------------------
# 内容过滤
#
# Commons 收录大量新闻、档案与执法记录视频（例如警方公开的肇事逃逸监控、战争
# 档案片），它们许可证合规却不适合作为通用配图。下面的词表在标题、描述
# （ObjectName / ImageDescription）和分类（extmetadata Categories）上做整词匹配：
#
# * 词条按“整词/整短语”匹配，前后不能紧邻字母或数字，因此 ``deadline``、
#   ``skill``、``warsaw``、``bodyboard`` 不会命中 ``dead``/``kill``/``war``/``body``。
# * 刻意不收录过宽的单词（``body``、``police``、``operation``、``hunting``），
#   改用 ``dead body``、``police dashcam`` 这类短语，避免误杀 ``body of water``、
#   警察乐队巡游等普通素材。
# * 先移除“安全短语”再匹配，例如 ``crashing waves``、``dead sea``、
#   ``shooting star``、``killer whale``，让海浪、地名和天文素材照常通过。
#   ``crashing`` 本身不在词表中，只有 ``crash``/``crashes``/``crashed`` 会命中。
# * 连字符和下划线视为空格（``hit-and-run`` == ``hit and run``）。
#
# 已知误伤：姓氏或地名中的词（如 ``Jason Slaughter``、``Bloody Creek``）也会被拒，
# 可用 ``wikimedia_allowed_phrases`` 放行。
#
# 宁可多拒绝：后备源只是补位，误拒一条普通素材的代价远低于在成片中出现
# 伤亡画面。用户可通过 ``wikimedia_blocked_terms`` / ``wikimedia_allowed_phrases``
# 扩展两张表。
# ---------------------------------------------------------------------------
DEFAULT_BLOCKED_TERMS: tuple[str, ...] = (
    # 事故 / 碰撞
    "crash", "crashes", "crashed", "car crash", "plane crash", "collision", "collisions",
    "accident", "accidents", "hit and run", "wreck", "wreckage", "pileup", "pile up",
    "derailment",
    # 死亡
    "fatal", "fatally", "fatality", "fatalities", "death", "deaths", "dead", "die",
    "died", "dying", "corpse", "corpses", "cadaver", "dead body", "dead bodies",
    "body bag", "body bags", "human remains", "funeral", "mass grave",
    # 杀戮 / 暴力
    "kill", "kills", "killed", "killing", "killings", "murder", "murders", "murdered",
    "homicide", "manslaughter", "shooting", "shootings", "shot dead", "gunfire",
    "gunshot", "gunshots", "gunman", "execution", "executions", "executed", "beheading",
    "stabbing", "massacre", "genocide", "assault", "violence", "violent", "lynching",
    # 战争 / 爆炸
    "war", "wars", "warfare", "war footage", "war zone", "combat", "battle", "battles",
    "battlefield", "airstrike", "airstrikes", "air strike", "bomb", "bombs", "bombing",
    "bombings", "bombed", "explosion", "explosions", "ied", "missile strike",
    "casualty", "casualties", "invasion", "military operation", "insurgent",
    "insurgents",
    # 血腥 / 医疗
    "blood", "bloody", "gore", "gory", "injury", "injuries", "injured", "wound",
    "wounds", "wounded", "surgery", "surgical", "autopsy", "amputation",
    # 执法 / 监控
    "dashcam", "dash cam", "bodycam", "body cam", "body camera", "body worn",
    "body worn camera", "police dashcam", "police bodycam", "police footage",
    "police video", "critical incident", "officer involved", "surveillance",
    "cctv", "security camera", "security footage", "arrest", "arrested",
    "inmate", "inmates", "prisoner", "prisoners", "shackles", "shackled", "handcuffed",
    # 骚乱 / 恐怖主义
    "riot", "riots", "rioting", "rioters", "terrorism", "terrorist", "terrorists",
    "terror attack", "hostage", "tear gas",
    # 虐待 / 性内容
    "abuse", "abused", "child abuse", "torture", "nudity", "nude", "nudes", "naked",
    "topless", "sexual", "sex", "porn", "porno", "pornography", "pornographic",
    "erotic", "erotica", "masturbation", "genitals", "genitalia",
    # 自残
    "suicide", "suicides", "self harm", "hanged", "noose", "hanging",
    # 动物残忍
    "animal cruelty", "cruelty", "slaughter", "slaughtered", "slaughterhouse",
    "abattoir", "bullfight", "bullfighting", "dogfight", "dogfighting", "cockfight",
    "cockfighting", "roadkill", "carcass", "carcasses", "poaching",
    # 灾难受害者
    "victim", "victims", "disaster victims", "refugee camp", "famine", "starvation",
)
DEFAULT_ALLOWED_PHRASES: tuple[str, ...] = (
    "crashing waves", "waves crashing", "wave crashing", "waves crash", "wave crash",
    "crash of waves", "crash of the waves", "surf crash",
    "dead sea", "dead end", "dead leaves", "dead leaf", "dead tree", "dead trees",
    "dead wood", "dead calm", "death valley",
    "shooting star", "shooting stars", "photo shooting", "film shooting",
    "killer whale", "killer whales",
    "hanging bridge", "hanging garden", "hanging gardens", "hanging lamp",
    "hanging lamps", "hanging light", "hanging lights", "hanging plant",
    "hanging plants", "hanging basket", "hanging valley", "hanging glacier",
    "star wars", "pillow fight", "snowball fight", "food fight",
    "blood moon", "blood orange", "blood oranges",
)

_NORMALIZE_SEPARATORS = re.compile(r"[_\-–—/]+")


def _normalize_text(value: str) -> str:
    text = strip_html(value).lower()
    text = _NORMALIZE_SEPARATORS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_pattern(phrases: Iterable[str]) -> re.Pattern | None:
    cleaned = sorted(
        {_normalize_text(str(p)) for p in phrases if isinstance(p, str) and p.strip()},
        key=len,
        reverse=True,
    )
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return None
    body = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in cleaned)
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])")


class ContentFilter:
    """按整词匹配拒绝敏感素材；``extra_*`` 用于在默认表之外追加词条。"""

    def __init__(
        self,
        extra_blocked_terms: Iterable[str] = (),
        extra_allowed_phrases: Iterable[str] = (),
    ):
        self.blocked_terms = tuple(DEFAULT_BLOCKED_TERMS) + tuple(
            t for t in extra_blocked_terms or () if isinstance(t, str) and t.strip()
        )
        self.allowed_phrases = tuple(DEFAULT_ALLOWED_PHRASES) + tuple(
            t for t in extra_allowed_phrases or () if isinstance(t, str) and t.strip()
        )
        self._blocked = _phrase_pattern(self.blocked_terms)
        self._allowed = _phrase_pattern(self.allowed_phrases)

    @property
    def signature(self) -> str:
        """词表指纹：写入搜索缓存键，修改词表后旧缓存自动失效。"""
        digest = hashlib.sha256(
            "\n".join(
                sorted(_normalize_text(t) for t in self.blocked_terms)
                + ["--"]
                + sorted(_normalize_text(t) for t in self.allowed_phrases)
            ).encode("utf-8")
        )
        return digest.hexdigest()[:10]

    def match(self, *texts: str) -> str | None:
        """返回首个命中的敏感词；全部通过时返回 ``None``。"""
        if self._blocked is None:
            return None
        for text in texts:
            normalized = _normalize_text(text or "")
            if not normalized:
                continue
            if self._allowed is not None:
                normalized = self._allowed.sub(" | ", normalized)
            hit = self._blocked.search(normalized)
            if hit:
                return hit.group(0)
        return None



def _title_text(title: str) -> str:
    text = re.sub(r"^file:", "", title or "", flags=re.IGNORECASE)
    return re.sub(r"\.(webm|ogv|ogg|mp4|mpg|mpeg|mov)$", "", text, flags=re.IGNORECASE)


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
            "LicenseShortName|License|LicenseUrl|Artist|Credit|ObjectName|NonFree|"
            "ImageDescription|Categories"
        ),
    }


def parse_search_response(
    payload: Any,
    search_term: str,
    minimum_duration: int,
    aspect: VideoAspect,
    max_bytes: int,
    content_filter: ContentFilter | None = None,
) -> list[MaterialInfo]:
    content_filter = content_filter or DEFAULT_CONTENT_FILTER
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
        title = str(page.get("title") or "")
        categories = _meta_value(meta, "Categories").replace("|", " | ")
        page_categories = page.get("categories")
        if isinstance(page_categories, list):
            categories += " | " + " | ".join(
                str(c.get("title") or "") for c in page_categories if isinstance(c, dict)
            )
        blocked = content_filter.match(
            _title_text(title),
            _meta_value(meta, "ObjectName"),
            _meta_value(meta, "ImageDescription"),
            categories,
        )
        if blocked:
            logger.info(
                f"wikimedia commons: skipped sensitive file {title!r} (matched {blocked!r})"
            )
            continue
        rendition = pick_rendition(info, float(duration), aspect, max_bytes)
        if not rendition:
            continue

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
    content_filter: ContentFilter | None = None,
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
        response.json(), search_term, minimum_duration, aspect, max_bytes, content_filter
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


# 依赖 strip_html，需在模块末尾构造。
DEFAULT_CONTENT_FILTER = ContentFilter()
