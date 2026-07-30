"""
webio.py — outbound HTTP for the bot: page fetch/summarize and file download.

Every request in this module goes through `_validate_url`, and — critically —
it is re-run on every redirect hop, not just the URL the model handed us. A
same-origin page that 302s to http://169.254.169.254/ would sail straight
through a check that only looked at the first URL. This is the one place
external URLs are allowed to touch the network from tool-triggered code, so
the SSRF guard lives here once instead of being reimplemented (or forgotten)
at each call site.

Known limitation, accepted rather than solved here: `_validate_url` resolves
the hostname once and httpx resolves it again independently when it actually
connects, which leaves a narrow DNS-rebinding gap between the two lookups.
Closing that fully means pinning the connection to the address we already
resolved, which is more machinery than a bot fetching text pages and small
files warrants right now.
"""
import ipaddress
import logging
import os
import re
import socket
from html import unescape as _html_unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

import config

logger = logging.getLogger(__name__)

_USER_AGENT = "ClaireBot/1.0 (+telegram)"

# ── SSRF guard ───────────────────────────────────────────────────────────────

_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = {80, 443}

# ipaddress's own is_private doesn't cover the shared/CGNAT range (RFC 6598),
# used by carrier-grade NAT and increasingly by cloud metadata endpoints.
_EXTRA_BLOCKED_IPV4_NETS = [ipaddress.ip_network("100.64.0.0/10")]


class SSRFBlocked(Exception):
    """A URL, or a redirect target, resolves somewhere it shouldn't."""


def _is_blocked_ip(ip) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _EXTRA_BLOCKED_IPV4_NETS)
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _is_blocked_ip(mapped)
    return False


def _validate_url(url: str) -> None:
    """Raises SSRFBlocked with a human-readable reason if `url` is unsafe to fetch."""
    parsed = urlsplit(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlocked(f"unsupported scheme {parsed.scheme!r} (only http/https allowed)")

    if parsed.username or parsed.password:
        raise SSRFBlocked("URLs with embedded credentials are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlocked("URL has no hostname")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in _ALLOWED_PORTS:
        raise SSRFBlocked(f"port {port} is not allowed (only 80/443)")

    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFBlocked(f"could not resolve host {hostname!r}: {e}")

    if not infos:
        raise SSRFBlocked(f"host {hostname!r} did not resolve to any address")

    for info in infos:
        raw_ip = info[4][0].split("%", 1)[0]  # strip an IPv6 zone id, if any
        ip = ipaddress.ip_address(raw_ip)
        if _is_blocked_ip(ip):
            raise SSRFBlocked(f"host {hostname!r} resolves to a disallowed address ({ip})")


def _open(client: httpx.Client, method: str, url: str, *, max_redirects: int,
          headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    """
    Issue `method url`, following up to `max_redirects` redirects manually —
    validating the SSRF guard again on every hop. A streaming httpx.Response is
    returned; the caller is responsible for closing it.
    """
    seen = 0
    current = url
    while True:
        _validate_url(current)
        req = client.build_request(method, current, headers=headers)
        resp = client.send(req, stream=True)
        if resp.has_redirect_location:
            location = resp.headers.get("location")
            resp.close()
            if not location:
                raise SSRFBlocked("redirect response had no Location header")
            if seen >= max_redirects:
                raise SSRFBlocked(f"too many redirects (> {max_redirects})")
            current = urljoin(current, location)
            seen += 1
            continue
        return resp


def _read_capped(resp: httpx.Response, max_bytes: int) -> "tuple[bytes, bool]":
    """Read a streaming response body, stopping at max_bytes. Returns (data, truncated)."""
    chunks: List[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            keep = len(chunk) - (total - max_bytes)
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


# ── HTML → text, no extra dependency ─────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Minimal HTML→text: drops tags/script/style/nav chrome, keeps the words."""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
    _BREAK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        raw = " ".join("\n" if p == "\n" else p for p in self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        return parser.text()
    except Exception:
        logger.debug("HTML parse failed; falling back to a crude tag strip.", exc_info=True)
        return _html_unescape(re.sub(r"<[^>]+>", " ", html)).strip()


_TEXT_LIKE_PREFIXES = ("text/", "application/json", "application/xml", "application/xhtml", "application/javascript")


def _is_text_like(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith(_TEXT_LIKE_PREFIXES)


# ── fetch_url ────────────────────────────────────────────────────────────────

def fetch_url(url: str, mode: str = "text") -> Dict[str, Any]:
    """
    Fetch a URL and return its content as a dict.

    mode:
      - "text" (default): HTML is reduced to readable text.
      - "raw": the body as decoded text, no HTML stripping (JSON/XML/plain text).
      - "headers": status + response headers only, body is never read.

    Bounded by config.FETCH_TIMEOUT_SECONDS, config.FETCH_MAX_BYTES, and
    config.FETCH_MAX_REDIRECTS hops — every hop re-validated against SSRF.
    Non-text content types are refused with a pointer to download_url instead.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "empty URL"}
    if mode not in ("text", "raw", "headers"):
        mode = "text"

    headers = {"User-Agent": _USER_AGENT}
    method = "HEAD" if mode == "headers" else "GET"

    try:
        with httpx.Client(timeout=config.FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = _open(client, method, url, max_redirects=config.FETCH_MAX_REDIRECTS, headers=headers)
            try:
                final_url = str(resp.url)
                status_code = resp.status_code
                content_type = resp.headers.get("content-type", "")

                if mode == "headers":
                    return {
                        "ok": True,
                        "mode": mode,
                        "url": final_url,
                        "status": status_code,
                        "content_type": content_type,
                        "headers": dict(resp.headers),
                    }

                if not _is_text_like(content_type):
                    return {
                        "ok": False,
                        "error": (
                            f"content type {content_type!r} is not text; "
                            "use download_url to save it instead"
                        ),
                        "url": final_url,
                        "status": status_code,
                        "content_type": content_type,
                    }

                body, truncated = _read_capped(resp, config.FETCH_MAX_BYTES)
            finally:
                resp.close()
    except SSRFBlocked as e:
        return {"ok": False, "error": f"refused: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timed out after {config.FETCH_TIMEOUT_SECONDS:.0f}s"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"request failed: {e}"}

    if status_code >= 400:
        return {"ok": False, "error": f"HTTP {status_code}", "url": final_url, "status": status_code}

    text = body.decode("utf-8", errors="replace")
    if mode == "text" and content_type.split(";", 1)[0].strip().lower() in ("text/html", "application/xhtml+xml"):
        text = _html_to_text(text)

    return {
        "ok": True,
        "mode": mode,
        "url": final_url,
        "status": status_code,
        "content_type": content_type,
        "text": text,
        "truncated": truncated,
    }


# ── download_url ─────────────────────────────────────────────────────────────

class _DownloadTooLarge(Exception):
    pass


def _downloads_dir(chat_id: Any) -> str:
    d = os.path.join(config.TOOL_WORKSPACE_DIR, "downloads", str(chat_id))
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(name: str, fallback: str = "download") -> str:
    name = os.path.basename((name or "").strip())
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._")
    return (name or fallback)[:150]


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{root}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _enforce_chat_quota(chat_dir: str, incoming_bytes: int, quota_bytes: int) -> None:
    """Evict the least-recently-accessed files in chat_dir until incoming_bytes fits."""
    entries = []
    total = 0
    for fname in os.listdir(chat_dir):
        fpath = os.path.join(chat_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            st = os.stat(fpath)
        except OSError:
            continue
        entries.append((st.st_atime, fpath, st.st_size))
        total += st.st_size

    entries.sort(key=lambda e: e[0])  # oldest access time first
    i = 0
    while total + incoming_bytes > quota_bytes and i < len(entries):
        _, fpath, fsize = entries[i]
        try:
            os.remove(fpath)
            total -= fsize
            logger.info(f"Evicted {fpath!r} to stay under the per-chat download quota.")
        except OSError as e:
            logger.warning(f"Failed evicting {fpath!r}: {e}")
        i += 1


def download_url(url: str, chat_id: Any, filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Download a URL into workspace/downloads/<chat_id>/.

    Bounded by config.DOWNLOAD_MAX_BYTES per file and config.DOWNLOAD_TIMEOUT_SECONDS.
    Each chat has its own quota (config.DOWNLOAD_CHAT_QUOTA_BYTES) — the
    least-recently-accessed files in that chat's folder are evicted first to
    make room for a new one, so one very active chat can't starve another.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "empty URL"}

    headers = {"User-Agent": _USER_AGENT}

    try:
        with httpx.Client(timeout=config.DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = _open(client, "GET", url, max_redirects=config.FETCH_MAX_REDIRECTS, headers=headers)
            try:
                if resp.status_code >= 400:
                    return {"ok": False, "error": f"HTTP {resp.status_code}", "url": str(resp.url)}

                content_length = resp.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > config.DOWNLOAD_MAX_BYTES:
                            return {
                                "ok": False,
                                "error": (
                                    f"file is {int(content_length):,} bytes, over the "
                                    f"{config.DOWNLOAD_MAX_BYTES:,} byte limit"
                                ),
                            }
                    except ValueError:
                        pass

                final_url = str(resp.url)
                content_type = resp.headers.get("content-type", "")

                if not filename:
                    filename = os.path.basename(urlsplit(final_url).path)
                safe_name = _safe_filename(filename)

                chat_dir = _downloads_dir(chat_id)
                dest_path = _unique_path(os.path.join(chat_dir, safe_name))
                _enforce_chat_quota(chat_dir, config.DOWNLOAD_MAX_BYTES, config.DOWNLOAD_CHAT_QUOTA_BYTES)

                total = 0
                tmp_path = dest_path + ".part"
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            total += len(chunk)
                            if total > config.DOWNLOAD_MAX_BYTES:
                                raise _DownloadTooLarge()
                            f.write(chunk)
                except _DownloadTooLarge:
                    os.remove(tmp_path)
                    return {
                        "ok": False,
                        "error": f"download exceeded the {config.DOWNLOAD_MAX_BYTES:,} byte limit and was aborted",
                    }
                os.replace(tmp_path, dest_path)
            finally:
                resp.close()
    except SSRFBlocked as e:
        return {"ok": False, "error": f"refused: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timed out after {config.DOWNLOAD_TIMEOUT_SECONDS:.0f}s"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"download failed: {e}"}
    except OSError as e:
        return {"ok": False, "error": f"could not save file: {e}"}

    return {
        "ok": True,
        "url": final_url,
        "path": dest_path,
        "filename": os.path.basename(dest_path),
        "size": total,
        "content_type": content_type,
    }
