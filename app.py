import html as html_lib
import ipaddress
import json
import re
import socket
import time
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import parse_qs, quote, unquote, urldefrag, urljoin, urlsplit

import httpx
from flask import Flask, Response, make_response, redirect, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import PathConverter

from config import ALLOW_PRIVATE_HOSTS, PORT


class AnyPathConverter(PathConverter):
    # The stock converter refuses a leading "/", but "//host/x" is a valid
    # upstream path that the catch-all route must still receive.
    regex = ".+?"
    part_isolating = False


app = Flask(__name__)
app.url_map.converters["anypath"] = AnyPathConverter
# "//x" is a legitimate upstream path; never let Flask merge it into "/x".
app.url_map.merge_slashes = False
# Honour X-Forwarded-* from the platform's reverse proxy (Render, Fly, nginx...).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SESSION_COOKIE = "vpn_target"
SESSION_MAX_AGE = 7 * 24 * 3600
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

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
            direction: ltr;
        }
        button {
            padding: 12px 24px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <h1>آدرس سایت مقصد را وارد کنید</h1>
    <form method="post" action="/__connect">
        <input
            name="target_url"
            type="text"
            inputmode="url"
            autocomplete="url"
            autocapitalize="none"
            spellcheck="false"
            placeholder="https://example.com"
            required
        >
        <button type="submit">اتصال</button>
    </form>
</body>
</html>
"""

# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

SKIP_PREFIXES = (
    "#", "data:", "javascript:", "mailto:", "tel:", "sms:", "blob:", "about:",
)
PATH_SAFE = "/%:@!$&+,;=~-._*"
QUERY_SAFE = PATH_SAFE + "?"


def validate_url(value):
    """Return the stripped URL if it is a usable absolute http(s) URL."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        parts.port  # raises ValueError for an invalid port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return value


def normalize_user_url(value):
    """Accept 'example.com' typed without a scheme."""
    value = (value or "").strip()
    if not value:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value
    return validate_url(value)


def origin_of(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    port = parts.port
    suffix = ":" + str(port) if port and port != default_port else ""
    return parts.scheme + "://" + host + suffix


def plain_location(url):
    """Path form of an upstream URL, or None when it cannot be a plain path."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if path.startswith("//"):
        return None
    out = quote(path, safe=PATH_SAFE)
    if parts.query:
        out += "?" + quote(parts.query, safe=QUERY_SAFE)
    return out


def to_proxy(absolute, origin):
    """
    URLs on the current session origin become plain paths (so single-page apps
    keep a real location.pathname); everything else goes through /proxy.
    """
    parts = urlsplit(absolute)
    fragment = "#" + quote(parts.fragment, safe=QUERY_SAFE) if parts.fragment else ""
    if origin and origin_of(absolute) == origin:
        local = plain_location(absolute)
        if local is not None:
            return local + fragment
    upstream = urldefrag(absolute)[0]
    return "/proxy?url=" + quote(upstream, safe="") + fragment


def rewrite_url(value, base_url, origin):
    if not value:
        return value
    stripped = value.strip()
    lowered = stripped.lower()
    if (
        not stripped
        or lowered.startswith(SKIP_PREFIXES)
        or stripped[0] in "\\'\""
        or "{{" in stripped
        or "${" in stripped
        or "<%" in stripped
    ):
        return value
    try:
        absolute = urljoin(base_url, stripped)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return value
        return to_proxy(absolute, origin)
    except ValueError:
        return value


def rewrite_srcset(value, base_url, origin):
    """Rewrite 'url 1x, url2 2x' lists (URLs may contain commas)."""
    out = []
    i, n = 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not value[j].isspace():
            j += 1
        url = value[i:j]
        i = j
        descriptor = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            k = value.find(",", i)
            if k == -1:
                k = n
            descriptor = value[i:k].strip()
            i = k
        if url:
            item = rewrite_url(url, base_url, origin)
            out.append(item + (" " + descriptor if descriptor else ""))
    return ", ".join(out)


# --------------------------------------------------------------------------
# CSS / HTML rewriting
# --------------------------------------------------------------------------

CSS_URL_RE = re.compile(
    r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s'"]*))\s*\)""", re.IGNORECASE
)
CSS_IMPORT_RE = re.compile(
    r"""(@import\s+)(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE
)


def rewrite_css(css, base_url, origin):
    def url_repl(match):
        double, single, bare = match.groups()
        if double is not None:
            value, quote_char = double, '"'
        elif single is not None:
            value, quote_char = single, "'"
        else:
            value, quote_char = bare or "", ""
        new = rewrite_url(value, base_url, origin)
        if new == value:
            return match.group(0)
        return "url(" + quote_char + new + quote_char + ")"

    def import_repl(match):
        if match.group(2) is not None:
            value, quote_char = match.group(2), '"'
        else:
            value, quote_char = match.group(3), "'"
        new = rewrite_url(value, base_url, origin)
        if new == value:
            return match.group(0)
        return match.group(1) + quote_char + new + quote_char

    css = CSS_URL_RE.sub(url_repl, css)
    return CSS_IMPORT_RE.sub(import_repl, css)


_ATTRS = r"""(?:"[^"]*"|'[^']*'|[^'">])*"""
HTML_TOKEN_RE = re.compile(
    r"<script\b(?P<sattrs>" + _ATTRS + r")>(?P<sbody>.*?)</script\s*>"
    r"|<style\b(?P<tattrs>" + _ATTRS + r")>(?P<tbody>.*?)</style\s*>"
    r"|<(?P<tag>[a-zA-Z][^\s/>]*)(?P<attrs>" + _ATTRS + r")>",
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(
    r"""(?P<name>[^\s"'<>/=]+)(?P<eq>\s*=\s*)"""
    r"""(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'>]+))"""
)
BASE_HREF_RE = re.compile(
    r"""<base\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
REFRESH_RE = re.compile(
    r"""^(\s*[\d.]+\s*[;,]\s*(?:url\s*=\s*)?)(['"]?)(.*?)\2\s*$""",
    re.IGNORECASE | re.DOTALL,
)
URL_ATTRS = {
    "href", "src", "action", "formaction", "poster", "background",
    "data-src", "data-original", "data-lazy-src",
}
SRCSET_ATTRS = {"srcset", "data-srcset", "imagesrcset"}


def rewrite_attrs(tag, attrs, base_url, origin):
    is_refresh = tag == "meta" and re.search(
        r"""http-equiv\s*=\s*["']?\s*refresh""", attrs, re.IGNORECASE
    )

    def replace(match):
        name = match.group("name").lower().rsplit(":", 1)[-1]
        for group in ("dq", "sq", "uq"):
            if match.group(group) is not None:
                raw = match.group(group)
                break

        # Subresource Integrity hashes no longer match once we rewrite a file.
        if name == "integrity":
            return ""

        value = html_lib.unescape(raw)
        if name in URL_ATTRS or (name == "data" and tag == "object"):
            new = rewrite_url(value, base_url, origin)
        elif name in SRCSET_ATTRS:
            new = rewrite_srcset(value, base_url, origin)
        elif name == "style":
            new = rewrite_css(value, base_url, origin)
        elif name == "content" and is_refresh:
            refresh = REFRESH_RE.match(value)
            new = value
            if refresh:
                new = (
                    refresh.group(1)
                    + rewrite_url(refresh.group(3), base_url, origin)
                )
        else:
            return match.group(0)

        if new == value:
            return match.group(0)
        return (
            match.group("name") + match.group("eq")
            + '"' + html_lib.escape(new, quote=True) + '"'
        )

    return ATTR_RE.sub(replace, attrs)


def rewrite_html(text, current_url, origin):
    base_url = current_url
    base = BASE_HREF_RE.search(text)
    if base:
        href = next(g for g in base.groups() if g is not None)
        base_url = urljoin(current_url, html_lib.unescape(href))
    text = BASE_TAG_RE.sub("", text)

    def replace(match):
        if match.group("sattrs") is not None:
            attrs = rewrite_attrs("script", match.group("sattrs"), base_url, origin)
            return "<script" + attrs + ">" + match.group("sbody") + "</script>"

        if match.group("tattrs") is not None:
            attrs = rewrite_attrs("style", match.group("tattrs"), base_url, origin)
            body = rewrite_css(match.group("tbody"), base_url, origin)
            return "<style" + attrs + ">" + body + "</style>"

        tag = match.group("tag").lower()
        attrs = match.group("attrs")
        if tag == "meta" and re.search(
            r"""http-equiv\s*=\s*["']?\s*content-security-policy""",
            attrs,
            re.IGNORECASE,
        ):
            return ""
        return "<" + match.group("tag") + rewrite_attrs(
            tag, attrs, base_url, origin
        ) + ">"

    return HTML_TOKEN_RE.sub(replace, text)


# --------------------------------------------------------------------------
# Script and widget injected into proxied pages
# --------------------------------------------------------------------------

CLIENT_SCRIPT = r"""
(function () {
  if (window.__vpnPatched) { return; }
  window.__vpnPatched = true;
  var SESSION_ORIGIN = __VPN_ORIGIN__;
  var HOST = location.origin;
  var SKIP = /^(?:data:|blob:|javascript:|mailto:|tel:|about:|#)/i;

  function px(input) {
    try {
      if (input === null || input === undefined) { return input; }
      var raw = String(input);
      if (SKIP.test(raw)) { return input; }
      var u = new URL(raw, location.href);
      if (u.protocol !== "http:" && u.protocol !== "https:") { return input; }
      if (u.origin === HOST) { return input; }
      var hash = u.hash;
      u.hash = "";
      if (SESSION_ORIGIN && u.origin === SESSION_ORIGIN) {
        var local = u.pathname + u.search;
        if (local.indexOf("//") !== 0) { return local + hash; }
      }
      return "/proxy?url=" + encodeURIComponent(u.href) + hash;
    } catch (e) { return input; }
  }

  if (window.fetch) {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (typeof Request !== "undefined" && input instanceof Request) {
          var changed = px(input.url);
          if (changed !== input.url) { input = new Request(changed, input); }
        } else {
          input = px(input);
        }
      } catch (e) {}
      return nativeFetch.call(this, input, init);
    };
  }

  var nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var args = Array.prototype.slice.call(arguments);
    args[1] = px(url);
    return nativeOpen.apply(this, args);
  };

  if (navigator.sendBeacon) {
    var nativeBeacon = navigator.sendBeacon;
    navigator.sendBeacon = function (url, data) {
      return nativeBeacon.call(navigator, px(url), data);
    };
  }

  var nativeWindowOpen = window.open;
  window.open = function (url) {
    var args = Array.prototype.slice.call(arguments);
    if (url) { args[0] = px(url); }
    return nativeWindowOpen.apply(window, args);
  };

  ["pushState", "replaceState"].forEach(function (name) {
    var original = history[name];
    history[name] = function (state, title, url) {
      var next = (url === undefined || url === null) ? url : px(url);
      return original.call(history, state, title, next);
    };
  });

  var URL_ATTRS = { src: 1, href: 1, action: 1, formaction: 1, poster: 1, data: 1 };
  var nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    try {
      var key = String(name).toLowerCase();
      if (URL_ATTRS[key] === 1 && typeof value === "string" &&
          this.tagName !== "BASE" && (key !== "data" || this.tagName === "OBJECT")) {
        value = px(value);
      }
    } catch (e) {}
    return nativeSetAttribute.call(this, name, value);
  };

  function hook(ctor, prop) {
    try {
      var proto = window[ctor] && window[ctor].prototype;
      if (!proto) { return; }
      var d = Object.getOwnPropertyDescriptor(proto, prop);
      if (!d || !d.set || !d.configurable) { return; }
      Object.defineProperty(proto, prop, {
        configurable: true,
        enumerable: d.enumerable,
        get: d.get,
        set: function (v) { d.set.call(this, px(v)); }
      });
    } catch (e) {}
  }
  [
    ["HTMLScriptElement", "src"], ["HTMLImageElement", "src"],
    ["HTMLIFrameElement", "src"], ["HTMLSourceElement", "src"],
    ["HTMLMediaElement", "src"], ["HTMLEmbedElement", "src"],
    ["HTMLTrackElement", "src"], ["HTMLInputElement", "src"],
    ["HTMLVideoElement", "poster"], ["HTMLLinkElement", "href"],
    ["HTMLAnchorElement", "href"], ["HTMLFormElement", "action"]
  ].forEach(function (pair) { hook(pair[0], pair[1]); });

  // A service worker registered here would be shared by every proxied site.
  try {
    if (navigator.serviceWorker) {
      navigator.serviceWorker.register = function () {
        return Promise.reject(new Error("Service workers are disabled by the proxy"));
      };
    }
  } catch (e) {}

  // A GET form whose action is /proxy?url=... would lose that query string.
  document.addEventListener("submit", function (event) {
    try {
      var form = event.target;
      if (!form || form.tagName !== "FORM" || event.defaultPrevented) { return; }
      var submitter = event.submitter;
      var method = ((submitter && submitter.getAttribute("formmethod")) ||
                    form.getAttribute("method") || "get").toLowerCase();
      var action = (submitter && submitter.getAttribute("formaction")) ||
                   form.getAttribute("action") || "";
      var marker = "/proxy?url=";
      if (method !== "get" || action.indexOf(marker) !== 0) { return; }
      var encoded = action.slice(marker.length).split("#")[0];
      var target = new URL(decodeURIComponent(encoded));
      var data;
      try { data = new FormData(form, submitter || undefined); }
      catch (e) { data = new FormData(form); }
      var params = new URLSearchParams();
      data.forEach(function (v, k) { if (typeof v === "string") { params.append(k, v); } });
      target.search = params.toString();
      event.preventDefault();
      location.href = px(target.href);
    } catch (e) {}
  }, true);
})();
"""

WIDGET = """
<style id="vpn-widget-style">
#vpn-widget {
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 2147483647;
    display: flex;
    gap: 8px;
    font-family: sans-serif;
}
#vpn-widget button, #vpn-widget a {
    border: 0;
    border-radius: 10px;
    padding: 10px 15px;
    background: #111;
    color: #fff;
    font-size: 14px;
    cursor: pointer;
    text-decoration: none;
    box-shadow: 0 3px 12px rgba(0,0,0,.25);
}
#vpn-widget button:active, #vpn-widget a:active {
    transform: scale(.96);
}
</style>
<div id="vpn-widget">
    <a href="/__home">⌂ خانه</a>
    <button id="vpn-reload-button" type="button"
            onclick="window.location.reload()">↻ ریلود</button>
</div>
"""

HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def inject(text, origin, with_script, with_widget):
    if with_script:
        script = "<script>" + CLIENT_SCRIPT.replace(
            "__VPN_ORIGIN__", json.dumps(origin).replace("</", "<\\/")
        ) + "</script>"
        for pattern in (HEAD_OPEN_RE, HTML_OPEN_RE, DOCTYPE_RE):
            match = pattern.search(text)
            if match:
                text = text[:match.end()] + script + text[match.end():]
                break
        else:
            text = script + text

    if with_widget:
        index = text.lower().rfind("</body>")
        if index == -1:
            text += WIDGET
        else:
            text = text[:index] + WIDGET + text[index:]
    return text


# --------------------------------------------------------------------------
# Request / response plumbing
# --------------------------------------------------------------------------

REQUEST_HEADERS_BLOCKED = {
    "host", "content-length", "connection", "keep-alive", "accept-encoding",
    "cookie", "te", "trailer", "transfer-encoding", "upgrade",
    "proxy-authorization", "proxy-connection", "forwarded", "via",
    "origin", "referer",
}
REQUEST_HEADER_PREFIXES = (
    "x-forwarded-", "x-real-ip", "x-request-id", "x-render-", "render-",
    "cf-", "fly-", "sec-fetch-", "true-client-ip",
)
RESPONSE_HEADERS_BLOCKED = {
    "content-encoding", "transfer-encoding", "connection", "keep-alive",
    "content-length", "content-type", "location", "set-cookie", "refresh",
    "date", "server",
    # Policies that describe the upstream origin and break the page once it
    # is served from the proxy origin.
    "content-security-policy", "content-security-policy-report-only",
    "x-content-security-policy", "x-webkit-csp", "x-frame-options",
    "strict-transport-security", "cross-origin-opener-policy",
    "cross-origin-embedder-policy", "cross-origin-resource-policy",
    "clear-site-data", "public-key-pins", "report-to", "nel", "alt-svc",
    # `Link: <https://...>; rel=preload` would make the browser bypass us.
    "link", "content-location",
    # Digests and ranges are invalid for a rewritten body.
    "content-md5", "digest", "content-digest", "repr-digest",
}
REWRITTEN_BODY_BLOCKED = {"accept-ranges", "content-range"}
META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.IGNORECASE
)
HTML_TYPES = ("text/html", "application/xhtml+xml")


def build_client():
    client = httpx.Client(
        follow_redirects=False,
        http2=False,
        timeout=httpx.Timeout(connect=20, read=120, write=120, pool=30),
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=30,
        ),
    )
    # One client is shared by all visitors to reuse connections, so it must
    # never remember upstream cookies. They go to the browser instead.
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return client


CLIENT = build_client()


def text_response(message, status):
    return Response(
        message, status=status, content_type="text/plain; charset=utf-8"
    )


def session_origin():
    value = validate_url(request.cookies.get(SESSION_COOKIE))
    return origin_of(value) if value else None


def set_session_cookie(response, origin):
    response.set_cookie(
        SESSION_COOKIE,
        origin,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
        path="/",
    )


def parse_proxy_url(url):
    """Upstream URL behind a '/proxy?url=...' URL of this server, else None."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.netloc.lower() != request.host.lower():
        return None
    if parts.path not in ("/proxy", "/url"):
        return None
    if parts.query.startswith("url="):
        return validate_url(unquote(parts.query[4:]))
    values = parse_qs(parts.query).get("url")
    return validate_url(values[0]) if values else None


def upstream_referer():
    """Translate the browser's Referer (a proxy URL) to the real upstream URL."""
    referer = request.headers.get("Referer")
    if not referer:
        return None
    explicit = parse_proxy_url(referer)
    if explicit:
        return urldefrag(explicit)[0]
    parts = urlsplit(referer)
    origin = session_origin()
    if origin and parts.netloc.lower() == request.host.lower():
        return origin + (parts.path or "/") + (
            "?" + parts.query if parts.query else ""
        )
    return None


def request_url_param():
    raw = request.query_string.decode("utf-8", errors="replace")
    if raw.startswith("url="):
        # Everything after "url=" is the target, even if it was not encoded.
        return validate_url(unquote(raw[4:]))
    return validate_url(request.args.get("url", ""))


def raw_path_and_query():
    """Path exactly as the browser sent it (no decoding of %2F, %3F, ...)."""
    raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
    if raw:
        path, _, query = raw.partition("?")
        path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", path) or "/"
        return path, query.split("#", 1)[0]
    return (
        quote(request.path, safe="/%:@!$&'()*+,;=~-._"),
        request.query_string.decode("latin-1"),
    )


def request_kind():
    dest = request.headers.get("Sec-Fetch-Dest")
    if dest:
        return dest
    accept = request.headers.get("Accept", "")
    if "text/html" in accept and not request.headers.get("X-Requested-With"):
        return "document"
    return "empty"


def is_top_level_navigation():
    return (
        request.method == "GET"
        and request.headers.get("Sec-Fetch-Dest") == "document"
    )


_dns_cache = {}


def address_is_public(address):
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_global


def host_is_blocked(hostname):
    """Block loopback / private / link-local targets (SSRF protection)."""
    if ALLOW_PRIVATE_HOSTS:
        return False

    try:
        return not address_is_public(ipaddress.ip_address(hostname.strip("[]")))
    except ValueError:
        pass  # not an IP literal: resolve the name below

    now = time.monotonic()
    cached = _dns_cache.get(hostname)
    if cached and now - cached[0] < 300:
        return cached[1]
    blocked = False
    try:
        for info in socket.getaddrinfo(hostname, None):
            address = ipaddress.ip_address(info[4][0].split("%")[0])
            if not address_is_public(address):
                blocked = True
                break
    except (socket.gaierror, ValueError, UnicodeError):
        blocked = False  # unresolved names fail later with a clear error
    if len(_dns_cache) > 1024:
        _dns_cache.clear()
    _dns_cache[hostname] = (now, blocked)
    return blocked


def check_target(url):
    hostname = urlsplit(url).hostname or ""
    if hostname == request.host.split(":")[0].lower():
        return text_response("آدرس مقصد همین سرور است.", 400)
    if host_is_blocked(hostname):
        return text_response("دسترسی به این آدرس مجاز نیست.", 403)
    return None


def upstream_headers(target):
    headers = {}
    for key, value in request.headers:
        lowered = key.lower()
        if (
            not value
            or lowered in REQUEST_HEADERS_BLOCKED
            or lowered.startswith(REQUEST_HEADER_PREFIXES)
        ):
            continue
        headers[key] = value

    # Origin and Referer must describe the upstream site, not this proxy.
    origin_header = request.headers.get("Origin")
    if origin_header:
        headers["Origin"] = (
            origin_header if origin_header == "null" else origin_of(target)
        )
    referer = upstream_referer()
    if referer:
        headers["Referer"] = referer

    # Forward the visitor's cookies, minus our own session marker.
    cookies = [
        part.strip()
        for part in request.headers.get("Cookie", "").split(";")
        if part.strip()
        and part.split("=", 1)[0].strip() != SESSION_COOKIE
    ]
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    # HTML/CSS are decoded and rewritten here. Ranges only make sense on the
    # untouched bytes, so ask for identity encoding in that case.
    if "Range" in request.headers or "If-Range" in request.headers:
        headers["Accept-Encoding"] = "identity"
    else:
        headers["Accept-Encoding"] = "gzip, deflate"
    return headers


def rewrite_set_cookie(cookie):
    parts = [part.strip() for part in cookie.split(";")]
    attributes = []
    for part in parts[1:]:
        key, _, value = part.partition("=")
        key = key.strip().lower()
        if key in ("domain", "partitioned"):
            continue
        if key == "secure" and not request.is_secure:
            continue
        if key == "samesite" and value.strip().lower() == "none":
            part = "SameSite=None" if request.is_secure else "SameSite=Lax"
        attributes.append(part)
    return "; ".join([parts[0]] + attributes)


def decode_body(body, declared, sniff_html):
    candidates = []
    if body.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if declared:
        candidates.append(declared)
    if sniff_html:
        match = META_CHARSET_RE.search(body[:4096])
        if match:
            candidates.append(match.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def apply_headers(upstream, response, target, origin, modified):
    for key, value in upstream.headers.multi_items():
        lowered = key.lower()
        if lowered in RESPONSE_HEADERS_BLOCKED:
            continue
        if modified and lowered in REWRITTEN_BODY_BLOCKED:
            continue
        response.headers.add(key, value)

    location = upstream.headers.get("location")
    if location:
        response.headers["Location"] = rewrite_url(location, target, origin)

    refresh = upstream.headers.get("refresh")
    if refresh:
        match = REFRESH_RE.match(refresh)
        response.headers["Refresh"] = (
            match.group(1) + rewrite_url(match.group(3), target, origin)
            if match else refresh
        )

    for cookie in upstream.headers.get_list("set-cookie"):
        if cookie.split("=", 1)[0].strip() == SESSION_COOKIE:
            continue
        response.headers.add("Set-Cookie", rewrite_set_cookie(cookie))


def stream_body(upstream):
    try:
        for chunk in upstream.iter_bytes(64 * 1024):
            yield chunk
    except httpx.HTTPError as exc:
        app.logger.warning("Upstream stream interrupted: %s", exc)
    finally:
        upstream.close()


def proxy_request(target, origin=None):
    target = validate_url(target)
    if not target:
        return text_response("آدرس مقصد معتبر نیست.", 400)
    target = urldefrag(target)[0]

    problem = check_target(target)
    if problem is not None:
        return problem

    if origin is None:
        origin = session_origin()

    body = request.get_data()
    try:
        upstream_request = CLIENT.build_request(
            request.method,
            target,
            headers=upstream_headers(target),
            content=body or None,
        )
        upstream = CLIENT.send(upstream_request, stream=True)
    except httpx.TimeoutException:
        return text_response("زمان پاسخ‌گویی سایت مقصد تمام شد.", 504)
    except Exception as exc:  # DNS, TLS, connection reset, bad URL...
        app.logger.warning("Proxy error for %s: %s", target, exc)
        return text_response(
            "خطا در اتصال به سایت مقصد: " + str(exc)[:200], 502
        )

    status = upstream.status_code
    content_type = upstream.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    has_body = request.method != "HEAD" and status not in (204, 304)

    if has_body and (media_type in HTML_TYPES or media_type == "text/css"):
        try:
            raw = upstream.read()
        except Exception as exc:
            app.logger.warning("Reading %s failed: %s", target, exc)
            return text_response("دریافت پاسخ از سایت مقصد ناقص ماند.", 502)
        finally:
            upstream.close()

        is_html = media_type in HTML_TYPES
        text = decode_body(raw, upstream.charset_encoding, is_html)
        if is_html:
            kind = request_kind()
            text = rewrite_html(text, target, origin)
            text = inject(
                text,
                origin or "",
                with_script=kind in ("document", "iframe", "frame"),
                with_widget=kind == "document",
            )
            out_type = "text/html; charset=utf-8"
        else:
            text = rewrite_css(text, target, origin)
            out_type = "text/css; charset=utf-8"

        response = make_response(text.encode("utf-8"), status)
        apply_headers(upstream, response, target, origin, modified=True)
        response.headers["Content-Type"] = out_type
        if is_html:
            vary = response.headers.get("Vary")
            response.headers["Vary"] = (
                (vary + ", " if vary else "") + "Cookie, Sec-Fetch-Dest"
            )
        return response

    if not has_body:
        upstream.close()
        response = Response(b"", status=status)
        apply_headers(upstream, response, target, origin, modified=False)
        if request.method == "HEAD" and upstream.headers.get("content-length"):
            response.headers["Content-Length"] = upstream.headers["content-length"]
        if content_type:
            response.headers["Content-Type"] = content_type
        return response

    response = Response(stream_body(upstream), status=status)
    apply_headers(upstream, response, target, origin, modified=False)
    if content_type:
        response.headers["Content-Type"] = content_type
    # Without a Content-Encoding the length is unchanged; keeping it lets
    # browsers show progress and seek in media files.
    if (
        upstream.headers.get("content-length")
        and not upstream.headers.get("content-encoding")
    ):
        response.headers["Content-Length"] = upstream.headers["content-length"]
    return response


def start_session(target):
    """Remember the site and move the browser onto its real paths."""
    location = plain_location(target)
    response = redirect(location or "/proxy?url=" + quote(target, safe=""))
    set_session_cookie(response, origin_of(target))
    return response


def proxy_session_request():
    origin = session_origin()
    # Requests from a page that was itself loaded through /proxy?url=... (for
    # example an iframe) belong to that page's origin.
    explicit = parse_proxy_url(request.headers.get("Referer"))
    base_origin = origin_of(explicit) if explicit else origin
    if not base_origin:
        return None

    path, query = raw_path_and_query()
    target = base_origin + path + ("?" + query if query else "")
    return proxy_request(target, origin=origin)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/", methods=ALL_METHODS)
def index():
    if request.method == "POST" and not session_origin():
        # Legacy form posting target_url straight to "/".
        target = normalize_user_url(request.form.get("target_url", ""))
        if target:
            return start_session(target)

    response = proxy_session_request()
    if response is not None:
        return response

    response = make_response(HOME_PAGE)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/__home")
def home():
    response = make_response(HOME_PAGE)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/__connect", methods=["GET", "POST"])
def connect():
    if request.method != "POST":
        return redirect("/__home")
    target = normalize_user_url(request.form.get("target_url", ""))
    if not target:
        return text_response("آدرس واردشده معتبر نیست.", 400)
    problem = check_target(target)
    if problem is not None:
        return problem
    return start_session(target)


@app.route("/url")
def open_url():
    target = request_url_param()
    if not target:
        return text_response("آدرس واردشده معتبر نیست.", 400)
    problem = check_target(target)
    if problem is not None:
        return problem
    return start_session(target)


@app.route("/proxy", methods=ALL_METHODS)
def proxy_external():
    target = request_url_param()
    if not target:
        return text_response("آدرس مقصد معتبر نیست.", 400)

    if not is_top_level_navigation():
        return proxy_request(target)

    # A link to another site was clicked: make that site the active session
    # and continue on its real path so single-page apps route correctly.
    problem = check_target(target)
    if problem is not None:
        return problem
    location = plain_location(urldefrag(target)[0])
    if location is not None:
        return start_session(urldefrag(target)[0])

    response = proxy_request(target, origin=origin_of(target))
    set_session_cookie(response, origin_of(target))
    return response


@app.route("/<anypath:path>", methods=ALL_METHODS)
def proxy_path(path):
    response = proxy_session_request()
    if response is not None:
        return response
    return redirect("/")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
