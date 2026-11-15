"""
KomikStation API by Nat — v1.0.0
Sumber: https://komikstation.org  (WordPress + MangaStream)

Scrape HTML + ts_reader JSON.
Anti-bot ringan:
  - Header mirip Chrome lengkap (sec-ch-ua, Sec-Fetch-*)
  - Cookie jar + warm-up homepage sebelum request sensitif
  - Retry + ganti Referer bila 403 / challenge
  - Fallback path search (?s= → /manga/?title=)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from mangum import Mangum

APP_VERSION = "1.0.0"
BASE = os.environ.get("KOMIKSTATION_BASE", "https://komikstation.org").rstrip("/")
TIMEOUT = float(os.environ.get("KOMIKSTATION_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("KOMIKSTATION_RETRIES", "3"))

# cookie jar process-level (warm-up antar request di instance yang sama)
_COOKIES: Dict[str, str] = {}
_LAST_WARM = 0.0

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

def _browser_headers(referer: Optional[str] = None, navigate: bool = True) -> Dict[str, str]:
    ref = referer or (BASE + "/")
    same = urlparse(ref).netloc.endswith("komikstation.org")
    h = {
        "User-Agent": UA_CHROME,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": ref,
        "Origin": BASE if same else BASE,
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if navigate:
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin" if same else "none"
        h["Sec-Fetch-User"] = "?1"
    else:
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "same-origin"
    return h


AD_RE = re.compile(
    r"(?:banner|casino|slot|doubleclick|adservice|/iklan/|promo|1x1|pixel\.|/logo)",
    re.I,
)
SVG_RE = re.compile(r"data:image/svg|\.svg(?:\?|$)", re.I)
CHAPTER_NUM_RE = re.compile(r"chapter[-_\s]?(\d+(?:\.\d+)?)", re.I)
SLUG_MANGA_RE = re.compile(r"/manga/([^/]+)/?")
FLAG_RE = re.compile(r"/flags/|typeflag|kr\.svg|cn\.svg|jp\.svg", re.I)

app = FastAPI(
    title="KomikStation API by Nat",
    description="Unofficial scraper API untuk komikstation.org",
    version=APP_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _abs(url: Optional[str]) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return urljoin(BASE + "/", url.lstrip("/"))


def _clean_img(url: Optional[str]) -> str:
    if not url:
        return ""
    url = str(url).strip()
    if not url or SVG_RE.search(url) or FLAG_RE.search(url):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def _img_from_tag(img) -> str:
    if not img:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-original", "data-srcset", "srcset", "src"):
        val = img.get(attr)
        if not val:
            continue
        if "srcset" in attr:
            val = val.split(",")[0].strip().split(" ")[0]
        cleaned = _clean_img(val)
        if cleaned and not FLAG_RE.search(cleaned):
            return cleaned
    return ""


def _norm_type(raw: str) -> str:
    t = (raw or "").strip().upper()
    if "MANHWA" in t:
        return "MANHWA"
    if "MANHUA" in t:
        return "MANHUA"
    if "MANGA" in t:
        return "MANGA"
    return t[:20] or "COMIC"


def _slug_from_url(url: str) -> str:
    if not url:
        return ""
    m = SLUG_MANGA_RE.search(url)
    if m:
        return m.group(1).strip("/")
    path = urlparse(url).path.strip("/")
    # chapter url: slug-chapter-N
    m = re.match(r"(.+)-chapter-", path, re.I)
    if m:
        return m.group(1)
    return path.split("/")[-1] if path else ""


def _item(judul, slug, thumb, type_="", **extra) -> Dict[str, Any]:
    slug = (slug or "").strip()
    return {
        "judul": (judul or slug or "Tanpa judul").strip(),
        "title": (judul or slug or "Tanpa judul").strip(),
        "slug": slug,
        "thumbnail": _clean_img(thumb),
        "sampul": _clean_img(thumb),
        "type": _norm_type(type_),
        "source": "komikstation",
        "url": f"{BASE}/manga/{slug}/" if slug else "",
        **extra,
    }


def _is_challenge(status: int, text: str) -> bool:
    if status in (403, 503):
        return True
    low = (text or "")[:4000].lower()
    return (
        "just a moment" in low
        or "cf-browser-verification" in low
        or "challenge-platform" in low
        or "attention required" in low
    )


async def _warm_up(client: httpx.AsyncClient) -> None:
    """Kunjungi homepage agar cookie CF/session terisi."""
    global _LAST_WARM, _COOKIES
    now = time.time()
    if now - _LAST_WARM < 45 and _COOKIES:
        return
    try:
        res = await client.get(BASE + "/", headers=_browser_headers(navigate=True))
        _COOKIES.update({k: v for k, v in res.cookies.items()})
        _LAST_WARM = time.time()
    except Exception:
        pass


async def fetch_html(path: str, referer: Optional[str] = None) -> str:
    """GET HTML dengan warm-up, cookie, header browser, retry anti-CF."""
    url = path if path.startswith("http") else f"{BASE}{path if path.startswith('/') else '/' + path}"
    last_err: Optional[Exception] = None
    text = ""
    status = 0

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        cookies=_COOKIES or None,
    ) as client:
        await _warm_up(client)

        for attempt in range(MAX_RETRIES):
            # rotasi referer ringan
            if attempt == 0:
                ref = referer or (BASE + "/")
            elif attempt == 1:
                ref = BASE + "/manga/"
            else:
                ref = BASE + "/"

            headers = _browser_headers(referer=ref, navigate=True)
            # sedikit jeda antar retry (mengurangi pola bot)
            if attempt:
                await asyncio.sleep(0.35 * attempt)

            try:
                res = await client.get(url, headers=headers)
                status = res.status_code
                text = res.text or ""
                # simpan cookie baru
                for k, v in res.cookies.items():
                    _COOKIES[k] = v

                if _is_challenge(status, text):
                    last_err = HTTPException(
                        status_code=403,
                        detail="Cloudflare challenge/403. Coba lagi sebentar.",
                    )
                    # warm ulang lalu retry
                    _LAST_WARM = 0
                    await _warm_up(client)
                    continue

                if status == 404:
                    raise HTTPException(404, "Halaman tidak ditemukan di KomikStation")
                if status >= 400:
                    last_err = HTTPException(502, f"Upstream HTTP {status}")
                    continue

                return text
            except HTTPException:
                raise
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.25)

    if isinstance(last_err, HTTPException):
        raise last_err
    raise HTTPException(
        status_code=403,
        detail=f"Gagal mengambil halaman (status={status}). Cloudflare mungkin memblokir IP server.",
    )


def parse_list_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, Any]] = []
    seen = set()

    cards = soup.select(".bs .bsx, .listupd .bsx, .bsx")
    if not cards:
        cards = soup.select(".bs")

    for card in cards:
        a = card.select_one("a[href*='/manga/']") or card.select_one("a[href]")
        if not a:
            continue
        href = a.get("href") or ""
        slug = _slug_from_url(href)
        if not slug or slug in ("list-mode", "manga") or slug in seen:
            continue
        if any(x in slug for x in ("page", "genre", "?")):
            continue
        seen.add(slug)

        title = (a.get("title") or "").strip()
        if not title:
            tt = card.select_one(".tt, h2, h3, h4")
            title = tt.get_text(" ", strip=True) if tt else slug
        title = re.sub(r"\s+", " ", title).strip()

        # sampul: prioritaskan img uploads / ts-post-image, skip flags
        thumb = ""
        for img in card.select("img"):
            src = _img_from_tag(img)
            if src and "uploads" in src:
                thumb = src
                break
            if src and not thumb:
                thumb = src
        if not thumb:
            thumb = _img_from_tag(card.select_one("img.ts-post-image, img.wp-post-image"))

        type_ = ""
        t_el = card.select_one(".type")
        if t_el:
            classes = " ".join(t_el.get("class") or [])
            for name in ("Manhwa", "Manhua", "Manga"):
                if name.lower() in classes.lower() or name.lower() in (t_el.get("title") or "").lower():
                    type_ = name
                    break
            if not type_:
                type_ = t_el.get_text(" ", strip=True)

        status = ""
        st = card.select_one(".status")
        if st:
            status = st.get_text(" ", strip=True)

        out.append(_item(title, slug, thumb, type_, status=status))
    return out


def parse_detail_html(html: str, slug: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one("h1.entry-title, h1")
    judul = h1.get_text(" ", strip=True) if h1 else slug
    judul = re.sub(r"\s*Bahasa Indonesia\s*$", "", judul, flags=re.I).strip()

    thumb = ""
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        thumb = _clean_img(og.get("content"))
    if not thumb:
        for img in soup.select(".thumb img, .seriestucontent img, img.ts-post-image, img.wp-post-image"):
            src = _img_from_tag(img)
            if src and "uploads" in src:
                thumb = src
                break

    sinopsis = ""
    for sel in (".entry-content", ".seriestucontent .entry-content", ".wd-full", ".desc"):
        el = soup.select_one(sel)
        if not el:
            continue
        t = el.get_text("\n", strip=True)
        if len(t) > 50:
            sinopsis = t
            break

    type_ = ""
    status = ""
    info = soup.get_text(" ", strip=True)[:3000]
    if re.search(r"\bManhwa\b", info, re.I):
        type_ = "MANHWA"
    elif re.search(r"\bManhua\b", info, re.I):
        type_ = "MANHUA"
    elif re.search(r"\bManga\b", info, re.I):
        type_ = "MANGA"
    m = re.search(r"Status\s*[:：]\s*([A-Za-z]+)", info, re.I)
    if m:
        status = m.group(1)

    genres: List[str] = []
    for a in soup.select(".mgen a, a[href*='/genres/'], a[href*='/genre/']"):
        g = a.get_text(strip=True)
        if g and g not in genres:
            genres.append(g)

    chapters: List[Dict[str, Any]] = []
    seen = set()
    for a in soup.select("#chapterlist a[href], .eplister a[href]"):
        href = _abs(a.get("href") or "")
        if not href or href in seen:
            continue
        # skip download mirrors
        if "klikcdn.com/ddl" in href or "/ddl?" in href:
            continue
        if "-chapter-" not in href and "/chapter/" not in href:
            continue
        seen.add(href)
        num = ""
        m = CHAPTER_NUM_RE.search(href) or CHAPTER_NUM_RE.search(a.get_text(" ", strip=True))
        if m:
            num = m.group(1)
        judul_ch = f"Chapter {num}" if num else re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        # buang tanggal di judul
        judul_ch = re.sub(
            r"\b(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember|\w+ \d{1,2},?\s*\d{4}).*$",
            "",
            judul_ch,
            flags=re.I,
        ).strip() or (f"Chapter {num}" if num else "Chapter")
        chapters.append(
            {
                "judul": judul_ch,
                "title": judul_ch,
                "slug": href.rstrip("/").split("/")[-1],
                "number": num,
                "url": href,
            }
        )

    return {
        "judul": judul or slug,
        "title": judul or slug,
        "slug": slug,
        "thumbnail": thumb,
        "sampul": thumb,
        "sinopsis": sinopsis,
        "synopsis": sinopsis,
        "type": _norm_type(type_),
        "status": status,
        "genres": genres,
        "genre": genres,
        "chapters": chapters,
        "source": "komikstation",
        "url": f"{BASE}/manga/{slug}/",
    }


def parse_chapter_images(html: str) -> List[str]:
    """Utamakan ts_reader.run JSON; fallback img#readerarea."""
    images: List[str] = []

    # 1) ts_reader
    m = re.search(r"ts_reader\.run\((\{.*?\})\)\s*;", html, re.S)
    if not m:
        m = re.search(r"ts_reader\.run\((\{.*)\)\s*;\s*</script>", html, re.S)
    if m:
        raw = m.group(1)
        data = None
        for end in range(len(raw), max(len(raw) - 5000, 0), -1):
            try:
                data = json.loads(raw[:end])
                break
            except Exception:
                continue
        if isinstance(data, dict):
            for src in data.get("sources") or []:
                if not isinstance(src, dict):
                    continue
                for u in src.get("images") or []:
                    if isinstance(u, str) and u.startswith("http"):
                        images.append(u)
                if images:
                    break

    # 2) DOM
    if not images:
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.select("#readerarea img, #chimg img, .rdl img"):
            src = _img_from_tag(img)
            if src:
                images.append(src)

    # 3) regex CDN
    if not images:
        for m in re.finditer(
            r"https?://img\.klikcdn\.com/[^\"'\s<>]+\.(?:jpg|jpeg|png|webp)",
            html,
            re.I,
        ):
            images.append(m.group(0))

    out: List[str] = []
    seen = set()
    for u in images:
        u = _clean_img(u)
        if not u or AD_RE.search(u) or u in seen:
            continue
        if "logo" in u.lower() or "flag" in u.lower():
            continue
        seen.add(u)
        out.append(u)
    return out


# ── routes ───────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "name": "KomikStation API by Nat",
        "version": APP_VERSION,
        "source": BASE,
        "endpoints": {
            "health": "/health",
            "latest": "/api/latest?page=1",
            "popular": "/api/popular?page=1&type=",
            "search": "/api/search?q=",
            "detail": "/api/detail/{slug}",
            "chapter": "/api/chapter?slug=&chapter=  OR  ?url=",
            "proxy_img": "/api/img?url=",
        },
    }


@app.get("/health")
async def health():
    return {
        "status": True,
        "version": APP_VERSION,
        "base": BASE,
        "has_cookies": bool(_COOKIES),
    }


@app.get("/api/latest")
async def api_latest(page: int = Query(1, ge=1)):
    # order=update kadang CF; fallback order=popular / home
    paths = []
    if page <= 1:
        paths = ["/manga/?order=update", "/manga/?order=popular", "/"]
    else:
        paths = [f"/manga/?order=update&page={page}", f"/manga/?order=popular&page={page}"]

    last_exc = None
    for path in paths:
        try:
            html = await fetch_html(path)
            data = parse_list_html(html)
            if data:
                return {
                    "status": True,
                    "source": "komikstation",
                    "page": page,
                    "total": len(data),
                    "data": data,
                }
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc if isinstance(last_exc, HTTPException) else HTTPException(502, str(last_exc))
    return {"status": True, "source": "komikstation", "page": page, "total": 0, "data": []}


@app.get("/api/popular")
async def api_popular(
    page: int = Query(1, ge=1),
    type: str = Query("", description="manga|manhwa|manhua"),
):
    t = (type or "").strip().lower()
    q = "order=popular"
    if t in ("manga", "manhwa", "manhua"):
        q = f"type={t}&order=popular"
    path = f"/manga/?{q}" + (f"&page={page}" if page > 1 else "")
    html = await fetch_html(path, referer=BASE + "/manga/")
    data = parse_list_html(html)
    return {
        "status": True,
        "source": "komikstation",
        "page": page,
        "type": t or "all",
        "total": len(data),
        "data": data,
    }


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    q = q.strip()
    # 1) ?s= sering CF → coba dulu
    paths = []
    if page <= 1:
        paths.append(f"/?s={quote(q)}")
        paths.append(f"/manga/?title={quote(q)}")
    else:
        paths.append(f"/page/{page}/?s={quote(q)}")
        paths.append(f"/manga/?title={quote(q)}&page={page}")

    last_exc = None
    for path in paths:
        try:
            html = await fetch_html(path, referer=BASE + "/")
            data = parse_list_html(html)
            return {
                "status": True,
                "source": "komikstation",
                "query": q,
                "page": page,
                "total": len(data),
                "data": data,
            }
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc if isinstance(last_exc, HTTPException) else HTTPException(502, str(last_exc))
    return {"status": True, "source": "komikstation", "query": q, "page": page, "total": 0, "data": []}


@app.get("/api/detail/{slug}")
async def api_detail(slug: str):
    slug = slug.strip("/")
    html = await fetch_html(f"/manga/{slug}/", referer=BASE + "/manga/")
    detail = parse_detail_html(html, slug)
    if not detail.get("judul") and not detail.get("chapters"):
        raise HTTPException(404, "Komik tidak ditemukan / parse gagal")
    return {"status": True, "source": "komikstation", "data": detail}


@app.get("/api/chapter")
async def api_chapter(
    slug: str = Query(""),
    chapter: str = Query(""),
    url: str = Query(""),
):
    page_url = (url or "").strip()
    if not page_url:
        s = (slug or "").strip()
        c = (chapter or "").strip()
        if not s or not c:
            raise HTTPException(400, "Butuh url=... atau slug=...&chapter=...")
        if c.startswith("http"):
            page_url = c
        elif "-chapter-" in c:
            page_url = f"{BASE}/{c.strip('/')}/"
        else:
            num = re.sub(r"^[^\d]*", "", c) or c
            page_url = f"{BASE}/{s}-chapter-{num}/"

    ref = f"{BASE}/manga/{slug}/" if slug else BASE + "/"
    html = await fetch_html(page_url, referer=ref)
    images = parse_chapter_images(html)
    if not images:
        raise HTTPException(404, "Gambar chapter kosong / terkunci")

    ch_num = chapter
    m = CHAPTER_NUM_RE.search(page_url)
    if m:
        ch_num = m.group(1)

    return {
        "status": True,
        "source": "komikstation",
        "slug": slug or _slug_from_url(page_url),
        "chapter": ch_num,
        "url": page_url,
        "total_images": len(images),
        "images": images,
        "gambar": images,
    }


@app.get("/api/img")
async def api_img(url: str = Query(...)):
    target = _abs(url)
    if not target.startswith("http"):
        raise HTTPException(400, "url tidak valid")
    headers = {
        "User-Agent": UA_CHROME,
        "Referer": BASE + "/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        res = await client.get(target, headers=headers)
        if res.status_code >= 400:
            raise HTTPException(res.status_code, f"Gagal ambil gambar ({res.status_code})")
        ctype = res.headers.get("content-type") or "image/jpeg"
        return Response(content=res.content, media_type=ctype)


handler = Mangum(app, lifespan="off")
