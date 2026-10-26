"""
Vecteezy stock video source (https://www.vecteezy.com/developers).

API reference: https://www.vecteezy.com/api-docs (Swagger, V1 and V2).

How this source differs from Pexels/Pixabay:

- Search (``GET /v2/{account_id}/resources?content_type=video``) does not return
  download URLs. Each file needs its own ``GET .../resources/{id}/download``
  call, which counts against the *download* quota. Free accounts get 500
  downloads per month; Pay-As-You-Go accounts pay $0.002 per API call and
  $1.00 per download. Search results therefore carry a stable
  ``vecteezy:resource/<id>`` reference instead of a URL, and the signed URL is
  requested only for clips the download loop really uses.
- Before every search and download the account quota is read from
  ``GET .../account/info`` (documented as not counting against any quota).
  Unless ``vecteezy_allow_paid_downloads`` is true, a download is refused once
  the no-overage allowance (``download.call_limit``, 500 on the free plan) is
  used up, so a key is never charged $1 per download by accident. Search calls
  are only blocked when the account reports a positive ``general.call_limit``
  that is used up (free accounts report 0 = unmetered).
- Free API accounts only see free content, and tags/dimensions/AI flags are
  "only available for paid customers". The orientation of a clip without
  dimensions is checked after download; titles (and tags when present) go
  through the same sensitive-content filter as Wikimedia Commons.
- Free License content requires attribution. The download response says
  whether attribution is required; the line is written to ``credits.txt``.
- Vecteezy's terms forbid stockpiling: downloaded content not used in a
  project must be deleted within 30 days. Clean the material cache
  (``storage/cache_videos`` or the configured material directory) regularly.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

PROVIDER = "vecteezy"
API_BASE_URL = "https://api.vecteezy.com"
REFERENCE_PREFIX = "vecteezy:resource/"
LICENSE_URL = "https://www.vecteezy.com/licensing-agreement"
FREE_LICENSE = "Vecteezy Free License (attribution required)"
PAID_LICENSE = "Vecteezy License"
DEFAULT_ATTRIBUTION_URL = "https://www.vecteezy.com/free-videos"
SEARCH_PER_PAGE = 50
# Vecteezy's ``duration`` filter takes "min_max" in seconds.
MAX_SEARCH_DURATION_SECONDS = 3600
PREFERRED_FILE_TYPES = ("mp4", "mov", "webm")

_quota_lock = threading.Lock()


class VecteezyQuotaError(RuntimeError):
    """The next call would exceed the free allowance and paid calls are disabled."""


def reference(resource_id: Any) -> str:
    return f"{REFERENCE_PREFIX}{int(resource_id)}"


def is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIX)


def resource_id_from_reference(value: str) -> int:
    if not is_reference(value):
        raise ValueError("not a vecteezy resource reference")
    resource_id = int(value[len(REFERENCE_PREFIX):])
    if resource_id <= 0:
        raise ValueError("invalid vecteezy resource id")
    return resource_id


def api_prefix(account_id: Any) -> str:
    """V2 paths include the account id; without one fall back to the V1 paths."""
    text = str(account_id or "").strip()
    if text:
        if not text.isdigit():
            raise ValueError("vecteezy_account_id must be numeric")
        return f"{API_BASE_URL}/v2/{text}"
    return f"{API_BASE_URL}/v1"


def build_search_params(search_term: str, minimum_duration: int) -> dict[str, Any]:
    minimum = max(1, int(minimum_duration or 1))
    return {
        "term": search_term,
        "content_type": "video",
        "per_page": SEARCH_PER_PAGE,
        "page": 1,
        "sort_by": "relevance",
        "license_type": "commercial",
        "family_friendly": "true",
        "duration": f"{minimum}_{max(minimum, MAX_SEARCH_DURATION_SECONDS)}",
    }


def is_vecteezy_url(value: Any) -> bool:
    try:
        host = (urlsplit(str(value)).hostname or "").lower()
    except ValueError:
        return False
    return host == "vecteezy.com" or host.endswith(".vecteezy.com")


def safe_attribution_url(value: Any) -> str:
    """Keep only a public https vecteezy.com page (no query/credentials)."""
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_ATTRIBUTION_URL
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return DEFAULT_ATTRIBUTION_URL
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not is_vecteezy_url(value)
    ):
        return DEFAULT_ATTRIBUTION_URL
    netloc = parsed.netloc.lower()
    if netloc == "api.vecteezy.com":
        # The live API returns attribution links on its own host, which 404s;
        # the same path is the public page on www.vecteezy.com.
        netloc = "www.vecteezy.com"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def resource_dimensions(resource: dict) -> tuple[int, int]:
    """Source dimensions, or the largest listed download size (paid fields)."""
    dims = resource.get("dimensions")
    if isinstance(dims, dict):
        width, height = _positive_int(dims.get("width")), _positive_int(dims.get("height"))
        if width and height:
            return width, height
    sizes: list = []
    metadata = resource.get("file_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("available_download_sizes"), list):
        sizes.extend(metadata["available_download_sizes"])
    if isinstance(resource.get("file_sizes"), list):
        sizes.extend(resource["file_sizes"])
    best = (0, 0)
    for size in sizes:
        if not isinstance(size, dict):
            continue
        width, height = _positive_int(size.get("width")), _positive_int(size.get("height"))
        if width and height and width * height > best[0] * best[1]:
            best = (width, height)
    return best


def resource_file_type(resource: dict) -> str:
    """Pick the download ``file_type`` (V2 file_metadata or V1 file_types)."""
    extensions: list[str] = []
    metadata = resource.get("file_metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("available_file_types"), list):
        for entry in metadata["available_file_types"]:
            if isinstance(entry, dict) and isinstance(entry.get("extension"), str):
                extensions.append(entry["extension"].lower().lstrip("."))
    if isinstance(resource.get("file_types"), list):
        extensions.extend(
            str(ext).lower().lstrip(".") for ext in resource["file_types"] if isinstance(ext, str)
        )
    for preferred in PREFERRED_FILE_TYPES:
        if preferred in extensions:
            return preferred
    # No list at all: the API accepts an omitted file_type for single-format
    # resources; "mp4" is the documented video default.
    return "" if extensions else "mp4"


def parse_search_response(
    payload: Any,
    search_term: str,
    minimum_duration: int,
    matches_aspect: Callable[[int, int], bool],
    content_filter: Any = None,
) -> list[dict[str, Any]]:
    """
    Turn a search payload into candidate dicts.

    ``matches_aspect(width, height)`` is only applied when dimensions are known;
    clips without dimensions (free accounts) are kept and flagged so the
    downloader can verify the real orientation.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
        return []
    candidates = []
    for resource in payload["resources"]:
        if not isinstance(resource, dict):
            continue
        if str(resource.get("content_type") or "video").lower() != "video":
            continue
        resource_id = _positive_int(resource.get("id"))
        if not resource_id:
            continue
        file_type = resource_file_type(resource)
        if file_type == "":
            continue  # only non-video formats (e.g. after-effects projects)
        title = str(resource.get("title") or "").strip()
        tags = [str(t) for t in resource.get("tags") or [] if isinstance(t, str)]
        if content_filter is not None and content_filter.match(title, " ".join(tags)):
            continue
        width, height = resource_dimensions(resource)
        if width and height and not matches_aspect(width, height):
            continue
        candidates.append(
            {
                "resource_id": resource_id,
                "title": title[:300],
                "file_type": file_type or "mp4",
                "width": width or None,
                "height": height or None,
                "ai_generated": resource.get("ai_generated"),
                # Search results carry no duration; the API-side ``duration``
                # filter guarantees at least ``minimum_duration`` seconds.
                "duration": max(1, int(minimum_duration or 1)),
            }
        )
    return candidates


def _quota_bucket(info: Any, kind: str) -> dict:
    if not isinstance(info, dict):
        return {}
    current = info.get("current")
    if not isinstance(current, dict):
        return {}
    bucket = current.get(kind)
    return bucket if isinstance(bucket, dict) else {}


def free_calls_remaining(info: Any, kind: str) -> int:
    """Calls left before overage charges, from ``account/info`` (``general``/``download``)."""
    bucket = _quota_bucket(info, kind)
    limit = bucket.get("call_limit")
    count = bucket.get("call_count")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return 0
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return 0
    return max(0, int(limit) - int(count))


def _general_calls_unmetered(info: Any) -> bool:
    """
    Free accounts report ``general.call_limit == 0`` because API calls are not
    capped. Only an explicit positive limit is enforced for search calls; a
    Pay-As-You-Go account may therefore spend $0.002 per search call, which is
    documented in config.example.toml. Downloads are always enforced.
    """
    bucket = _quota_bucket(info, "general")
    limit = bucket.get("call_limit")
    return bool(bucket) and (limit in (0, None) or isinstance(limit, bool))


def ensure_quota(
    fetch_account_info: Callable[[], Any],
    kind: str,
    allow_paid: bool,
) -> None:
    """
    Fail closed before a call that could be billed.

    With ``allow_paid`` the check is skipped (Pay-As-You-Go: $0.002 per call,
    $1.00 per download). Otherwise the call only proceeds while the account
    still has no-overage allowance for ``kind``; any error reading the quota
    also blocks the call.
    """
    if allow_paid:
        return
    try:
        info = fetch_account_info()
    except Exception as exc:  # network/auth failure: do not risk a charge
        raise VecteezyQuotaError(
            f"vecteezy quota check failed ({type(exc).__name__}); "
            "refusing the call because vecteezy_allow_paid_downloads is false"
        ) from exc
    if kind == "general" and _general_calls_unmetered(info):
        return
    if free_calls_remaining(info, kind) <= 0:
        raise VecteezyQuotaError(
            f"vecteezy free {kind} allowance is used up (or this key has none); "
            "set vecteezy_allow_paid_downloads = true to allow billed calls"
        )


def quota_lock() -> threading.Lock:
    """Serialises quota check + download-URL request so workers cannot overshoot."""
    return _quota_lock


def parse_download_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("vecteezy download response is not an object")
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("vecteezy download response has no https url")
    requires_attribution = payload.get("requires_attribution")
    return {
        "url": url,
        # Unknown means free-tier semantics: attribute to be safe.
        "requires_attribution": requires_attribution is not False,
        "attribution_url": safe_attribution_url(payload.get("required_attribution_url")),
    }


def attribution_fields(title: str, download: dict[str, Any]) -> dict[str, str]:
    """Fields stored on the material record and used for credits.txt."""
    if download.get("requires_attribution", True):
        return {
            "title": title or "Untitled",
            "license": FREE_LICENSE,
            "license_url": LICENSE_URL,
            "credit": f"Stock Videos by Vecteezy.com: {download.get('attribution_url') or DEFAULT_ATTRIBUTION_URL}",
        }
    return {
        "title": title or "Untitled",
        "license": PAID_LICENSE,
        "license_url": LICENSE_URL,
    }


# Vecteezy's "How to give attribution": videos must add "Vecteezy.com" to the
# final credits; on the web, link "<keyword> Stock Videos by Vecteezy" to the
# ``required_attribution_url`` from the download response.
CREDITS_NOTE = (
    'Free License: add "Vecteezy.com" to the final credits of the video; '
    'online, link "Stock Videos by Vecteezy" to the URL listed for each clip.'
)


def credit_line(record: dict) -> str | None:
    if not isinstance(record, dict) or record.get("provider") != PROVIDER:
        return None
    if record.get("license") != FREE_LICENSE:
        return None  # paid-plan downloads need no attribution
    title = record.get("title") or record.get("local_file") or "Untitled"
    asset = f" (Vecteezy #{record['asset_id']})" if record.get("asset_id") else ""
    credit = record.get("credit") or f"Stock Videos by Vecteezy.com: {DEFAULT_ATTRIBUTION_URL}"
    return f'"{title}"{asset} | {credit} | {FREE_LICENSE}'


def credit_lines(records: Iterable[dict]) -> list[str]:
    lines: list[str] = []
    seen = set()
    for record in records or []:
        line = credit_line(record)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines
