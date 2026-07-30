---
name: send-media
description: How to send files (images, audio, videos, documents) to a Telegram chat — local, downloaded, or straight from a URL.
---

# Delivering Media and Files to a Chat

When someone asks you to send a file, image, video, audio, or document, you have `send_file`.

## How to use `send_file`

### Parameters
- **file_path** (string): either
  - a local path — workspace-relative (e.g. `downloads/123/report.pdf`) or absolute — to a file already on disk, or
  - an `http(s)://` URL, in which case Telegram fetches it directly. No download happens on this end.
- **file_type** (string, optional): `document`, `photo`, `video`, or `audio`. Omit it (or pass `auto`) and it's inferred from the extension — you only need to set it when the extension is misleading or missing.
- **caption** (string, optional): a text message to attach underneath the file.

### Sending a file that's already local
Just call `send_file` with the path. **Verify it exists first** with `execute_shell_command ls <path>` if you're not sure — don't guess.

### Sending something from the internet
Two cases, and the difference matters:

- **Small enough for Telegram to fetch itself** (roughly under 20MB for a document, 5MB for a photo): pass the URL straight to `send_file` as `file_path`. Telegram's servers do the fetching — nothing is downloaded here first.
- **Bigger than that, or you need to look at / process the file before sending it**: use `download_url` first (saves it into this chat's workspace folder, capped at 45MB), then `send_file` with the local path it returns.

Never use `execute_shell_command` with `wget`/`curl` to fetch a file — `download_url` is the tool for that; it's sandboxed, size-capped, and blocked from reaching internal network addresses, none of which the shell tool gives you.

### Reading a page instead of sending it
If what's actually wanted is the *content* of a page or link (an article, a doc, an API response) — not the file itself — use `fetch_url`, not `send_file`/`download_url`. See the `web-fetch-and-deliver` skill.

### Generated images
If asked to *generate* an image, don't use `send_file` — use `image_generate` instead. `send_file`/`download_url` are for content that already exists somewhere.
