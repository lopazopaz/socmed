import base64
import hashlib
import hmac
import html
import json
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="SocMed Resolver", version="1.0.7")

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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://socmed.wasmer.app").rstrip("/")
MEDIA_SIGNING_KEY = (os.getenv("SOCMED_MEDIA_SECRET", "").strip() or API_KEY or "socmed-local-media-key").encode()

IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
INSTAGRAM_HEADERS = {
    "User-Agent": IOS_UA,
    "Referer": "https://www.instagram.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


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


def sign_media_url(remote_url: str, ext: str = "bin") -> str:
    payload = base64.urlsafe_b64encode(remote_url.encode()).decode().rstrip("=")
    signature = hmac.new(MEDIA_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    safe_ext = ext.lower() if ext.lower() in {"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "m4v"} else "bin"
    return f"{PUBLIC_BASE_URL}/download/instagram-media.{safe_ext}?token={payload}.{signature}"


def decode_media_token(token: str) -> str:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(MEDIA_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        padded = payload + "=" * (-len(payload) % 4)
        url = base64.urlsafe_b64decode(padded.encode()).decode()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("bad url")
        return url
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media token")


def proxy_instagram_media(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proxied: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        remote = copy.get("url")
        if remote:
            copy["url"] = sign_media_url(remote, str(copy.get("ext") or "bin"))
        proxied.append(copy)
    return proxied


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
        item_url = item.get("url")
        if not item_url:
            continue
        item_ext = (item.get("ext") or "").lower()
        out.append({"type": "image" if item_ext in image_exts else "video", "url": item_url, "ext": item_ext or "mp4"})

    if not out and info.get("thumbnail"):
        thumb = info["thumbnail"]
        out.append({"type": "image", "url": thumb, "ext": guess_ext(thumb, "jpg")})

    return dedupe(out)


async def og_fallback(url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=INSTAGRAM_HEADERS) as client:
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



def instasave_media(script: str) -> list[dict[str, Any]]:
    # Decode JavaScript string escapes as text; never execute provider code.
    decoded = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), script)
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), decoded)
    decoded = decoded.replace("\\/", "/").replace("\\'", "'").replace('\\"', '"')
    soup = BeautifulSoup(decoded, "html.parser")
    media = []
    for anchor in soup.select(".download-items__btn a[href]"):
        url = html.unescape(anchor["href"])
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "cdn.instasave.website":
            continue
        # Filename metadata determines type; it is not used for authorization.
        try:
            token = parse_qs(parsed.query)["token"][0]
            payload = token.split(".")[1]
            metadata = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            filename = metadata["filename"]
            ext = filename.rsplit(".", 1)[-1].lower()
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if ext not in {"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "m4v"}:
            continue
        remote = metadata.get("url", "")
        remote_host = normalized_host(remote)
        if urlparse(remote).scheme != "https" or not any(
            remote_host == domain or remote_host.endswith("." + domain)
            for domain in ("cdninstagram.com", "fbcdn.net")
        ):
            continue
        media.append({"type": "video" if ext in {"mp4", "mov", "m4v"} else "image",
                      "url": remote, "ext": ext})
    return dedupe(media)


async def resolve_instagram(url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.instasave.website/media",
            data={"url": url.split("?", 1)[0], "lang": "en"},
        )
        response.raise_for_status()
    media = instasave_media(response.text)
    if not media:
        raise ValueError("Instagram provider returned no downloadable media")
    return media



def instagram_shortcode(url: str) -> str:
    match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", urlparse(url).path)
    if not match:
        raise ValueError("Invalid Instagram post URL")
    return match.group(1)


def instagram_delivery_urls(post_url: str, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shortcode = instagram_shortcode(post_url)
    delivered = []
    for index, item in enumerate(media):
        copy = {key: value for key, value in item.items() if key != "url"}
        ext = str(copy.get("ext") or ("mp4" if copy.get("type") == "video" else "jpg"))
        copy["url"] = f"{PUBLIC_BASE_URL}/instagram/{shortcode}/{index}/media.{ext}"
        delivered.append(copy)
    return delivered


def detect_media(data: bytes, declared_type: str, remote_url: str) -> tuple[str, str] | None:
    declared = (declared_type or "").split(";", 1)[0].lower()
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4", "mp4"
    if declared.startswith("image/"):
        return declared, guess_ext(remote_url, declared.split("/", 1)[1].replace("jpeg", "jpg"))
    if declared.startswith("video/"):
        return declared, guess_ext(remote_url, "mp4")
    return None


@app.get("/")
def root():
    return {"ok": True, "service": "socmed-resolver", "version": "1.0.7"}


@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.7"}


@app.get("/instagram/{shortcode}/{index}/{filename}")
async def instagram_download(shortcode: str, index: int, filename: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,30}", shortcode) or index < 0 or index > 50:
        raise HTTPException(status_code=400, detail="Invalid Instagram media request")
    post_url = f"https://www.instagram.com/p/{shortcode}/"
    try:
        items = await resolve_instagram(post_url)
        if index >= len(items):
            raise HTTPException(status_code=404, detail="Instagram media item not found")
        remote_url = items[index]["url"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Could not retrieve Instagram download")
    return await fetch_remote_media(remote_url, filename)


async def fetch_remote_media(remote_url: str, filename: str = "media"):
    header_profiles = [
        {"User-Agent": IOS_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"},
        INSTAGRAM_HEADERS,
        {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.instagram.com/"},
    ]
    last_error = "Instagram CDN did not return valid media bytes"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        for headers in header_profiles:
            try:
                response = await client.get(remote_url, headers=headers)
                response.raise_for_status()
                data = response.content
                if not data:
                    last_error = "Instagram CDN returned an empty body"
                    continue
                head = data[:512].lstrip().lower()
                if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                    last_error = "Instagram CDN returned HTML instead of media"
                    continue
                detected = detect_media(data, response.headers.get("content-type", ""), remote_url)
                if not detected:
                    last_error = f"Unexpected content type: {response.headers.get('content-type', 'unknown')}"
                    continue
                media_type, ext = detected
                stem = re.sub(r"[^A-Za-z0-9_-]", "-", filename.rsplit(".", 1)[0]) or "instagram-media"
                return Response(
                    content=data,
                    media_type=media_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{stem}.{ext}"',
                        "Cache-Control": "private, max-age=300",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            except Exception as exc:
                last_error = str(exc)
    raise HTTPException(status_code=502, detail=f"Could not fetch Instagram media: {last_error}")


@app.get("/download/{filename}")
@app.get("/media/{token}/{filename}")
@app.get("/media/{token}")
async def media_proxy(token: str, filename: str = "instagram-media"):
    remote_url = decode_media_token(token)
    return await fetch_remote_media(remote_url, filename)


@app.post("/resolve")
async def resolve(req: ResolveRequest, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    url = str(req.url)
    ensure_allowed(url)
    host = normalized_host(url)
    is_instagram = host in {"instagram.com", "www.instagram.com"}
    extraction_error = None

    if is_instagram:
        try:
            media = instagram_delivery_urls(url, await resolve_instagram(url))
            return {"ok": True, "source": host, "title": None,
                    "media": media, "provider": "instasave"}
        except Exception:
            raise HTTPException(status_code=502, detail={
                "message": "Instagram download provider is unavailable or could not extract this post. Please try again later."
            })

    try:
        from yt_dlp import YoutubeDL
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best",
            "http_headers": INSTAGRAM_HEADERS if is_instagram else {"User-Agent": IOS_UA},
        }
        cf = cookie_file()
        if cf:
            ydl_opts["cookiefile"] = cf
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        media = media_from_info(info or {})
        if media:
            if is_instagram:
                media = proxy_instagram_media(media)
            return {"ok": True, "source": host, "title": (info or {}).get("title"), "media": media}
    except Exception as exc:
        extraction_error = str(exc)

    try:
        media = await og_fallback(url)
        if media:
            if is_instagram:
                media = proxy_instagram_media(media)
            return {"ok": True, "source": host, "title": None, "media": media, "fallback": "opengraph"}
    except Exception as exc:
        if not extraction_error:
            extraction_error = str(exc)

    raise HTTPException(status_code=422, detail={"message": "Could not resolve downloadable media. The post may be private, login-only, deleted, or unsupported.", "extractor": extraction_error})
