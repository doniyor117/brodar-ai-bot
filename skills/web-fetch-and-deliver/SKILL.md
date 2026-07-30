---
name: web-fetch-and-deliver
description: Reading a specific page/link (fetch_url) versus downloading and delivering a file from one (download_url + send_file).
---

# Reading a Link vs. Delivering a File

Two different tools depending on what was actually asked for. Don't confuse them.

## Someone gave you a link and wants to know what's on it
Use **`fetch_url`**. It returns the page's content as text — HTML is reduced to
readable text by default, so you get the article/doc/answer, not a wall of tags.

- `mode="text"` (default) — for a normal web page, article, or doc.
- `mode="raw"` — for JSON, XML, or plain text you want unmodified (an API response, a `.txt` file, RSS).
- `mode="headers"` — just the status code and response headers, no body. Rarely needed; mostly for checking whether a link is alive or what type it is before deciding what to do with it.

`fetch_url` is for text. It will refuse images, video, audio, archives, and
other binaries — for those, see below.

## Someone wants a file *from* a link — image, video, audio, document, archive
`fetch_url` won't help here; there's no meaningful "text" version of a JPEG.
Two paths:

- **Small file, just deliver it**: skip fetching entirely and pass the URL straight to `send_file` — Telegram fetches it directly (see the `send-media` skill for the size ceiling on that).
- **Need to save it, inspect it, or it's too big for the direct path**: `download_url` first (saves into this chat's workspace, capped at 45MB), then `send_file` with the path it returns.

## Things worth knowing
- Both tools refuse URLs that resolve to an internal/private network address (localhost, RFC1918 ranges, cloud metadata endpoints, etc.) — this is a hard refusal, not something to work around by retrying or rephrasing.
- `fetch_url` caps how much of a page it reads (2MB). A truncated result is still useful for most articles/docs; say so if it's clearly cut off mid-thought.
- Neither tool needs admin privilege to call — anyone can ask you to read a page or grab a file into the workspace. Actually *sending* a file back out with `send_file` still follows the normal admin rules for that tool.
- If a fetch or download fails, the error explains why (bad URL, blocked address, too large, timed out, wrong content type). Read it and tell the user what actually happened — don't just say "that didn't work."
