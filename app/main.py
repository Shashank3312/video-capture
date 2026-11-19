"""
The notifying service: start a watch job from a phone, have a
background worker run it, and get a push notification the moment the
person shows up.

Phase 4 built it for one user with no accounts. Phase 5 makes it
multi-user: every /api route except /api/health needs a Firebase
sign-in (auth.current_user), every job, device and saved person belongs
to a user, and anything belonging to someone else answers 404 exactly
like something that never existed - so ids can't even be probed. Face
and name (YouTube) watches are owner-only for now, because every one of
them runs on the owner's own YouTube session.

Run it:
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import io
import json
import shutil
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, cricket, db, people, push, storage, worker
from .auth import User
from .people_routes import BAD_PHOTO, is_allowed_photo, require_name
from .people_routes import router as people_router

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
# Job photo folders live under people.UPLOADS_DIR, read at call time
# (people.UPLOADS_DIR / job_id) - never copied into a constant here, or
# repointing it (as the tests do) would silently miss this module.

OWNER_ONLY = "face and name watches are owner-only for now"
NO_SUCH_JOB = "no such job"

app = FastAPI(title="Don't Miss The Moment")
app.include_router(people_router)


@app.on_event("startup")
def startup():
    db.init_db()
    people.UPLOADS_DIR.mkdir(exist_ok=True)
    warning = auth.check_access_settings()
    if warning:
        # Fails closed on purpose; say so, or "nobody can sign in" looks
        # like a broken login rather than a missing setting.
        print(f"[auth] {warning}")
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
    """Public, so a signed-out page can tell the server is up. It no
    longer counts devices: with accounts, that number would leak how
    many people use the app."""
    return {"ok": True, "push_configured": push.is_configured()}


@app.get("/api/me")
def me(user: User = Depends(auth.current_user)):
    return {"email": user.email, "is_owner": user.is_owner}


@app.post("/api/devices")
def register_device(token: str = Form(...), user: User = Depends(auth.current_user)):
    """A phone says 'notify me here' - for this user's jobs only."""
    storage.save_device(token, user.id)
    return {"ok": True}


@app.post("/api/check-photo")
async def check_photo(photo: UploadFile, user: User = Depends(auth.current_user)):
    """Show what face a reference photo actually yields, before it's used.

    A photo of someone in sunglasses was accepted silently and then
    matched strangers better than the real person - recognition leans
    on the eye region, so hiding it leaves an embedding with almost no
    identity in it. The app couldn't have known the eyes were covered,
    but it could have shown the crop and let a human see the problem
    in a second, which is what this does.
    """
    import base64
    import cv2
    import numpy as np

    raw = np.frombuffer(await photo.read(), np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        return {"ok": False, "error": "that file isn't an image I can read"}

    from deepface import DeepFace

    faces = DeepFace.extract_faces(
        img_path=image, detector_backend="yolov11m", enforce_detection=False,
        color_face="bgr", normalize_face=False,
    )
    faces = [f for f in faces if f.get("confidence", 1) > 0]
    if not faces:
        return {"ok": False, "error": "no face found in that photo - try a clearer, closer one"}

    biggest = max(faces, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
    a = biggest["facial_area"]
    crop = image[a["y"]: a["y"] + a["h"], a["x"]: a["x"] + a["w"]]
    ok, buf = cv2.imencode(".jpg", crop)
    return {
        "ok": True,
        "faces": len(faces),
        "size": f"{a['w']}x{a['h']}",
        "small": a["w"] < 120 or a["h"] < 120,
        "crop": base64.b64encode(buf).decode() if ok else None,
    }


def _check_owner_rule(kind: str, user: User):
    # Server-side on purpose: hiding the buttons on the page is not the
    # protection. Every YouTube job runs on the owner's YouTube session,
    # so a stranger's watch would look like the owner's account to
    # YouTube - and one block would stop everyone's watches at once.
    if kind in ("video", "audio") and not user.is_owner:
        raise HTTPException(403, OWNER_ONLY)


def _stage_photos(job_id: str, params: dict, fill: Callable[[Path], int]):
    """Give a job its own photo folder, or fail the job - never leave it pending.

    The job row already exists by now (its id names the folder), so a
    failure here can't simply be refused: a row left "pending" would
    never run and never resolve. It's marked failed with the reason,
    and any partial folder of face images is removed.
    """
    folder = people.UPLOADS_DIR / job_id
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if fill(folder) < 1:
            raise RuntimeError("no photo could be copied")
        params["photos_dir"] = str(folder)
        storage.update_job(job_id, params=params)
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        reason = f"couldn't store the reference photos: {exc}"
        storage.update_job(job_id, status=storage.FAILED, outcome="failed", progress=reason)
        raise HTTPException(500, reason)


def _restartable(job: dict) -> bool:
    # A one-off face watch's photos are deleted when it finishes, and a
    # deleted person's are gone with them (person_id is SET NULL), so
    # there is nothing left to watch for. Keeping one-off photos around
    # just to allow a restart would undo the promise to delete them.
    return not (job["kind"] == "video" and job["person_id"] is None)


def _with_restartable(job: dict) -> dict:
    return {**job, "restartable": _restartable(job)}


@app.post("/api/jobs")
async def create_job(
    kind: str = Form(...),
    url: str = Form(None),
    match_id: str = Form(None),
    player: str = Form(None),
    listen_for: str = Form(None),
    max_minutes: float = Form(None),
    alert_mode: str = Form("once"),
    person_id: str = Form(None),
    save_person: str = Form(None),
    person_name: str = Form(None),
    photo: UploadFile = None,
    user: User = Depends(auth.current_user),
):
    # There is deliberately no `cookies` field any more. It was a server
    # file path handed straight to yt-dlp's --cookies, which reads AND
    # writes that file - with several users, anyone signed in could
    # point it at any path on the machine. Jobs use only the server's
    # own default_cookies(); a stray `cookies` form field is ignored.
    if kind not in ("video", "audio", "sports"):
        raise HTTPException(400, "kind must be video, audio or sports")
    _check_owner_rule(kind, user)

    # Everything that can reject the request is checked BEFORE the job
    # row exists. Creating it first and then rejecting left an orphaned
    # "pending" job that would never run and never resolve - the one
    # outcome this app is not allowed to have.
    person = None
    if person_id:
        person = people.get_person(person_id, user.id)
        if person is None:
            raise HTTPException(404, "no such person")
        if kind == "audio":
            listen_for = listen_for or person["listen_for"]
        elif kind == "sports":
            player = player or person["player_name"]
        elif not person["photos"]:
            raise HTTPException(400, "this person has no photos yet")

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
        if kind == "audio" and not listen_for:
            raise HTTPException(400, "an audio job needs something to listen for")

    # A saved person's photos are used instead of any one-off upload.
    has_photo = person is None and photo is not None and bool(photo.filename)
    if kind == "video" and person is None and not has_photo:
        raise HTTPException(400, "a video job needs at least one reference photo")

    saving = person is None and save_person == "1"
    if saving:
        person_name = require_name(person_name, "person_name")
        if has_photo and not is_allowed_photo(photo.filename):
            raise HTTPException(400, BAD_PHOTO)

    # Read once: the same bytes go to the saved person and to the job.
    photo_bytes = await photo.read() if has_photo else None

    if saving:
        person = people.create_person(user.id, person_name, listen_for, player)
        if photo_bytes is not None:
            try:
                people.add_photo(person["id"], user.id, photo.filename, io.BytesIO(photo_bytes))
            except Exception:
                # No job is created after this, so don't keep a person
                # the user was never told about.
                people.delete_person(person["id"], user.id)
                raise

    job_id = storage.create_job(user.id, kind, params, person["id"] if person else None)

    # Reference photos are per-job, so one job's photos never leak into
    # another's matching, and later edits to (or deletion of) a saved
    # person can't pull the photos out from under a running watch.
    if kind == "video" and person_id:
        _stage_photos(job_id, params, lambda folder: people.copy_photos_to(person_id, folder))
    elif photo_bytes is not None:
        def write_upload(folder: Path) -> int:
            # .name only: a filename is attacker-controlled, and without
            # this a crafted one could write outside the uploads folder.
            (folder / Path(photo.filename).name).write_bytes(photo_bytes)
            return 1
        _stage_photos(job_id, params, write_upload)

    worker.start_job(job_id)
    return {"id": job_id, "status": storage.PENDING}


@app.get("/api/jobs")
def list_jobs(user: User = Depends(auth.current_user)):
    return [_with_restartable(job) for job in storage.list_jobs(user.id)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(auth.current_user)):
    job = storage.get_job(job_id, user.id)
    if job is None:
        raise HTTPException(404, NO_SUCH_JOB)
    return _with_restartable(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(auth.current_user)):
    # Ownership first: worker.cancel_job only knows job ids, and would
    # happily terminate anyone's watcher.
    if storage.get_job(job_id, user.id) is None:
        raise HTTPException(404, NO_SUCH_JOB)
    if not worker.cancel_job(job_id):
        raise HTTPException(404, "that job isn't running")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: str, user: User = Depends(auth.current_user)):
    """Run the same watch again.

    The usual case is a job that timed out before the person appeared:
    the answer to "they never showed up" is normally "then watch again
    for a while longer", and retyping the whole thing on a phone is a
    poor way to ask for that.

    The old job's photo folder is not reused - it's deleted when a job
    finishes. A face watch from a saved person re-copies that person's
    CURRENT photos, since an improved set is what someone restarting
    would want.
    """
    old = storage.get_job(job_id, user.id)
    if old is None:
        raise HTTPException(404, NO_SUCH_JOB)
    kind, person_id = old["kind"], old["person_id"]
    _check_owner_rule(kind, user)
    if not _restartable(old):
        raise HTTPException(409, "save this person to watch them again")
    if kind == "video":
        person = people.get_person(person_id, user.id)
        if person is None:  # deleted between the job read and now
            raise HTTPException(409, "save this person to watch them again")
        if not person["photos"]:
            raise HTTPException(400, "this person has no photos yet")

    params = {k: v for k, v in old["params"].items() if k not in ("photos_dir", "cookies")}
    new_id = storage.create_job(user.id, kind, params, person_id)
    if kind == "video":
        _stage_photos(new_id, params, lambda folder: people.copy_photos_to(person_id, folder))
    worker.start_job(new_id)
    return {"id": new_id, "status": storage.PENDING}


@app.get("/api/cricket/matches")
def cricket_matches(user: User = Depends(auth.current_user)):
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
def test_notification(user: User = Depends(auth.current_user)):
    """Prove the phone can actually receive one, before relying on it.

    Only this user's own devices - a test from one account must never
    buzz someone else's phone."""
    sent, error = push.send(
        storage.list_device_tokens(user.id),
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
    if not WEB_CONFIG_PATH.is_file():
        body = (
            "const firebaseConfig = {};\n"
            "self.VAPID_KEY = '';\n"
            "console.warn('No firebase-web-config.json - notifications are off. "
            "See firebase-web-config.example.json.');\n"
        )
        return Response(body, media_type="application/javascript")

    config = json.loads(WEB_CONFIG_PATH.read_text(encoding="utf-8"))
    vapid = config.pop("vapidKey", "")
    body = (
        f"const firebaseConfig = {json.dumps(config, indent=2)};\n"
        f"self.VAPID_KEY = {json.dumps(vapid)};\n"
    )
    return Response(body, media_type="application/javascript")


# The service worker has to be served from the site root, not /static,
# or the browser won't let it receive background push.
@app.get("/firebase-messaging-sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "firebase-messaging-sw.js", media_type="application/javascript")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
