# VPN MVS110

A small Flask + Playwright browser relay designed to run in a Docker container on Render.

## Flow

1. The user enters an HTTP/HTTPS website address.
2. Flask starts or reuses a server-side Playwright Chromium browser.
3. Playwright opens the destination website on the Render server.
4. The rendered HTML is returned to the user's browser.
5. The **Reload server page** button calls Flask and reloads the same Playwright page on the server.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
```

## Docker

```bash
docker build -t vpn-mvs110 .
docker run -p 10000:10000 vpn-mvs110
```

## Render

Create a Render Web Service from this repository and choose **Docker** as the runtime. Render will use the included Dockerfile.

## Important limitation

This is an MVP server-side browser relay. Returning `page.content()` does not make every modern website a perfect transparent reverse proxy. Complex sites can depend on their original origin, cookies, service workers, WebSockets, CSP, or client-side navigation.

The current version intentionally allows only public HTTP/HTTPS destinations and blocks private/local IP addresses.
