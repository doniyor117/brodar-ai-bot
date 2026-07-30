"""
Phase 10 smoke checks — web fetch, download, deliver.

    python3 test_phase10.py
"""
import os
import re
import sys
import tempfile
import time

import httpx

import test_support

test_support.install_stubs()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


# ── webio._validate_url: the SSRF guard ──────────────────────────────────────
def test_ssrf_guard_blocks_private_and_special_ranges():
    import webio

    blocked = [
        "http://127.0.0.1/",           # loopback
        "http://10.0.0.5/",            # RFC1918
        "http://192.168.1.1/",         # RFC1918
        "http://172.16.0.1/",          # RFC1918
        "http://169.254.169.254/",     # link-local / cloud metadata
        "http://100.64.0.1/",          # CGNAT (RFC 6598)
        "http://224.0.0.1/",           # multicast
        "http://240.0.0.1/",           # reserved
        "http://0.0.0.0/",             # unspecified
        "http://[::1]/",               # IPv6 loopback
        "http://[fe80::1]/",           # IPv6 link-local
        "http://[fc00::1]/",           # IPv6 unique-local
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
    ]
    for url in blocked:
        try:
            webio._validate_url(url)
            check(f"blocks {url}", False, "was allowed through")
        except webio.SSRFBlocked:
            check(f"blocks {url}", True)

    # A real, globally-routable IP must NOT be blocked.
    try:
        webio._validate_url("http://8.8.8.8/")
        check("allows a global IP", True)
    except webio.SSRFBlocked as e:
        check("allows a global IP", False, str(e))


def test_ssrf_guard_scheme_and_authority_rules():
    import webio

    def blocked(url):
        try:
            webio._validate_url(url)
            return False
        except webio.SSRFBlocked:
            return True

    check("rejects ftp://", blocked("ftp://8.8.8.8/"))
    check("rejects file://", blocked("file:///etc/passwd"))
    check("rejects embedded credentials", blocked("http://user:pass@8.8.8.8/"))
    check("rejects a non-standard port", blocked("http://8.8.8.8:8080/"))
    check("allows explicit default port 443", not blocked("https://8.8.8.8:443/"))
    check("rejects a hostname with no DNS record", blocked("http://this-does-not-exist.invalid/"))


# ── fetch_url / download_url, end to end against a mock transport ───────────
def _mock_client(monkeypatch_target, handler):
    """Point webio.httpx.Client at an in-memory transport; returns a restore fn."""
    import webio

    real_client_cls = httpx.Client
    transport = httpx.MockTransport(handler)

    def fake_client(*a, **kw):
        kw["transport"] = transport
        return real_client_cls(*a, **kw)

    webio.httpx.Client = fake_client
    return lambda: setattr(webio.httpx, "Client", real_client_cls)


def _fetch_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if (host, path) == ("93.184.216.34", "/start"):
        return httpx.Response(302, headers={"location": "http://93.184.216.35/final"})
    if (host, path) == ("93.184.216.35", "/final"):
        html = "<html><body><script>evil()</script><h1>Title</h1><p>hello there.</p></body></html>"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
    if (host, path) == ("93.184.216.34", "/private-redirect"):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    if (host, path) == ("93.184.216.34", "/loop-a"):
        return httpx.Response(302, headers={"location": "http://93.184.216.34/loop-b"})
    if (host, path) == ("93.184.216.34", "/loop-b"):
        return httpx.Response(302, headers={"location": "http://93.184.216.34/loop-c"})
    if (host, path) == ("93.184.216.34", "/loop-c"):
        return httpx.Response(302, headers={"location": "http://93.184.216.34/loop-d"})
    if (host, path) == ("93.184.216.34", "/loop-d"):
        return httpx.Response(200, text="too many hops")
    if (host, path) == ("93.184.216.34", "/binary"):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    if (host, path) == ("93.184.216.34", "/big"):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 1000)
    if (host, path) == ("93.184.216.34", "/headers-only"):
        return httpx.Response(200, headers={"content-type": "text/plain", "x-custom": "yes"}, text="body")
    if (host, path) == ("93.184.216.34", "/404"):
        return httpx.Response(404, text="not found")
    if (host, path) == ("93.184.216.34", "/file.pdf"):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 fake pdf")
    if (host, path) == ("93.184.216.34", "/too-big.bin"):
        return httpx.Response(200, headers={"content-type": "application/octet-stream", "content-length": "999999999"})
    if (host, path) == ("93.184.216.34", "/streamed-too-big.bin"):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"y" * 5000)
    return httpx.Response(404)


def test_fetch_url_follows_redirect_and_strips_html():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    try:
        r = webio.fetch_url("http://93.184.216.34/start", mode="text")
        check("fetch ok", r.get("ok") is True, r)
        check("final url is the redirect target", r.get("url") == "http://93.184.216.35/final")
        check("script contents are dropped", "evil" not in r.get("text", ""))
        check("visible text survives", "Title" in r.get("text", "") and "hello there" in r.get("text", ""))
    finally:
        restore()


def test_fetch_url_blocks_redirect_to_private_ip():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    try:
        r = webio.fetch_url("http://93.184.216.34/private-redirect", mode="text")
        check("redirect-to-private is refused", r.get("ok") is False)
        check("refusal names the reason", "refused" in r.get("error", ""))
    finally:
        restore()


def test_fetch_url_too_many_redirects():
    import webio
    import config

    restore = _mock_client(webio, _fetch_handler)
    original = config.FETCH_MAX_REDIRECTS
    config.FETCH_MAX_REDIRECTS = 2
    try:
        r = webio.fetch_url("http://93.184.216.34/loop-a", mode="text")
        check("a redirect chain longer than the cap is refused", r.get("ok") is False)
        check("refusal mentions redirects", "redirect" in r.get("error", "").lower())
    finally:
        config.FETCH_MAX_REDIRECTS = original
        restore()


def test_fetch_url_refuses_binary_content_type():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    try:
        r = webio.fetch_url("http://93.184.216.34/binary", mode="text")
        check("binary content type is refused", r.get("ok") is False)
        check("refusal points at download_url", "download_url" in r.get("error", ""))
    finally:
        restore()


def test_fetch_url_truncates_at_byte_cap():
    import webio
    import config

    restore = _mock_client(webio, _fetch_handler)
    original = config.FETCH_MAX_BYTES
    config.FETCH_MAX_BYTES = 100
    try:
        r = webio.fetch_url("http://93.184.216.34/big", mode="raw")
        check("still ok, just truncated", r.get("ok") is True)
        check("truncated flag is set", r.get("truncated") is True)
        check("body capped at the byte limit", len(r.get("text", "")) == 100)
    finally:
        config.FETCH_MAX_BYTES = original
        restore()


def test_fetch_url_headers_mode_never_reads_body():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    try:
        r = webio.fetch_url("http://93.184.216.34/headers-only", mode="headers")
        check("headers mode ok", r.get("ok") is True)
        check("no text key in headers mode", "text" not in r)
        check("custom header is visible", r.get("headers", {}).get("x-custom") == "yes")
    finally:
        restore()


def test_fetch_url_http_error_status():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    try:
        r = webio.fetch_url("http://93.184.216.34/404", mode="text")
        check("404 surfaces as an error", r.get("ok") is False and "404" in r.get("error", ""))
    finally:
        restore()


def test_fetch_url_rejects_empty_url():
    import webio

    r = webio.fetch_url("", mode="text")
    check("empty URL is rejected without touching the network", r.get("ok") is False)


def test_download_url_saves_into_chat_scoped_dir():
    import webio
    import config

    restore = _mock_client(webio, _fetch_handler)
    tmp = tempfile.mkdtemp()
    original_ws = config.TOOL_WORKSPACE_DIR
    config.TOOL_WORKSPACE_DIR = tmp
    try:
        r = webio.download_url("http://93.184.216.34/file.pdf", chat_id=555)
        check("download ok", r.get("ok") is True, r)
        check("saved under downloads/<chat_id>/", r.get("path", "").startswith(
            os.path.join(tmp, "downloads", "555")
        ))
        check("file actually exists on disk", os.path.isfile(r.get("path", "")))
        check("size matches what was written", r.get("size") == len(b"%PDF-1.4 fake pdf"))
    finally:
        config.TOOL_WORKSPACE_DIR = original_ws
        restore()


def test_download_url_rejects_declared_oversize():
    import webio

    restore = _mock_client(webio, _fetch_handler)
    tmp = tempfile.mkdtemp()
    import config
    original_ws = config.TOOL_WORKSPACE_DIR
    config.TOOL_WORKSPACE_DIR = tmp
    try:
        r = webio.download_url("http://93.184.216.34/too-big.bin", chat_id=1)
        check("declared-oversize download refused before any bytes are read", r.get("ok") is False)
        check("refusal mentions the byte limit", "byte limit" in r.get("error", ""))
    finally:
        config.TOOL_WORKSPACE_DIR = original_ws
        restore()


def test_download_url_aborts_when_stream_exceeds_cap_with_no_content_length():
    import webio
    import config

    restore = _mock_client(webio, _fetch_handler)
    tmp = tempfile.mkdtemp()
    original_ws = config.TOOL_WORKSPACE_DIR
    original_cap = config.DOWNLOAD_MAX_BYTES
    config.TOOL_WORKSPACE_DIR = tmp
    config.DOWNLOAD_MAX_BYTES = 1000  # body is 5000 bytes, no content-length header
    try:
        r = webio.download_url("http://93.184.216.34/streamed-too-big.bin", chat_id=2)
        check("streamed overflow is caught mid-download", r.get("ok") is False)
        leftover = os.path.join(tmp, "downloads", "2")
        partials = [f for f in os.listdir(leftover)] if os.path.isdir(leftover) else []
        check("no partial .part file left behind", not any(f.endswith(".part") for f in partials))
    finally:
        config.TOOL_WORKSPACE_DIR = original_ws
        config.DOWNLOAD_MAX_BYTES = original_cap
        restore()


# ── webio helpers, no network involved ───────────────────────────────────────
def test_safe_filename_sanitizes():
    import webio

    check("path components are stripped", webio._safe_filename("../../etc/passwd") == "passwd")
    check("odd characters are replaced", webio._safe_filename("weird name!.txt") == "weird_name_.txt")
    check("empty name falls back", webio._safe_filename("") == "download")
    check("very long name is capped", len(webio._safe_filename("a" * 500)) <= 150)


def test_unique_path_dedupes():
    import webio

    tmp = tempfile.mkdtemp()
    base = os.path.join(tmp, "report.pdf")
    open(base, "w").close()
    second = webio._unique_path(base)
    check("a colliding name gets a numeric suffix", second == os.path.join(tmp, "report_1.pdf"))
    open(second, "w").close()
    third = webio._unique_path(base)
    check("the next collision increments again", third == os.path.join(tmp, "report_2.pdf"))


def test_enforce_chat_quota_evicts_oldest_first():
    import webio

    tmp = tempfile.mkdtemp()
    old_path = os.path.join(tmp, "old.bin")
    new_path = os.path.join(tmp, "new.bin")
    with open(old_path, "wb") as f:
        f.write(b"x" * 100)
    time.sleep(0.05)
    with open(new_path, "wb") as f:
        f.write(b"x" * 100)
    os.utime(old_path, (time.time() - 1000, time.time() - 1000))

    # 200 bytes already on disk (100 + 100). Asking for room for 150 more
    # against a 250 quota means only the older 100-byte file needs to go.
    webio._enforce_chat_quota(tmp, incoming_bytes=150, quota_bytes=250)
    check("the older file was evicted", not os.path.exists(old_path))
    check("the newer file survived", os.path.exists(new_path))


def test_html_to_text_strips_script_and_style():
    import webio

    html = "<html><head><style>body{color:red}</style></head><body>" \
           "<script>track()</script><h1>Hi</h1><p>para one.</p><p>para two.</p></body></html>"
    text = webio._html_to_text(html)
    check("style contents removed", "color:red" not in text)
    check("script contents removed", "track()" not in text)
    check("headings kept", "Hi" in text)
    check("paragraphs kept and separated", "para one." in text and "para two." in text)


# ── tools.py: curl's tightened allowlist regex ───────────────────────────────
def test_curl_regex_closes_bare_ip_hole():
    import tools

    regex = tools.ALLOWED_COMMANDS["curl"]["args_regex"]
    check("a real hostname still matches", bool(re.match(regex, "-I https://example.com")))
    check("a bare IPv4 literal is rejected", not re.match(regex, "-I https://169.254.169.254"))
    check("a bare IPv4 literal (arbitrary) is rejected", not re.match(regex, "-I https://8.8.8.8"))


# ── agent.py: schema, dispatch helpers, formatting ──────────────────────────
def test_tools_schema_has_new_tools():
    import agent

    names = {t["function"]["name"] for t in agent.TOOLS_SCHEMA}
    check("fetch_url is registered", "fetch_url" in names)
    check("download_url is registered", "download_url" in names)

    fetch_schema = next(t for t in agent.TOOLS_SCHEMA if t["function"]["name"] == "fetch_url")
    check("fetch_url only requires url", fetch_schema["function"]["parameters"]["required"] == ["url"])

    download_schema = next(t for t in agent.TOOLS_SCHEMA if t["function"]["name"] == "download_url")
    check("download_url only requires url", download_schema["function"]["parameters"]["required"] == ["url"])

    send_schema = next(t for t in agent.TOOLS_SCHEMA if t["function"]["name"] == "send_file")
    check("send_file no longer requires file_type", send_schema["function"]["parameters"]["required"] == ["file_path"])


def test_new_tools_are_not_privileged():
    import agent

    check("fetch_url is open to everyone", "fetch_url" not in agent._PRIVILEGED_TOOLS)
    check("download_url is open to everyone", "download_url" not in agent._PRIVILEGED_TOOLS)
    check("send_file is still privileged", "send_file" in agent._PRIVILEGED_TOOLS)


def test_tool_status_lines():
    import agent

    check("fetch_url gets a status line", "fetching" in agent._tool_status_line("fetch_url", {"url": "http://x"}))
    check("download_url gets a status line", "downloading" in agent._tool_status_line("download_url", {"url": "http://x"}))


def test_infer_file_type():
    import agent

    check("jpg -> photo", agent._infer_file_type("pic.jpg") == "photo")
    check("mp4 -> video", agent._infer_file_type("clip.mp4") == "video")
    check("mp3 -> audio", agent._infer_file_type("song.mp3") == "audio")
    check("pdf -> document (default)", agent._infer_file_type("report.pdf") == "document")
    check("no extension -> document (default)", agent._infer_file_type("README") == "document")


def test_send_file_precheck_remote_url_passthrough():
    import agent

    file_input, display, ftype, err = agent._send_file_precheck("https://example.com/pic.jpg", "")
    check("no error for a remote URL", err is None)
    check("remote URL is passed straight through", file_input == "https://example.com/pic.jpg")
    check("type inferred from the URL's extension", ftype == "photo")


def test_send_file_precheck_missing_local_file():
    import agent

    _, _, _, err = agent._send_file_precheck("workspace/does/not/exist.txt", "document")
    check("a missing local file is refused with a plain error", err is not None and "does not exist" in err)


def test_send_file_precheck_oversize_local_file():
    import agent
    import config

    tmp_ws = tempfile.mkdtemp()
    original_ws = config.TOOL_WORKSPACE_DIR
    original_limit = config.SEND_FILE_MAX_DOCUMENT_BYTES
    config.TOOL_WORKSPACE_DIR = tmp_ws
    config.SEND_FILE_MAX_DOCUMENT_BYTES = 10
    try:
        big_path = os.path.join(tmp_ws, "big.bin")
        with open(big_path, "wb") as f:
            f.write(b"x" * 100)
        _, _, _, err = agent._send_file_precheck("big.bin", "document")
        check("an oversize document is refused with the byte counts", err is not None and "byte limit" in err)
    finally:
        config.TOOL_WORKSPACE_DIR = original_ws
        config.SEND_FILE_MAX_DOCUMENT_BYTES = original_limit


def test_format_fetch_and_download_results():
    import agent

    ok_text = agent._format_fetch_result({
        "ok": True, "mode": "text", "url": "http://x/y", "status": 200, "text": "hello",
    })
    check("successful fetch formats the text", "hello" in ok_text and "http://x/y" in ok_text)

    err_text = agent._format_fetch_result({"ok": False, "error": "refused: nope"})
    check("failed fetch formats the error", "refused: nope" in err_text)

    ok_dl = agent._format_download_result({
        "ok": True, "url": "http://x/f.pdf", "path": "/tmp/f.pdf", "size": 123, "content_type": "application/pdf",
    })
    check("successful download mentions the path and send_file", "/tmp/f.pdf" in ok_dl and "send_file" in ok_dl)

    err_dl = agent._format_download_result({"ok": False, "error": "HTTP 404"})
    check("failed download formats the error", "HTTP 404" in err_dl)


def main():
    print("phase 10 — web fetch, download, deliver\n")
    for fn in (
        test_ssrf_guard_blocks_private_and_special_ranges,
        test_ssrf_guard_scheme_and_authority_rules,
        test_fetch_url_follows_redirect_and_strips_html,
        test_fetch_url_blocks_redirect_to_private_ip,
        test_fetch_url_too_many_redirects,
        test_fetch_url_refuses_binary_content_type,
        test_fetch_url_truncates_at_byte_cap,
        test_fetch_url_headers_mode_never_reads_body,
        test_fetch_url_http_error_status,
        test_fetch_url_rejects_empty_url,
        test_download_url_saves_into_chat_scoped_dir,
        test_download_url_rejects_declared_oversize,
        test_download_url_aborts_when_stream_exceeds_cap_with_no_content_length,
        test_safe_filename_sanitizes,
        test_unique_path_dedupes,
        test_enforce_chat_quota_evicts_oldest_first,
        test_html_to_text_strips_script_and_style,
        test_curl_regex_closes_bare_ip_hole,
        test_tools_schema_has_new_tools,
        test_new_tools_are_not_privileged,
        test_tool_status_lines,
        test_infer_file_type,
        test_send_file_precheck_remote_url_passthrough,
        test_send_file_precheck_missing_local_file,
        test_send_file_precheck_oversize_local_file,
        test_format_fetch_and_download_results,
    ):
        fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} phase 10 check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all phase 10 checks passed")


if __name__ == "__main__":
    main()
