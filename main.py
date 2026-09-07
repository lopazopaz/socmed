import base64
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="SocMed Resolver", version="1.0.1")

ALLOWED_HOSTS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "reddit.com", "www.reddit.com", "old.reddit.com", "redd.it", "v.redd.it", "i.redd.it",
    "facebook.com", "www.facebook.com", "fb.watch",
    "pinterest.com", "www.pinterest.com", "pin.it",
}

API_KEY = os.getenv("SOCMED_API_KEY", "").strip()
COOKIES_B64 = os.getenv("COOKIES_B64", "").strip()


class ResolveRequest(BaseModel):
    url: HttpUrl


def require_auth(authorization: str | None) -> None:
    if not API_KEY:
        return
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def normalized_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def ensure_allowed(url: str) -> None:
    host = normalized_host(url)
    if host not in ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail=f"Unsupported host: {host or 'unknown'}")


def cookie_file() -> str | None:
    if not COOKIES_B64:
        return None
    try:
        raw = base64.b64decode(COOKIES_B64)
        path = os.path.join(tempfile.gettempdir(), "socmed-cookies.txt")
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception:
        return None


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        url = item.get("url")
        if url and url not in seen:
            seen.add(url)
            result.append(item)
    return result


def guess_ext(url: str, fallback: str) -> str:
    path = urlparse(url).path.lower()
    m = re.search(r"\.([a-z0-9]{2,5})$", path)
    return m.group(1) if m else fallback


def media_from_info(info: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    entries = info.get("entries")
    if entries:
        for entry in entries:
            if entry:
                out.extend(media_from_info(entry))
        return dedupe(out)

    direct_url = info.get("url")
    ext = (info.get("ext") or "").lower()
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")

    image_exts = {"jpg", "jpeg", "png", "webp", "gif"}
    video_exts = {"mp4", "mov", "m4v", "webm"}

    if direct_url:
        if ext in image_exts:
            out.append({"type": "image", "url": direct_url, "ext": ext})
        elif ext in video_exts or (vcodec not in (None, "none") and acodec not in (None, "none")):
            out.append({"type": "video", "url": direct_url, "ext": ext or "mp4"})

    for item in info.get("requested_downloads") or []:
        url = item.get("url")
        if not url:
            continue
        item_ext = (item.get("ext") or "").lower()
        out.append({
            "type": "image" if item_ext in image_exts else "video",
            "url": url,
            "ext": item_ext or "mp4",
        })

    if not out and info.get("thumbnail"):
        thumb = info["thumbnail"]
        out.append({"type": "image", "url": thumb, "ext": guess_ext(thumb, "jpg")})

    return dedupe(out)


async def og_fallback(url: str) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    media: list[dict[str, Any]] = []

    for prop in ["og:video:secure_url", "og:video", "twitter:player:stream"]:
        tags = soup.find_all("meta", attrs={"property": prop}) + soup.find_all("meta", attrs={"name": prop})
        for tag in tags:
            content = tag.get("content")
            if content:
                media.append({"type": "video", "url": content, "ext": guess_ext(content, "mp4")})

    for prop in ["og:image:secure_url", "og:image", "twitter:image"]:
        tags = soup.find_all("meta", attrs={"property": prop}) + soup.find_all("meta", attrs={"name": prop})
        for tag in tags:
            content = tag.get("content")
            if content:
                media.append({"type": "image", "url": content, "ext": guess_ext(content, "jpg")})

    return dedupe(media)


@app.get("/")
def root():
    return {"ok": True, "service": "socmed-resolver", "version": "1.0.1"}


@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.1"}


@app.post("/resolve")
async def resolve(req: ResolveRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    url = str(req.url)
    ensure_allowed(url)

    extraction_error = None

    try:
        from yt_dlp import YoutubeDL

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best",
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
            },
        }

        cf = cookie_file()
        if cf:
            ydl_opts["cookiefile"] = cf

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        media = media_from_info(info or {})
        if media:
            return {
                "ok": True,
                "source": normalized_host(url),
                "title": (info or {}).get("title"),
                "media": media,
            }
    except Exception as exc:
        extraction_error = str(exc)

    try:
        media = await og_fallback(url)
        if media:
            return {
                "ok": True,
                "source": normalized_host(url),
                "title": None,
                "media": media,
                "fallback": "opengraph",
            }
    except Exception as exc:
        if not extraction_error:
            extraction_error = str(exc)

    raise HTTPException(
        status_code=422,
        detail={
            "message": "Could not resolve downloadable media. The post may be private, login-only, deleted, or unsupported.",
            "extractor": extraction_error,
        },
    )
