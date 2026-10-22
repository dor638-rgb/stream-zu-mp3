from __future__ import annotations
import hmac, ipaddress, os, socket, subprocess, tempfile, threading, time, uuid
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse
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

def auth(creds: HTTPBasicCredentials | None = Depends(basic)):
    if not PASSWORD:
        raise HTTPException(503, "APP_PASSWORD fehlt")
    if not creds or not (hmac.compare_digest(creds.username, USERNAME) and hmac.compare_digest(creds.password, PASSWORD)):
        raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": 'Basic realm="StreamZuMP3"'})

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

@app.get("/health")
def health(): return {"ok":True}

@app.get("/")
def index(_:None = Depends(auth)):
    return HTMLResponse(Path(__file__).with_name("index.html").read_text(encoding="utf-8"))

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
