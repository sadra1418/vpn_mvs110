from urllib.parse import quote, urljoin, urlparse

import httpx
from flask import Flask, Response, make_response, redirect, request

from config import PORT

app = Flask(__name__)

HOME_PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>اتصال به سایت</title>
    <style>
        body {
            font-family: sans-serif;
            max-width: 700px;
            margin: 80px auto;
            padding: 20px;
        }
        input {
            width: 100%;
            box-sizing: border-box;
            padding: 12px;
            margin: 10px 0;
        }
        button {
            padding: 12px 24px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <h1>آدرس سایت مقصد را وارد کنید</h1>
    <form method="post">
        <input
            name="target_url"
            type="url"
            placeholder="https://example.com"
            required
        >
        <button type="submit">اتصال</button>
    </form>
</body>
</html>
"""


def validate_url(value):
    parsed = urlparse((value or "").strip())

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return value.strip()


def proxy_url(url):
    return "/proxy?url=" + quote(url, safe="")


def rewrite_url(value, current_url):
    if not value:
        return value

    value = value.strip()

    if value.startswith((
        "#",
        "data:",
        "javascript:",
        "mailto:",
        "tel:",
        "blob:",
    )):
        return value

    absolute = urljoin(current_url, value)
    parsed = urlparse(absolute)
    current = urlparse(current_url)

    if parsed.netloc == current.netloc:
        path = parsed.path or "/"

        if parsed.query:
            path += "?" + parsed.query

        if parsed.fragment:
            path += "#" + parsed.fragment

        return path

    return proxy_url(absolute)


def rewrite_srcset(value, current_url):
    parts = []

    for item in value.split(","):
        item = item.strip()

        if not item:
            continue

        pieces = item.split()
        url = rewrite_url(pieces[0], current_url)

        if len(pieces) > 1:
            parts.append(url + " " + " ".join(pieces[1:]))
        else:
            parts.append(url)

    return ", ".join(parts)


def rewrite_html(html, current_url):
    import re

    def replace_attribute(match):
        name = match.group(1)
        value = match.group(3)

        if name.lower() == "srcset":
            value = rewrite_srcset(value, current_url)
        else:
            value = rewrite_url(value, current_url)

        return match.group(2) + value + match.group(4)

    html = re.sub(
        r'(?i)(\b(?:href|src|action|poster|content|data|formaction|srcset)\s*)(=\s*)(["\'])(.*?)\3',
        lambda m: (
            m.group(1)
            + m.group(2)
            + m.group(3)
            + (
                rewrite_srcset(m.group(4), current_url)
                if m.group(1).strip().lower() == "srcset"
                else rewrite_url(m.group(4), current_url)
            )
            + m.group(3)
        ),
        html,
    )

    html = re.sub(
        r'(?i)url\((\s*["\']?)(.*?)\1\)',
        lambda m: "url(" + m.group(1) + rewrite_url(m.group(2), current_url) + m.group(1) + ")",
        html,
    )

    reload_button = """
<style id="vpn-reload-style">
#vpn-reload-button {
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 2147483647;
    border: 0;
    border-radius: 10px;
    padding: 10px 15px;
    background: #111;
    color: white;
    font-size: 14px;
    cursor: pointer;
    box-shadow: 0 3px 12px rgba(0,0,0,.25);
}
#vpn-reload-button:active {
    transform: scale(.96);
}
</style>
<button id="vpn-reload-button" type="button"
        onclick="window.location.reload()">
    ↻ ریلود
</button>
"""

    if "</body>" in html.lower():
        html = re.sub(
            r"(?i)</body>",
            reload_button + "</body>",
            html,
            count=1,
        )
    else:
        html += reload_button

    return html


def make_client():
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=20,
            read=180,
            write=180,
            pool=20,
        ),
        http2=False,
    )


def forwarded_headers(target_url):
    excluded = {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
    }

    headers = {
        key: value
        for key, value in request.headers
        if key.lower() not in excluded
    }

    # Rewrite proxy-origin headers to the upstream site's origin.
    # Otherwise APIs and CDNs may reject requests because they see the proxy host.
    target = urlparse(target_url)
    target_origin = f"{target.scheme}://{target.netloc}"

    if "origin" in headers:
        headers["Origin"] = target_origin

    if "referer" in headers:
        headers["Referer"] = target_origin + "/"

    # Avoid compressed upstream bodies because HTML/CSS must be rewritten.
    headers["Accept-Encoding"] = "identity"

    return headers


def copy_response_headers(source, destination):
    excluded = {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "content-security-policy",
        "content-security-policy-report-only",
        "x-frame-options",
    }

    for key, value in source.headers.items():
        if key.lower() not in excluded and key.lower() != "location":
            destination.headers[key] = value

    location = source.headers.get("location")

    if location:
        destination.headers["Location"] = rewrite_url(
            location,
            request.url_root.rstrip("/") + request.path,
        )

    for cookie in source.headers.get_list("set-cookie"):
        destination.headers.add("Set-Cookie", cookie)


def proxy_request(target):
    target = validate_url(target)

    if not target:
        return make_response("آدرس مقصد معتبر نیست.", 400)

    current_url = target

    headers = forwarded_headers(target)

    cookies = request.cookies.to_dict()
    cookies.pop("vpn_target", None)

    try:
        client = make_client()

        response = client.build_request(
            request.method,
            target,
            headers=headers,
            content=request.get_data(),
            cookies=cookies,
        )

        upstream = client.send(
            response,
            stream=True,
        )

    except Exception as exc:
        return make_response(f"Proxy error: {exc}", 502)

    content_type = upstream.headers.get("content-type", "").lower()

    if "text/html" in content_type:
        try:
            body = upstream.read()
            text = body.decode(
                upstream.encoding or "utf-8",
                errors="replace",
            )
            text = rewrite_html(text, current_url)
            body = text.encode("utf-8")

            result = make_response(body, upstream.status_code)
            copy_response_headers(upstream, result)
            result.headers["Content-Type"] = "text/html; charset=utf-8"

            return result
        finally:
            upstream.close()
            client.close()

    if "text/css" in content_type:
        try:
            body = upstream.read()
            text = body.decode(
                upstream.encoding or "utf-8",
                errors="replace",
            )
            text = rewrite_css(text, current_url)
            body = text.encode("utf-8")

            result = make_response(body, upstream.status_code)
            copy_response_headers(upstream, result)
            result.headers["Content-Type"] = "text/css; charset=utf-8"

            return result
        finally:
            upstream.close()
            client.close()

    def stream():
        try:
            for chunk in upstream.iter_bytes(1024 * 64):
                yield chunk
        finally:
            upstream.close()
            client.close()

    result = Response(
        stream(),
        status=upstream.status_code,
        content_type=upstream.headers.get("content-type"),
    )

    copy_response_headers(upstream, result)

    return result


def rewrite_css(css, current_url):
    import re

    return re.sub(
        r'url\((\s*["\']?)(.*?)\1\)',
        lambda m: (
            "url("
            + m.group(1)
            + rewrite_url(m.group(2), current_url)
            + m.group(1)
            + ")"
        ),
        css,
        flags=re.IGNORECASE,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        target_url = validate_url(request.form.get("target_url", ""))

        if not target_url:
            return "آدرس واردشده معتبر نیست.", 400

        response = redirect("/url?url=" + quote(target_url, safe=""))

        response.set_cookie(
            "vpn_target",
            target_url,
            max_age=86400,
            httponly=True,
            samesite="Lax",
            path="/",
        )

        return response

    return HOME_PAGE


@app.route("/url")
def open_url():
    target_url = validate_url(request.args.get("url", ""))

    if not target_url:
        return "آدرس واردشده معتبر نیست.", 400

    return proxy_request(target_url)


@app.route("/proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy_external():
    target_url = validate_url(request.args.get("url", ""))

    if not target_url:
        return "آدرس مقصد معتبر نیست.", 400

    return proxy_request(target_url)


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy_path(path):
    target_url = request.cookies.get("vpn_target")

    if not target_url:
        return redirect("/")

    target_url = validate_url(target_url)

    if not target_url:
        return redirect("/")

    target = urljoin(target_url.rstrip("/") + "/", path)

    if request.query_string:
        target += "?" + request.query_string.decode()

    return proxy_request(target)


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
