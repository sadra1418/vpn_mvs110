from urllib.parse import urljoin, urlparse, quote
import re

import httpx
from flask import Flask, make_response, redirect, request

from config import TARGET_URL

app = Flask(__name__)

parsed_target = urlparse(TARGET_URL)
TARGET_ORIGIN = f"{parsed_target.scheme}://{parsed_target.netloc}"
TARGET_HOST = parsed_target.netloc


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
    parsed = urlparse(value.strip())

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return value.strip().rstrip("/")


def rewrite_url(value, target_origin):
    if not value:
        return value

    value = value.strip()

    if value.startswith(("#", "data:", "javascript:", "mailto:", "tel:")):
        return value

    absolute = urljoin(target_origin + "/", value)
    parsed = urlparse(absolute)

    if parsed.netloc == urlparse(target_origin).netloc:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        if parsed.fragment:
            path += "#" + parsed.fragment
        return path

    return value


def rewrite_html(html, target_origin):
    def replace_attribute(match):
        prefix = match.group(1)
        value = match.group(2)
        return prefix + rewrite_url(value, target_origin) + '"'

    html = re.sub(
        r'((?:href|src|action)\s*=\s*")([^"]*)"',
        replace_attribute,
        html,
        flags=re.IGNORECASE,
    )

    def replace_css(match):
        value = match.group(1).strip().strip("'").strip('"')
        return "url(" + rewrite_url(value, target_origin) + ")"

    html = re.sub(
        r'url\(([^)]*)\)',
        replace_css,
        html,
        flags=re.IGNORECASE,
    )

    if "<head" in html.lower() and "<base " not in html.lower():
        html = re.sub(
            r"(<head[^>]*>)",
            r'\1<base href="/">',
            html,
            count=1,
            flags=re.IGNORECASE,
        )

    return html


def proxy_request(target):
    parsed = urlparse(target)
    target_origin = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        key: value
        for key, value in request.headers
        if key.lower() not in {
            "host",
            "content-length",
            "connection",
            "accept-encoding",
        }
    }

    forwarded_cookies = request.cookies.to_dict()
    forwarded_cookies.pop("vpn_target", None)

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=120,
            headers={
                "User-Agent": request.headers.get(
                    "User-Agent",
                    "Mozilla/5.0",
                ),
                **headers,
            },
        ) as client:
            response = client.request(
                request.method,
                target,
                content=request.get_data(),
                params=None,
                cookies=forwarded_cookies,
            )
    except Exception as exc:
        return make_response(f"Proxy error: {exc}", 502)

    content_type = response.headers.get("content-type", "")
    body = response.content

    if "text/html" in content_type:
        text = response.text
        text = rewrite_html(text, target_origin)
        body = text.encode(response.encoding or "utf-8")

    elif "text/css" in content_type:
        text = response.text
        text = re.sub(
            r'url\(([^)]*)\)',
            lambda match: "url(" + rewrite_url(
                match.group(1).strip().strip("'").strip('"'),
                target_origin,
            ) + ")",
            text,
            flags=re.IGNORECASE,
        )
        body = text.encode(response.encoding or "utf-8")

    flask_response = make_response(body, response.status_code)

    excluded = {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "content-type",
    }

    for key, value in response.headers.items():
        if key.lower() not in excluded and key.lower() != "location":
            flask_response.headers[key] = value

    if content_type:
        flask_response.headers["Content-Type"] = content_type

    location = response.headers.get("location")
    if location:
        flask_response.headers["Location"] = rewrite_url(
            location,
            target_origin,
        )

    for cookie in response.headers.get_list("set-cookie"):
        flask_response.headers.add("Set-Cookie", cookie)

    return flask_response


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


@app.route("/<path:path>")
def proxy_path(path):
    target_url = request.cookies.get("vpn_target")

    if not target_url:
        return redirect("/")

    target_url = validate_url(target_url)

    if not target_url:
        return redirect("/")

    base = target_url + "/"
    target = urljoin(base, path)

    if request.query_string:
        target += "?" + request.query_string.decode()

    return proxy_request(target)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
