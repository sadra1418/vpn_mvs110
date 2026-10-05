import ipaddress
import socket
from threading import Lock
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from playwright.sync_api import sync_playwright

app = Flask(__name__)

playwright = None
browser = None
page = None
browser_lock = Lock()


def validate_public_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http:// and https:// URLs are allowed.")

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                parsed.hostname, None, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ValueError("The destination host could not be resolved.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("Private or local network addresses are not allowed.")

    return value


def get_page():
    global playwright, browser, page

    if browser is None or not browser.is_connected():
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

    return page


def open_url(url: str):
    current_page = get_page()

    with browser_lock:
        current_page.goto(url, wait_until="domcontentloaded", timeout=120000)
        current_page.wait_for_timeout(1500)
        return {
            "url": current_page.url,
            "html": current_page.content(),
        }


def reload_page():
    current_page = get_page()

    with browser_lock:
        current_page.reload(wait_until="domcontentloaded", timeout=120000)
        current_page.wait_for_timeout(1500)
        return {
            "url": current_page.url,
            "html": current_page.content(),
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/open", methods=["POST"])
def open_site():
    data = request.get_json(silent=True) or {}

    try:
        safe_url = validate_public_url(data.get("url", ""))
        return jsonify(open_url(safe_url))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/reload", methods=["POST"])
def reload_site():
    try:
        return jsonify(reload_page())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
