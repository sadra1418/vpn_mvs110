from urllib.parse import urljoin, urlparse, quote, unquote
import re
from threading import RLock

import httpx
from flask import Flask, make_response, redirect, request
from config import PORT, TARGET_URL
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# Reuse one Chromium process; browser operations are serialized for safety.
playwright_instance = None
browser_instance = None
browser_lock = RLock()


def get_browser():
    global playwright_instance, browser_instance
    if browser_instance is None or not browser_instance.is_connected():
        playwright_instance = sync_playwright().start()
        browser_instance = playwright_instance.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
    return browser_instance


def render_with_playwright(target_url):
    """Load HTML in Chromium so client-side JavaScript can render before proxying."""
    browser = get_browser()
    context = browser.new_context(java_script_enabled=True)
    try:
        page = context.new_page()
        response = page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
        try:
            initial_text = (page.locator("body").inner_text(timeout=1000) or "").strip()
        except Exception:
            initial_text = ""
        if len(initial_text) < 120:
            try:
                page.wait_for_function(
                    "document.body && document.body.innerText.trim().length >= 120",
                    timeout=4000,
                )
            except Exception:
                pass
        return page.content(), page.url, (response.status if response else 200)
    finally:
        context.close()


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
    return (value or "").strip()


def proxy_url(target):
    """Create a local proxy URL for an absolute upstream URL."""
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return target
    # Fragments are browser-side only; keep them after the proxy URL.
    fragment = ("#" + parsed.fragment) if parsed.fragment else ""
    upstream = parsed._replace(fragment="").geturl()
    return "/proxy?url=" + quote(upstream, safe="") + fragment


def rewrite_url(value, current_url):
    """Rewrite an HTML/CSS URL so the browser requests it through this proxy."""
    if not value:
        return value

    value = value.strip()
    if value.startswith(("#", "data:", "javascript:", "mailto:", "tel:", "blob:")):
        return value

    absolute = urljoin(current_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return value

    return proxy_url(absolute)


def rewrite_html(html, current_url):
    # Rewrite URL-bearing attributes, including single-quoted and unquoted values.
    url_attributes = (
        "href", "src", "action", "formaction", "poster", "data"
    )
    attribute_pattern = (
        r'(\b(?:' + "|".join(url_attributes) + r')\s*=\s*)'
        r'(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))'
    )

    def replace_attribute(match):
        prefix = match.group(1)
        value = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else match.group(4)
        )
        rewritten = rewrite_url(value, current_url)
        quote_char = '"' if match.group(2) is not None else (
            "'" if match.group(3) is not None else ""
        )
        return prefix + quote_char + rewritten + quote_char

    html = re.sub(attribute_pattern, replace_attribute, html, flags=re.IGNORECASE)

    # Rewrite CSS resources relative to the stylesheet/document that declared them.
    def replace_css(match):
        raw_value = match.group(1).strip()
        quote_char = raw_value[0] if raw_value.startswith(("'", '"')) else ""
        value = raw_value.strip("'").strip('"')
        rewritten = rewrite_url(value, current_url)
        return "url(" + quote_char + rewritten + quote_char + ")"

    html = re.sub(r"url\(([^)]*)\)", replace_css, html, flags=re.IGNORECASE)

    # Keep browser-generated relative requests on this host. The catch-all route
    # forwards them to the selected target origin.
    if "<head" in html.lower():
        html = re.sub(
            r"<base\b[^>]*>",
            "",
            html,
            flags=re.IGNORECASE,
        )
        html = re.sub(
            r"(<head[^>]*>)",
            r'\1<base href="/">',
            html,
            count=1,
            flags=re.IGNORECASE,
        )

    return html


def needs_browser_render(html):
    """Detect an almost-empty JavaScript application shell."""
    lowered = html.lower()
    markers = (
        'id="root"', "id='root'", 'id="app"', "id='app'",
        'id="__next"', "id='__next'", 'id="svelte"', "id='svelte'",
        "data-reactroot",
    )
    if not any(marker in lowered for marker in markers):
        return False
    visible = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(
        r"&(?:nbsp|amp|lt|gt|quot);",
        " ",
        visible,
        flags=re.IGNORECASE,
    )
    visible = re.sub(r"\s+", " ", visible).strip()
    return len(visible) < 350


def proxy_request(target):
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return make_response("Invalid target URL", 400)

    target_origin = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        key: value
        for key, value in request.headers
        if key.lower() not in {
            "host", "content-length", "connection", "accept-encoding",
            "cookie", "origin", "referer",
        }
    }

    # Origin and Referer must describe the upstream site, not this proxy.
    if request.headers.get("Origin"):
        headers["Origin"] = target_origin
    if request.headers.get("Referer"):
        headers["Referer"] = target

    forwarded_cookies = request.cookies.to_dict()
    forwarded_cookies.pop("vpn_target", None)

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=120,
            headers={
                "User-Agent": request.headers.get("User-Agent", "Mozilla/5.0"),
                **headers,
            },
        ) as client:
            response = client.request(
                request.method,
                target,
                content=request.get_data(),
                cookies=forwarded_cookies,
            )
    except Exception as exc:
        return make_response(f"Proxy error: {exc}", 502)

    content_type = response.headers.get("content-type", "")
    body = response.content
    response_status = response.status_code

    if "text/html" in content_type:
        original_html = response.text
        if request.method == "GET" and needs_browser_render(original_html):
            try:
                with browser_lock:
                    rendered_html, final_url, browser_status = render_with_playwright(target)
                response_status = browser_status
                body = rewrite_html(rendered_html, final_url).encode("utf-8")
            except Exception as exc:
                body = rewrite_html(original_html, target).encode(
                    response.encoding or "utf-8"
                )
                app.logger.warning(
                    "Playwright rendering failed for %s: %s", target, exc
                )
        else:
            body = rewrite_html(original_html, target).encode(
                response.encoding or "utf-8"
            )

    elif "text/css" in content_type:
        text = response.text
        text = re.sub(
            r"url\(([^)]*)\)",
            lambda match: "url(" + rewrite_url(
                match.group(1).strip().strip("'").strip('"'), target
            ) + ")",
            text,
            flags=re.IGNORECASE,
        )
        body = text.encode(response.encoding or "utf-8")

    flask_response = make_response(body, response_status)

    excluded = {
        "content-length", "content-encoding", "transfer-encoding",
        "connection", "content-type", "location", "set-cookie",
    }
    for key, value in response.headers.items():
        if key.lower() not in excluded:
            flask_response.headers[key] = value

    if content_type:
        flask_response.headers["Content-Type"] = content_type

    # Redirects must be resolved against the actual upstream URL.
    location = response.headers.get("location")
    if location:
        flask_response.headers["Location"] = rewrite_url(location, target)

    # Preserve upstream cookies, but remove Domain so the browser stores them
    # for the proxy host and sends them back on subsequent proxied requests.
    for cookie in response.headers.get_list("set-cookie"):
        cookie = re.sub(r";\s*domain=[^;]+", "", cookie, flags=re.IGNORECASE)
        flask_response.headers.add("Set-Cookie", cookie)

    return flask_response


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        target_url = validate_url(request.form.get("target_url", ""))
        if not target_url:
            return "آدرس واردشده معتبر نیست.", 400

        response = redirect("/proxy?url=" + quote(target_url, safe=""))
        parsed = urlparse(target_url)
        target_origin = f"{parsed.scheme}://{parsed.netloc}"
        response.set_cookie(
            "vpn_target",
            target_origin,
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

    response = proxy_request(target_url)
    parsed = urlparse(target_url)
    response.set_cookie(
        "vpn_target",
        f"{parsed.scheme}://{parsed.netloc}",
        max_age=86400,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response


@app.route("/proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def proxy_absolute_url():
    target_url = validate_url(request.args.get("url", ""))
    if not target_url:
        return "آدرس مقصد معتبر نیست.", 400

    response = proxy_request(target_url)
    parsed = urlparse(target_url)
    response.set_cookie(
        "vpn_target",
        f"{parsed.scheme}://{parsed.netloc}",
        max_age=86400,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response


@app.route(
    "/<path:path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
def proxy_path(path):
    target_origin = request.cookies.get("vpn_target")
    if not target_origin:
        return redirect("/")

    target_origin = validate_url(target_origin)
    if not target_origin:
        return redirect("/")

    target = urljoin(target_origin.rstrip("/") + "/", path)
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", errors="replace")

    return proxy_request(target)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
