"""
Phase 4: the personal notifying service.

Start a watch job from a phone, have a background worker run it, and
get a push notification the moment the person shows up - the point at
which this stops being a pile of scripts and becomes the app.

Single user by design (implementation_plan.txt Phases 0-4 are the
single-user MVP). There are no accounts and no auth here: anyone who
can reach the server can start a job. That's fine while it's your own
machine behind a temporary tunnel, and it's exactly what Phase 5 adds
before this could ever be public.

Run it:
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import shutil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import cricket, push, storage, worker

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
UPLOADS_DIR = APP_DIR / "uploads"

app = FastAPI(title="Don't Miss The Moment")


@app.on_event("startup")
def startup():
    storage.init_db()
    UPLOADS_DIR.mkdir(exist_ok=True)
    problem = push.configure()
    if problem:
        # Not fatal on purpose: jobs still run and still resolve, they
        # just can't reach a phone yet.
        print(f"[push] notifications are off - {problem}")
    else:
        print("[push] Firebase ready")


@app.middleware("http")
async def note_public_url(request, call_next):
    """Learn the public address from whoever just reached us.

    Behind a tunnel the app has no way to know its own URL, and a
    notification's tap-through link has to be absolute HTTPS. The
    browser that just made this request knows it, so take it from
    there rather than making it configuration.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host")
    if host and forwarded_proto == "https":
        push.remember_base_url(f"https://{host}")
    return await call_next(request)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "push_configured": push.is_configured(),
        "devices_registered": len(storage.list_device_tokens()),
    }


@app.post("/api/devices")
def register_device(token: str = Form(...)):
    """A phone says 'notify me here'."""
    storage.save_device(token)
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(
    kind: str = Form(...),
    url: str = Form(None),
    match_id: str = Form(None),
    player: str = Form(None),
    listen_for: str = Form(None),
    max_minutes: float = Form(None),
    alert_mode: str = Form("once"),
    cookies: str = Form(None),
    photo: UploadFile = None,
):
    if kind not in ("video", "audio", "sports"):
        raise HTTPException(400, "kind must be video, audio or sports")

    params: dict = {"alert_mode": alert_mode}
    if max_minutes:
        params["max_minutes"] = max_minutes

    if kind == "sports":
        if not (match_id and player):
            raise HTTPException(400, "a sports job needs match_id and player")
        params |= {"match_id": match_id, "player": player}
    else:
        if not url:
            raise HTTPException(400, "a video or audio job needs a stream url")
        params["url"] = url
        if listen_for:
            params["listen_for"] = listen_for
        if cookies:
            params["cookies"] = cookies
        if kind == "audio" and not listen_for:
            raise HTTPException(400, "an audio job needs something to listen for")

    job_id = storage.create_job(kind, params)

    # A face job needs reference photos, and they're per-job so one
    # person's photos never leak into another job's matching.
    if photo is not None and photo.filename:
        job_photos = UPLOADS_DIR / job_id
        job_photos.mkdir(parents=True, exist_ok=True)
        target = job_photos / Path(photo.filename).name
        with target.open("wb") as out:
            shutil.copyfileobj(photo.file, out)
        params["photos_dir"] = str(job_photos)
        storage.update_job(job_id, params=__import__("json").dumps(params))
    elif kind == "video":
        raise HTTPException(400, "a video job needs at least one reference photo")

    worker.start_job(job_id)
    return {"id": job_id, "status": storage.PENDING}


@app.get("/api/jobs")
def list_jobs():
    return storage.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not worker.cancel_job(job_id):
        raise HTTPException(404, "that job isn't running")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: str):
    """Run the same watch again.

    The usual case is a job that timed out before the person appeared:
    the answer to "they never showed up" is normally "then watch again
    for a while longer", and retyping the whole thing on a phone is a
    poor way to ask for that.
    """
    old = storage.get_job(job_id)
    if old is None:
        raise HTTPException(404, "no such job")
    new_id = storage.create_job(old["kind"], old["params"])
    worker.start_job(new_id)
    return {"id": new_id, "status": storage.PENDING}


@app.get("/api/cricket/matches")
def cricket_matches():
    """Live matches with their IDs, because nobody knows a match ID.

    Asking a user to supply a Cricbuzz match id is asking them to go
    and find raw JSON. The watcher needs one, so the app has to offer
    the choice instead of demanding the answer.
    """
    try:
        return {"matches": cricket.live_matches()}
    except cricket.CricketUnavailable as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/test-notification")
def test_notification():
    """Prove the phone can actually receive one, before relying on it."""
    sent, error = push.send(
        storage.list_device_tokens(),
        title="Don't Miss The Moment",
        body="Notifications are working. This is what an alert will look like.",
        link=push.app_link("/"),
    )
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return {"ok": True, "sent": sent}


WEB_CONFIG_PATH = APP_DIR.parent / "firebase-web-config.json"


@app.get("/firebase-config.js")
def firebase_config():
    """Serve the Firebase web config, which is NOT kept in the repo.

    These values are public by design - they ship to every browser
    that loads the page, and Firebase access is controlled by security
    rules, not by hiding them. But GitHub's secret scanner flags a
    Google API key on sight, and an unrestricted key can be abused
    against other Google APIs on the same project's billing. Keeping
    it in an untracked file avoids both the alert and that risk,
    without pretending the browser never sees it.
    """
    import json as _json

    if not WEB_CONFIG_PATH.is_file():
        body = (
            "const firebaseConfig = {};\n"
            "self.VAPID_KEY = '';\n"
            "console.warn('No firebase-web-config.json - notifications are off. "
            "See firebase-web-config.example.json.');\n"
        )
        return Response(body, media_type="application/javascript")

    config = _json.loads(WEB_CONFIG_PATH.read_text(encoding="utf-8"))
    vapid = config.pop("vapidKey", "")
    body = (
        f"const firebaseConfig = {_json.dumps(config, indent=2)};\n"
        f"self.VAPID_KEY = {_json.dumps(vapid)};\n"
    )
    return Response(body, media_type="application/javascript")


# The service worker has to be served from the site root, not /static,
# or the browser won't let it receive background push.
@app.get("/firebase-messaging-sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "firebase-messaging-sw.js", media_type="application/javascript")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
