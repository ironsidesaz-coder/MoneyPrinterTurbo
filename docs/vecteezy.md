# Vecteezy video source

MoneyPrinterTurbo can pull stock clips from the [Vecteezy API](https://www.vecteezy.com/api).
Select **Vecteezy** in the WebUI video-source dropdown, or pass `--video-source vecteezy` to the CLI.

## Setup

1. Create a Vecteezy API account and copy the **API key** and the numeric **account ID**.
2. Add them to the `[app]` section of your local `config.toml`. This file is gitignored, so never commit it.

```toml
vecteezy_api_keys = ["<your key>"]        # Bearer token; a list rotates keys
vecteezy_account_id = "<numeric id>"       # selects the v2 API: /v2/{account_id}/...
vecteezy_allow_paid_downloads = false      # default; see "Cost guard"
```

You can also enter the key and account ID in the WebUI under **Video Source → Vecteezy**.

## How it works

- **Search:** `GET /v2/{account_id}/resources` with `content_type=video`,
  `license_type=commercial` (editorial-only content is excluded),
  `family_friendly=true` and `sort_by=relevance`. The minimum clip duration
  (`video_clip_duration`) is applied server-side through the `duration` filter.
- **Aspect ratio:** clips are filtered by their available download sizes. After a clip
  downloads, its real width and height are probed with ffprobe. A clip with the
  wrong orientation is deleted and skipped, and the probed duration replaces the estimate.
- **Blocklist:** the global `material_blocklist` is matched against each clip's
  title and tags. The free tier returns no tags, so in practice the title is checked.
- **Lazy download:** search results carry only a stable `vecteezy:resource/<id>`
  reference. The 24-hour signed URL is requested
  (`GET .../resources/{id}/download?file_type=mp4`) only for clips that are actually
  used, so each one costs exactly one download of quota.
- **No search cache:** Vecteezy asks API users to work from fresh results, so
  Vecteezy (like Coverr) is never stored in the material search cache.
- **Fallback:** if Vecteezy returns too few clips, the existing Wikimedia Commons
  fallback (`wikimedia_fallback_enabled`) fills the gap, just as it does for Pexels and Pixabay.

## Cost guard

| Plan | Search calls | Downloads |
| --- | --- | --- |
| Free | unlimited | 500 / month |
| Pay-As-You-Go | $0.002 / call | $1.00 / download |

Before every search and download, the quota is read from
`GET .../account/info`, which does not count against any quota.

- When `vecteezy_allow_paid_downloads = false` (the default), a download is refused
  once `download.call_limit` is used up, or if the quota cannot be read (fail closed).
- Search calls are blocked only when the account reports a positive
  `general.call_limit` that is used up. Free accounts report `0`, meaning unmetered.
  On Pay-As-You-Go, each search still costs $0.002.

Downloads are serialized under a lock so parallel workers cannot overshoot the allowance.

## Licence and attribution (Free License)

From the [Vecteezy licensing agreement](https://www.vecteezy.com/licensing-agreement):

- **Attribution is required.** For video, add **"Vecteezy.com"** to the final
  credits. Online, link "Stock Videos by Vecteezy" to the clip's attribution URL.
- Licensed to an individual user. No products for resale, merchandise, logos or NFTs.
  Production budgets are capped at $1,000.
- If you cannot give attribution, you need a Pro licence.

For every Vecteezy clip it uses, MoneyPrinterTurbo writes a line to `credits.txt`
next to the final video, under a "Video material credits (Vecteezy)" section
that repeats the rule above:

```
"A close up of a cracked concrete floor" (Vecteezy #50022880) | Stock Videos by Vecteezy.com: https://www.vecteezy.com/free-videos/... | Vecteezy Free License (attribution required)
```

The attribution URL comes from the download response's `required_attribution_url`.
Only https `*.vecteezy.com` URLs are accepted, with query strings stripped.

## 30-day deletion rule

The licence forbids stockpiling: downloaded content may be kept for **up to 30 days** while the
project is in progress, and **unused content must be deleted** once the project is complete.
MoneyPrinterTurbo caches downloaded clips under `storage/cache_videos/`. Delete
Vecteezy files you did not use from there (and from task folders) within 30 days, for example:

```bash
find storage/cache_videos -name 'vid-*' -mtime +30 -delete
```
