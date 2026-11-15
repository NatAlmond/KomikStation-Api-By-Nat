# KomikStation API by Nat

Unofficial scraper API untuk **https://komikstation.org** (MangaStream).

**By: Nat · v1.0.1**

## Endpoint

| Method | Path | Keterangan |
|--------|------|------------|
| GET | `/health` | Status + cookie session |
| GET | `/api/latest?page=1` | Update (fallback popular) |
| GET | `/api/popular?page=1&type=manhwa` | Populer / filter tipe |
| GET | `/api/search?q=` | Cari (`?s=` + fallback `title=`) |
| GET | `/api/detail/{slug}` | Detail + chapter |
| GET | `/api/chapter?slug=&chapter=` | Gambar chapter (`ts_reader`) |
| GET | `/api/img?url=` | Proxy gambar CDN |

## Anti-bot (ringan)

- Header Chrome lengkap (`sec-ch-ua`, `Sec-Fetch-*`)
- Cookie jar + **warm-up** homepage
- Retry 3× + ganti Referer
- Fallback path bila search kena CF

## Deploy

1. Upload file ke repo GitHub (root)
2. Vercel → Import → Deploy
3. Opsional env: `KOMIKSTATION_BASE`, `KOMIKSTATION_TIMEOUT`, `KOMIKSTATION_RETRIES`

## Search fallback

1. `?s=` / `title=` (sering CF)
2. Katalog `/manga/list-mode/` (cache ~15 menit) → filter judul lokal
3. Filter list popular/latest

## Catatan

Cloudflare bisa tetap 403 dari IP datacenter. Search lebih rentan daripada list/detail.
