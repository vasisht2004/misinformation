# Fact-check listing recon (2022–2025)

Probes only: robots.txt, sitemaps, WordPress REST index, RSS, homepage.
Article URLs found in sitemaps or feeds were not requested.
A bot challenge was recorded and not retried.
Politeness: 2s between requests, max 30 requests per site, 20s timeout.

## boom

Base: https://www.boomlive.in
Requests made: 15

| Probe | Status | Finding |
| --- | --- | --- |
| homepage | 200 | bot challenge (captcha) |
| robots.txt | 200 | Disallow (26): /admin/, /xhr/, /staging/, /search/, /search?*, /*?s=, /*?q=, /*?query=, /*?keyword=, /preview/story-, /amp/preview/story-, /embed/, /*?page=, /*?sort=, /*?order=, /*?filter=, /alfoo, /sildoo, /dutas, /metsmall, /*.woff$, /*.woff2$, /*.ttf$, /*.otf$, /platform.twitter.com/, /www.instagram.com/. Sitemap entries (3): https://www.boomlive.in/news-sitemap-daily.xml, https://www.boomlive.in/sitemap-daily.xml, https://www.boomlive.in/sitemap-daily-updated.xml |
| wp-json posts?per_page=1 | 404 | bot challenge (captcha) |
| wp-json categories?search=fact | 200 | bot challenge (captcha) Redirected to https://www.boomlive.in/search?search=fact&per_page=20. |
| wp-json categories?search=health | 200 | bot challenge (captcha) Redirected to https://www.boomlive.in/search?search=health&per_page=20. |
| /feed | 200 | 3 RSS/Atom items |
| /rss | 200 | 0 RSS/Atom items |
| /feed/rss | 404 | bot challenge (captcha) |
| /sitemap.xml | 404 | bot challenge (captcha) |
| /sitemap_index.xml | 404 | bot challenge (captcha) |
| /post-sitemap.xml | 404 | bot challenge (captcha) |
| /news-sitemap.xml | 404 | bot challenge (captcha) |
| robots Sitemap https://www.boomlive.in/news-sitemap-daily.xml | 200 | urlset; 0 child sitemaps; 3 article URLs; sample article URLs: https://www.boomlive.in/fact-check/video-of-punjab-police-linking-amit-shahs-nephew-to-lpu-case-is-a-deepfake-32300, https://www.boomlive.in/fact-check/scripted-video-of-disabled-muslim-man-being-harassed-shared-as-real-32285, https://www.boomlive.in/fact-check/old-videos-falsely-linked-to-protests-against-cec-gyanesh-kumar-32284 |
| robots Sitemap https://www.boomlive.in/sitemap-daily.xml | 200 | urlset; 0 child sitemaps; 3 article URLs; sample article URLs: https://www.boomlive.in/fact-check/video-of-punjab-police-linking-amit-shahs-nephew-to-lpu-case-is-a-deepfake-32300, https://www.boomlive.in/fact-check/scripted-video-of-disabled-muslim-man-being-harassed-shared-as-real-32285, https://www.boomlive.in/fact-check/old-videos-falsely-linked-to-protests-against-cec-gyanesh-kumar-32284 |
| robots Sitemap https://www.boomlive.in/sitemap-daily-updated.xml | 200 | urlset; 0 child sitemaps; 1 article URLs; sample article URLs: https://www.boomlive.in/fact-check/scripted-video-of-disabled-muslim-man-being-harassed-shared-as-real-32285 |
| sitemap summary | 200 | 0 unique child sitemaps; n/a (urlset, not an index); no child-sitemap name contains a year in 2022–2025. Sample article URLs (3): https://www.boomlive.in/fact-check/video-of-punjab-police-linking-amit-shahs-nephew-to-lpu-case-is-a-deepfake-32300, https://www.boomlive.in/fact-check/scripted-video-of-disabled-muslim-man-being-harassed-shared-as-real-32285, https://www.boomlive.in/fact-check/old-videos-falsely-linked-to-protests-against-cec-gyanesh-kumar-32284 |

**Recommended route:** sitemap — A sitemap index or urlset was readable. Child names do not encode 2022–2025, so a later pass still filters URLs by date.

## newsmobile

Base: https://newsmobile.in
Requests made: 13

| Probe | Status | Finding |
| --- | --- | --- |
| homepage | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/. |
| robots.txt | 200 | Disallow (1): /wp-admin/. Sitemap entries (1): https://www.newsmobile.in/wp-sitemap.xml Redirected to https://www.newsmobile.in/robots.txt. |
| wp-json posts?per_page=1 | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/wp-json/wp/v2/posts?per_page=1. |
| wp-json categories?search=fact | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/wp-json/wp/v2/categories?search=fact&per_page=20. |
| wp-json categories?search=health | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/wp-json/wp/v2/categories?search=health&per_page=20. |
| /feed | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/feed. |
| /rss | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/rss. |
| /feed/rss | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/feed/rss. |
| /sitemap.xml | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/wp-sitemap.xml. |
| /sitemap_index.xml | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/sitemap_index.xml. |
| /post-sitemap.xml | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/post-sitemap.xml. |
| /news-sitemap.xml | 200 | bot challenge (Anubis) Redirected to https://www.newsmobile.in/news-sitemap.xml. |
| robots Sitemap https://www.newsmobile.in/wp-sitemap.xml | 200 | bot challenge (Anubis) |

**Recommended route:** blocked-needs-permission — No usable wp-json, sitemap, or RSS listing, and homepage matched Anubis. Do not crawl until that block is permitted.

## thip

Base: https://thip.media
Requests made: 12

| Probe | Status | Finding |
| --- | --- | --- |
| homepage | 403 | bot challenge (Just a moment) |
| robots.txt | 403 | bot challenge (Just a moment); Disallow and Sitemap lines were not parsed |
| wp-json posts?per_page=1 | 403 | bot challenge (Just a moment) |
| wp-json categories?search=fact | 403 | bot challenge (Just a moment) |
| wp-json categories?search=health | 403 | bot challenge (Just a moment) |
| /feed | 403 | bot challenge (Just a moment) |
| /rss | 403 | bot challenge (Just a moment) |
| /feed/rss | 403 | bot challenge (Just a moment) |
| /sitemap.xml | 403 | bot challenge (Just a moment) |
| /sitemap_index.xml | 403 | bot challenge (Just a moment) |
| /post-sitemap.xml | 403 | bot challenge (Just a moment) |
| /news-sitemap.xml | 403 | bot challenge (Just a moment) |

**Recommended route:** blocked-needs-permission — No usable wp-json, sitemap, or RSS listing, and homepage matched Just a moment. Do not crawl until that block is permitted.
