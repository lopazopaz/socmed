# SocMed Resolver

Private social-media media resolver intended for an iOS Shortcut.

## What it does

`POST /resolve` with a supported social-media URL and receive one or more direct media URLs for images/videos.

Supported host families include Instagram, TikTok, X/Twitter, Reddit, Facebook, and Pinterest.

The service first tries `yt-dlp`, then falls back to Open Graph media metadata when possible.

## Deploy on Wasmer Edge

Wasmer can auto-detect this repository as a Python/FastAPI app because it contains `requirements.txt` and `app.py`.

1. In Wasmer, create/import an app from GitHub.
2. Choose `lopazopaz/socmed` and the `main` branch.
3. Add an environment variable:
   - `SOCMED_API_KEY` = a long private random string
4. Deploy.
5. Open `/health` on the generated Wasmer URL. It should return:

```json
{"ok": true}
```

## Optional cookies

Some sites may require a logged-in session for content you are authorized to access. You can optionally provide a Netscape-format cookies file as base64 in:

- `COOKIES_B64`

Do not commit cookies or tokens to this repository.

## API

### Request

```http
POST /resolve
Authorization: Bearer YOUR_SOCMED_API_KEY
Content-Type: application/json
```

```json
{
  "url": "https://www.instagram.com/reel/.../"
}
```

### Example response

```json
{
  "ok": true,
  "source": "www.instagram.com",
  "title": "Example",
  "media": [
    {
      "type": "video",
      "url": "https://...",
      "ext": "mp4"
    }
  ]
}
```

For carousel/slideshow posts, `media` may contain multiple image/video objects.

## iOS Shortcut flow

1. Receive a URL from the Share Sheet.
2. `POST` it to `https://YOUR-WASMER-APP.wasmer.app/resolve`.
3. Add header `Authorization: Bearer YOUR_SOCMED_API_KEY`.
4. Read the `media` array.
5. Repeat through each item.
6. Download each `url`.
7. Save to Photos.

Use this only for media you own or have permission to download.
