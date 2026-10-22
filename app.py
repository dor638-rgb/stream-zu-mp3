from __future__ import annotations
import hmac, hashlib, ipaddress, os, socket, subprocess, tempfile, threading, time, uuid
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends, Request as HttpRequest
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import Literal

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
basic = HTTPBasic(auto_error=False)
USERNAME = os.environ.get("APP_USERNAME", "stream")
PASSWORD = os.environ.get("APP_PASSWORD", "")
lock = threading.RLock()
jobs = {}
workdir = Path(tempfile.mkdtemp(prefix="stream-mp3-"))

class Request(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    quality: Literal[128, 192, 256, 320] = 192
    minutes: int = Field(default=5, ge=1, le=30)
    name: str = Field(default="Aufnahme", max_length=80)


LOGIN_HTML = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anmelden · StreamZuMP3</title><style>
*{box-sizing:border-box}body{background:#0b111b;color:#eaf5f8;font:16px system-ui,sans-serif;margin:0}
main{max-width:460px;margin:12vh auto;padding:25px}
h1{font-size:38px;letter-spacing:-1px;margin:15px 0}h1 span{color:#9aefcb}
p{color:#b6c6d0;line-height:1.55}form{background:#142130;padding:27px;border-radius:18px;border:1px solid #34495b}
label{display:block;margin:17px 0 8px;font-weight:600}
input{width:100%;padding:14px;border-radius:10px;border:1px solid #456378;background:#081522;color:white;font-size:16px}
button{background:#9aefcb;color:#0b261a;border:0;border-radius:10px;font-size:16px;font-weight:700;padding:15px;margin-top:23px;width:100%}
#error{color:#ffb3a6;min-height:25px}
</style></head><body><main><p>♫ STREAMZUMP3 ONLINE</p><h1>Willkommen <span>zurück.</span></h1>
<p>Mit deinen Zugangsdaten anmelden, um Audiostreams als MP3 aufzunehmen.</p>
<form id="login"><label for="username">Benutzername</label><input id="username" autocomplete="username" value="stream" required>
<label for="password">Passwort</label><input id="password" type="password" autocomplete="current-password" required>
<button type="submit" id="submit">Anmelden</button><p role="alert" id="error"></p></form>
</main><script>
document.getElementById('login').addEventListener('submit',async function(e){
e.preventDefault();
var button=document.getElementById('submit'),error=document.getElementById('error');
button.disabled=true;error.textContent='';
try{
var response=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({username:document.getElementById('username').value,password:document.getElementById('password').value})});
if(!response.ok){if(response.status===401)throw new Error('Benutzername oder Passwort ist falsch.');throw new Error('Anmeldung nicht möglich (HTTP '+response.status+').');}
location.replace('/');
}catch(ex){error.textContent=ex.message;button.disabled=false;}
});
</script></body></html>"""

class LoginRequest(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=250)

def valid_credentials(username: str, password: str) -> bool:
    return bool(PASSWORD) and hmac.compare_digest(username, USERNAME) and hmac.compare_digest(password, PASSWORD)

def make_session() -> str:
    ts = str(int(time.time()))
    signature = hmac.new(PASSWORD.encode(), (USERNAME + ":" + ts).encode(), hashlib.sha256).hexdigest()
    return ts + "." + signature

def valid_session(token: str | None) -> bool:
    if not token or not PASSWORD:
        return False
    try:
        ts, sig = token.split(".", 1)
        if not ts.isdigit() or len(ts) > 12 or not (0 <= time.time() - int(ts) < 12 * 3600):
            return False
        expected = hmac.new(PASSWORD.encode(), (USERNAME + ":" + ts).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except (TypeError, ValueError):
        return False

def is_authenticated(request: HttpRequest, creds: HTTPBasicCredentials | None) -> bool:
    return valid_session(request.cookies.get("stream_session")) or (
        creds is not None and valid_credentials(creds.username, creds.password)
    )

def auth(request: HttpRequest, creds: HTTPBasicCredentials | None = Depends(basic)):
    if not PASSWORD:
        raise HTTPException(503, "APP_PASSWORD fehlt")
    if not is_authenticated(request, creds):
        raise HTTPException(401, "Bitte erneut anmelden")

def public_url(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise HTTPException(400, "Nur öffentliche HTTP/HTTPS-Stream-Links erlaubt")
    if p.port and p.port not in (80, 443, 8000, 8080, 8443):
        raise HTTPException(400, "Dieser Stream-Port ist nicht freigegeben")
    host = p.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise HTTPException(400, "Interne Adressen nicht erlaubt")
    try:
        addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(400, "Stream-Adresse ist nicht öffentlich erreichbar")
    return url.strip()

def info(id):
    j = jobs.get(id)
    if not j:
        raise HTTPException(404, "Aufnahme nicht gefunden")
    return {"id":id,"status":j["status"],"message":j["message"],"download":"/api/jobs/"+id+"/download" if j["status"]=="done" else None}

def worker(id, req):
    j = jobs[id]
    path = workdir / (id + ".mp3")
    cmd = ["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y","-reconnect","1","-reconnect_streamed","1","-reconnect_delay_max","5","-i",req.url,"-map","0:a:0","-vn","-t",str(req.minutes*60),"-c:a","libmp3lame","-b:a",str(req.quality)+"k","-f","mp3",str(path)]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with lock: j["process"] = p
        try: _, err = p.communicate(timeout=req.minutes*60+40)
        except subprocess.TimeoutExpired:
            p.terminate()
            try: _, err = p.communicate(timeout=8)
            except subprocess.TimeoutExpired: p.kill(); _, err = p.communicate()
        with lock:
            if path.exists() and path.stat().st_size > 500 and path.stat().st_size <= 100*1024*1024:
                j["status"] = "done"
                j["message"] = "MP3 fertig zum Herunterladen"
                j["path"] = path
            else:
                j["status"] = "error"
                j["message"] = (err or b"Keine MP3 erzeugt").decode("utf-8","replace")[-200:]
                path.unlink(missing_ok=True)
    except Exception as exc:
        with lock:
            j["status"] = "error"
            j["message"] = str(exc)[:180]
    finally:
        with lock: j["process"] = None

@app.post("/api/login")
def login(creds: LoginRequest):
    if not PASSWORD:
        raise HTTPException(503, "APP_PASSWORD fehlt")
    if not valid_credentials(creds.username, creds.password):
        raise HTTPException(401, "Falsche Zugangsdaten")
    response = JSONResponse({"ok": True})
    response.set_cookie("stream_session", make_session(), max_age=12*3600, httponly=True, secure=True, samesite="strict", path="/")
    return response

@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("stream_session", path="/")
    return response

@app.get("/health")
def health(): return {"ok":True}

@app.get("/")
def index(request: HttpRequest, creds: HTTPBasicCredentials | None = Depends(basic)):
    if not PASSWORD:
        raise HTTPException(503, "APP_PASSWORD fehlt")
    if not is_authenticated(request, creds):
        return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})
    return HTMLResponse(Path(__file__).with_name("index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

@app.post("/api/jobs", status_code=202)
def start(req:Request, _:None = Depends(auth)):
    req.url = public_url(req.url)
    with lock:
        for id,j in list(jobs.items()):
            if j["status"] != "running" and time.time()-j["created"]>3600:
                if j.get("path"): j["path"].unlink(missing_ok=True)
                del jobs[id]
        if any(j["status"]=="running" for j in jobs.values()):
            raise HTTPException(429,"Es läuft bereits eine Aufnahme")
        id=uuid.uuid4().hex
        jobs[id]={"status":"running","message":"Stream wird aufgenommen ...","created":time.time(),"process":None}
        threading.Thread(target=worker,args=(id,req),daemon=True).start()
        return info(id)

@app.get("/api/jobs/{id}")
def status(id:str, _:None = Depends(auth)):
    with lock: return info(id)

@app.post("/api/jobs/{id}/stop")
def stop(id:str, _:None = Depends(auth)):
    with lock:
        j=jobs.get(id)
        if not j: raise HTTPException(404)
        p=j.get("process")
        if p and p.poll() is None: p.terminate()
        return info(id)

@app.get("/api/jobs/{id}/download")
def download(id:str, _:None = Depends(auth)):
    with lock:
        j=jobs.get(id)
        if not j or j["status"]!="done" or not j.get("path") or not j["path"].is_file(): raise HTTPException(404)
        return FileResponse(j["path"],media_type="audio/mpeg",filename="Stream-Aufnahme.mp3",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
