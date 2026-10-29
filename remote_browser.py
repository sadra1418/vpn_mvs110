import asyncio
import io
import os
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from playwright.async_api import async_playwright

TARGET = "https://www.youtube.com/"
browser = None
pw = None
sessions = {}
session_lock = asyncio.Lock()
MAX_SESSIONS = 3
SESSION_TTL = 900

PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube Remote Browser</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#121212;color:white;font:14px system-ui,sans-serif}
header{height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 15px;background:#202124}
main{position:relative;height:calc(100dvh - 52px);overflow:hidden;display:grid;place-items:center}
#screen{width:100%;height:100%;object-fit:contain;cursor:crosshair;display:block}
#status{font-size:12px;color:#bbb}button{border:0;border-radius:22px;padding:10px 14px;cursor:pointer;background:#eee;color:#111}
#refresh{position:fixed;top:65px;right:16px;z-index:9;box-shadow:0 2px 12px #0008}
#hint{position:fixed;bottom:10px;left:10px;background:#111b;padding:8px;border-radius:8px;font-size:12px}
</style></head>
<body><header><strong>YouTube on Render</strong><span id="status">در حال اتصال…</span></header>
<main><img id="screen" alt="Remote browser screen"></main>
<button id="refresh" aria-label="Refresh the remote browser">↻ رفرش صفحه</button>
<div id="hint">تصویر مرورگر سرور • فعلاً بدون صدا و پخش روان ویدئو</div>
<script>
const screen=document.getElementById('screen'), status=document.getElementById('status');
let busy=false, sid=null, nativeWidth=1280, nativeHeight=720;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function call(path,options={}){
 const r=await fetch(path, {...options,credentials:'same-origin',cache:'no-store'});
 if(!r.ok)throw new Error(await r.text());return r;
}
async function frame(){
 if(!sid || busy)return;
 busy=true;
 try{
  const resp=await call('/api/screenshot');
  nativeWidth=Number(resp.headers.get('x-viewport-width'))||1280;
  nativeHeight=Number(resp.headers.get('x-viewport-height'))||720;
  const blob=await resp.blob(),url=URL.createObjectURL(blob);
  const old=screen.dataset.url;
  screen.src=url;screen.dataset.url=url;if(old)URL.revokeObjectURL(old);
  status.textContent='متصل';
 }catch(e){status.textContent='خطا در دریافت تصویر: '+e.message}
 finally{busy=false}
}
async function start(){
 try{await call('/api/start',{method:'POST'});sid=true;await frame();}
 catch(e){status.textContent='اتصال ناموفق: '+e.message}
}
document.getElementById('refresh').onclick=async()=>{
 status.textContent='در حال رفرش مرورگر سرور…';
 try{await call('/api/reload',{method:'POST'});await frame()}catch(e){status.textContent=e.message}
};
screen.onclick=async e=>{
 if(!sid)return;
 const rect=screen.getBoundingClientRect(),scale=Math.min(rect.width/nativeWidth,rect.height/nativeHeight);
 const w=nativeWidth*scale,h=nativeHeight*scale,left=rect.left+(rect.width-w)/2,top=rect.top+(rect.height-h)/2;
 const x=(e.clientX-left)/scale,y=(e.clientY-top)/scale;
 if(x<0||y<0||x>=nativeWidth||y>=nativeHeight)return;
 try{await call('/api/click',{method:'POST',headers:{'content-type':'application/json'},
 body:JSON.stringify({x:Math.round(x),y:Math.round(y)})});await frame()}
 catch(err){status.textContent=err.message}
};
document.addEventListener('keydown',async e=>{
 if(!sid||e.target.tagName==='INPUT')return;
 if(e.key.length===1 || ['Enter','Backspace','Escape','Tab','ArrowDown','ArrowUp','ArrowLeft','ArrowRight'].includes(e.key)){
  e.preventDefault();
  try{await call('/api/key',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({key:e.key})});await frame()}catch(err){status.textContent=err.message}
 }
});
setInterval(frame,1200);start();
</script></body></html>"""

@asynccontextmanager
async def lifespan(app):
    global pw, browser
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    try:
        yield
    finally:
        for s in list(sessions.values()):
            await s["context"].close()
        await browser.close()
        await pw.stop()

app = FastAPI(lifespan=lifespan)

def get_session(request: Request):
    token = request.cookies.get("browser_sid")
    s = sessions.get(token)
    if not s:
        raise HTTPException(401, "Session expired. Refresh the tab.")
    s["last"] = time.monotonic()
    return s

@app.get("/")
async def index():
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self' blob: 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"})

@app.post("/api/start")
async def start(request: Request):
    now = time.monotonic()
    async with session_lock:
        for token, s in list(sessions.items()):
            if now-s["last"] > SESSION_TTL:
                await s["context"].close()
                del sessions[token]
        existing = request.cookies.get("browser_sid")
        if existing in sessions:
            sessions[existing]["last"] = now
            response = Response(status_code=204)
            return response
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(503, "Server at capacity. Try again later.")
        context = await browser.new_context(viewport={"width":1280,"height":720}, device_scale_factor=1, java_script_enabled=True, accept_downloads=False)
        page = await context.new_page()
        token = secrets.token_urlsafe(32)
        sessions[token] = {"context":context, "page":page,"last":now,"lock":asyncio.Lock()}
    try:
        await page.goto(TARGET, wait_until="domcontentloaded",timeout=30000)
    except Exception:
        pass  # Screenshot will show a browser error page if navigation fails.
    response = Response(status_code=204)
    response.set_cookie("browser_sid", token, httponly=True, secure=request.url.scheme=="https", samesite="lax", max_age=SESSION_TTL)
    return response

@app.get("/api/screenshot")
async def screenshot(request: Request):
    s = get_session(request)
    async with s["lock"]:
        data = await s["page"].screenshot(type="jpeg", quality=62, timeout=15000,animations="disabled")
    return Response(data,media_type="image/jpeg",headers={"Cache-Control":"no-store","X-Viewport-Width":"1280","X-Viewport-Height":"720"})

@app.post("/api/reload")
async def reload(request: Request):
    s=get_session(request)
    async with s["lock"]:
        try:
            await s["page"].reload(wait_until="domcontentloaded",timeout=30000)
        except Exception:
            pass
    return Response(status_code=204)

@app.post("/api/click")
async def click(request: Request):
    s=get_session(request)
    payload=await request.json()
    x,y=payload.get("x"),payload.get("y")
    if not all(isinstance(v,(int,float)) and 0<=v<=1280 for v in (x,y)):
        raise HTTPException(400,"Invalid coordinates")
    async with s["lock"]:
        await s["page"].mouse.click(x,y,timeout=10000)
    return Response(status_code=204)

@app.post("/api/key")
async def key(request: Request):
    s=get_session(request)
    payload=await request.json()
    k=payload.get("key","")
    if not isinstance(k,str) or len(k)>30:
        raise HTTPException(400,"Invalid key")
    allowed={"Enter","Backspace","Escape","Tab","ArrowDown","ArrowUp","ArrowLeft","ArrowRight"}
    if len(k)!=1 and k not in allowed:
        raise HTTPException(400,"Unsupported key")
    async with s["lock"]:
        if len(k)==1: await s["page"].keyboard.insert_text(k)
        else: await s["page"].keyboard.press(k)
    return Response(status_code=204)

@app.get("/health")
async def health():
    return {"status":"ok"}
