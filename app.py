import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from playwright.async_api import async_playwright

app = Flask(__name__)

playwright = None
browser = None
page = None
browser_lock = asyncio.Lock()


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


async def get_page():
    global playwright, browser, page

    if browser is None or not browser.is_connected():
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

    return page


async def open_url(url: str):
    current_page = await get_page()

    async with browser_lock:
        await current_page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await current_page.wait_for_timeout(1500)
        return {"url": current_page.url, "html": await current_page.content()}


async def reload_page():
    current_page = await get_page()

    async with browser_lock:
        await current_page.reload(
            wait_until="domcontentloaded",
            timeout=120000,
        )
        await current_page.wait_for_timeout(1500)
        return {"url": current_page.url, "html": await current_page.content()}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/open", methods=["POST"])
def open_site():
    data = request.get_json(silent=True) or {}
    try:
        safe_url = validate_public_url(data.get("url", ""))
        return jsonify(asyncio.run(open_url(safe_url)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/reload", methods=["POST"])
def reload_site():
    try:
        return jsonify(asyncio.run(reload_page()))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
