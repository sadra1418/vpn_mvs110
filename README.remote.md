# Render YouTube remote browser (experimental)

This is a separate prototype; the existing Flask proxy is unchanged.

## Deploy on Render
1. Create a **Web Service** linked to this branch.
2. Select **Docker** as the runtime.
3. Set **Dockerfile Path** to `Dockerfile.remote`.
4. Wait for deployment and open the service URL.

The remote Chromium browser requests youtube.com from Render. The client receives JPEG screenshots and sends clicks, keystrokes, and reload commands back to the server.

### Limitations and security
* This is a low-frequency screenshot prototype, **not real-time remote desktop**. Video/audio playback is not streamed; YouTube video may be unavailable due to service or cloud restrictions.
* Each browser is isolated in its own context, capped at 3 active sessions. Sessions expire after 15 minutes of inactivity (checked upon new session creation).
* Public deployment can consume substantial CPU/RAM. Put access controls in front of it before using publicly. Avoid signing into personal accounts.
* Navigation is fixed to YouTube at startup; interactions within that site can open other sites. Do not deploy this as a public proxy for arbitrary URL input.
* The UI refresh button runs Playwright `page.reload()` remotely.
