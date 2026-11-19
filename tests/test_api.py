"""
API ownership and access rules, with the sign-in dependency overridden
by two fake users. No watcher ever runs here: worker.start_job is
replaced by a list, and every photo lands in tmp_path, never in the
real app/uploads.
"""

import io

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

from app import auth, people, storage, worker
from app.auth import User
from app.main import app

owner = User("uid-a", "a@example.com", True)
guest = User("uid-b", "b@example.com", False)


@pytest.fixture
def started(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(worker, "start_job", calls.append)
    return calls


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client_as(two_users, started, uploads):
    """client_as(user) -> a TestClient signed in as that user.

    One override serves every client; each client names its user in a
    test-only header, so an owner's client and a guest's client can be
    used interleaved without the last one created winning for both.
    """
    users: dict[str, User] = {}
    clients: list[TestClient] = []

    def fake_current_user(x_test_user: str = Header(...)) -> User:
        return users[x_test_user]

    app.dependency_overrides[auth.current_user] = fake_current_user

    def make(user: User) -> TestClient:
        key = f"{user.id}|{user.is_owner}"
        users[key] = user
        client = TestClient(app, headers={"x-test-user": key})
        client.__enter__()  # runs startup, like `with TestClient(app)`
        clients.append(client)
        return client

    try:
        yield make
    finally:
        for client in clients:
            client.__exit__(None, None, None)
        app.dependency_overrides.clear()


def _person_with_photo(client, name="Ram Charan"):
    p = client.post("/api/people", data={"name": name},
                    files=[("photos", ("face.jpg", io.BytesIO(b"img"), "image/jpeg"))])
    assert p.status_code == 200, p.text
    return p.json()


def test_signed_out_is_401(started, uploads):
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/people").status_code == 401
        assert client.get("/api/me").status_code == 401
    assert started == []


def test_health_is_public_and_counts_nothing(started, uploads):
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().keys() == {"ok", "push_configured"}


def test_me_reports_owner_status(client_as):
    assert client_as(owner).get("/api/me").json() == {"email": "a@example.com", "is_owner": True}
    assert client_as(guest).get("/api/me").json() == {"email": "b@example.com", "is_owner": False}


def test_guest_cannot_start_face_or_name_watches(client_as, started):
    g = client_as(guest)
    for kind in ("video", "audio"):
        r = g.post("/api/jobs", data={"kind": kind, "url": "https://youtu.be/x", "listen_for": "x"})
        assert r.status_code == 403
        assert r.json()["detail"] == "face and name watches are owner-only for now"
    # Rejected before any row exists: nothing orphaned, nothing started.
    assert storage.list_jobs(guest.id) == [] and started == []
    r = g.post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "X"})
    assert r.status_code == 200
    assert started == [r.json()["id"]]


def test_cookies_field_is_ignored(client_as):
    r = client_as(owner).post("/api/jobs", data={
        "kind": "audio", "url": "https://youtu.be/x", "listen_for": "x", "cookies": "C:\\x",
    })
    assert r.status_code == 200
    params = storage.get_job(r.json()["id"])["params"]
    assert "cookies" not in params and params["listen_for"] == "x"


def test_other_users_jobs_are_invisible(client_as):
    job = client_as(owner).post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "X"}).json()["id"]
    g = client_as(guest)
    missing = g.get("/api/jobs/doesnotexist")
    assert g.get(f"/api/jobs/{job}").status_code == 404
    assert g.get(f"/api/jobs/{job}").json() == missing.json()
    assert g.post(f"/api/jobs/{job}/cancel").status_code == 404
    assert g.post(f"/api/jobs/{job}/restart").status_code == 404
    assert g.get("/api/jobs").json() == []
    # The owner still sees it, with the restart flag.
    jobs = client_as(owner).get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [job] and jobs[0]["restartable"] is True


def test_cancel_checks_owner_before_touching_the_process(client_as, monkeypatch):
    cancelled: list[str] = []
    monkeypatch.setattr(worker, "cancel_job", lambda job_id: cancelled.append(job_id) or True)
    job = client_as(owner).post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "X"}).json()["id"]
    assert client_as(guest).post(f"/api/jobs/{job}/cancel").status_code == 404
    assert cancelled == []
    assert client_as(owner).post(f"/api/jobs/{job}/cancel").status_code == 200
    assert cancelled == [job]


def test_saved_person_fills_in_a_cricket_watch(client_as):
    g = client_as(guest)
    p = g.post("/api/people", data={"name": "Virat Kohli", "player_name": "V Kohli"}).json()
    r = g.post("/api/jobs", data={"kind": "sports", "match_id": "1", "person_id": p["id"]})
    assert r.status_code == 200, r.text
    job = storage.get_job(r.json()["id"])
    assert job["params"]["player"] == "V Kohli" and job["person_id"] == p["id"]


def test_saved_person_fills_in_a_name_watch(client_as):
    o = client_as(owner)
    p = o.post("/api/people", data={"name": "Ram Charan"}).json()
    r = o.post("/api/jobs", data={"kind": "audio", "url": "https://youtu.be/x", "person_id": p["id"]})
    assert r.status_code == 200, r.text
    assert storage.get_job(r.json()["id"])["params"]["listen_for"] == "Ram Charan"


def test_someone_elses_person_cannot_be_used_for_a_job(client_as, started):
    p = client_as(owner).post("/api/people", data={"name": "X"}).json()
    r = client_as(guest).post("/api/jobs", data={"kind": "sports", "match_id": "1", "person_id": p["id"]})
    assert r.status_code == 404
    assert storage.list_jobs(guest.id) == [] and started == []


def test_face_watch_needs_a_person_with_photos(client_as, started):
    o = client_as(owner)
    p = o.post("/api/people", data={"name": "No Photos"}).json()
    r = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x", "person_id": p["id"]})
    assert r.status_code == 400
    assert r.json()["detail"] == "this person has no photos yet"
    assert storage.list_jobs(owner.id) == [] and started == []


def test_face_watch_from_person_copies_photos_and_survives_person_deletion(client_as, uploads):
    o = client_as(owner)
    p = _person_with_photo(o)
    r = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x", "person_id": p["id"]})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    job = storage.get_job(job_id)
    assert job["params"]["photos_dir"] == str(uploads / job_id)
    assert o.get("/api/jobs").json()[0]["restartable"] is True

    assert o.delete(f"/api/people/{p['id']}").status_code == 200
    assert [f.read_bytes() for f in (uploads / job_id).iterdir()] == [b"img"]
    assert not (uploads / "people" / p["id"]).exists()
    jobs = o.get("/api/jobs").json()
    assert jobs[0]["id"] == job_id and jobs[0]["restartable"] is False
    assert o.post(f"/api/jobs/{job_id}/restart").status_code == 409


def test_restarting_a_face_watch_uses_the_persons_current_photos(client_as, uploads, started):
    o = client_as(owner)
    p = _person_with_photo(o)
    first = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x", "person_id": p["id"]}).json()["id"]
    old_photo = p["photos"][0]["id"]
    o.post(f"/api/people/{p['id']}/photos",
           files={"photo": ("better.png", io.BytesIO(b"better"), "image/png")})
    o.delete(f"/api/people/{p['id']}/photos/{old_photo}")

    r = o.post(f"/api/jobs/{first}/restart")
    assert r.status_code == 200, r.text
    second = r.json()["id"]
    params = storage.get_job(second)["params"]
    assert params["photos_dir"] == str(uploads / second)
    assert [f.read_bytes() for f in (uploads / second).iterdir()] == [b"better"]
    assert storage.get_job(second)["person_id"] == p["id"]
    assert started == [first, second]


def test_photo_copy_failure_fails_the_job_instead_of_leaving_it_pending(
        client_as, uploads, started, monkeypatch):
    o = client_as(owner)
    p = _person_with_photo(o)

    def broken_copy(person_id, dest):
        (dest / "partial.jpg").write_bytes(b"half")
        raise OSError("disk full")
    monkeypatch.setattr(people, "copy_photos_to", broken_copy)

    r = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x", "person_id": p["id"]})
    assert r.status_code == 500
    (job,) = storage.list_jobs(owner.id)
    assert job["status"] == storage.FAILED and "disk full" in job["progress"]
    assert not (uploads / job["id"]).exists()
    assert started == []


def test_one_off_face_watch_is_not_restartable(client_as, uploads):
    o = client_as(owner)
    r = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x"},
               files={"photo": ("face.jpg", io.BytesIO(b"img"), "image/jpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert [f.read_bytes() for f in (uploads / job_id).iterdir()] == [b"img"]
    assert o.get("/api/jobs").json()[0]["restartable"] is False
    r = o.post(f"/api/jobs/{job_id}/restart")
    assert r.status_code == 409
    assert r.json()["detail"] == "save this person to watch them again"


def test_guest_cannot_restart_into_a_face_or_name_watch(client_as):
    # A job started while they were an owner; owner status since revoked.
    job = client_as(owner).post("/api/jobs", data={
        "kind": "audio", "url": "https://youtu.be/x", "listen_for": "x"}).json()["id"]
    demoted = User(owner.id, owner.email, False)
    assert client_as(demoted).post(f"/api/jobs/{job}/restart").status_code == 403


def test_save_person_from_a_one_off_watch(client_as):
    g = client_as(guest)
    r = g.post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "V Kohli",
                                  "save_person": "1", "person_name": "X"})
    assert r.status_code == 200, r.text
    listed = g.get("/api/people").json()
    assert [(p["name"], p["player_name"]) for p in listed] == [("X", "V Kohli")]
    assert storage.get_job(r.json()["id"])["person_id"] == listed[0]["id"]


def test_save_person_needs_a_name(client_as, started):
    g = client_as(guest)
    r = g.post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "V Kohli",
                                  "save_person": "1", "person_name": "  "})
    assert r.status_code == 400
    assert g.get("/api/people").json() == [] and storage.list_jobs(guest.id) == []
    assert started == []


def test_save_person_from_a_one_off_face_watch_keeps_the_photo_twice(client_as, uploads):
    o = client_as(owner)
    r = o.post("/api/jobs", data={"kind": "video", "url": "https://youtu.be/x",
                                  "save_person": "1", "person_name": "Ram Charan"},
               files={"photo": ("face.jpg", io.BytesIO(b"img"), "image/jpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    (person,) = o.get("/api/people").json()
    assert len(person["photos"]) == 1
    saved = o.get(f"/api/people/{person['id']}/photos/{person['photos'][0]['id']}")
    assert saved.content == b"img"
    assert [f.read_bytes() for f in (uploads / job_id).iterdir()] == [b"img"]
    assert storage.get_job(job_id)["person_id"] == person["id"]
    assert o.get("/api/jobs").json()[0]["restartable"] is True


def test_bad_photo_type_is_400(client_as):
    o = client_as(owner)
    p = o.post("/api/people", data={"name": "X"}).json()
    r = o.post(f"/api/people/{p['id']}/photos",
               files={"photo": ("evil.py", io.BytesIO(b"import os"), "text/plain")})
    assert r.status_code == 400
    assert r.json()["detail"] == "photos must be .jpg, .jpeg, .png or .webp"
    # And creating a person with a bad photo creates nothing at all.
    r = o.post("/api/people", data={"name": "Y"},
               files=[("photos", ("evil.py", io.BytesIO(b"x"), "text/plain"))])
    assert r.status_code == 400
    assert [q["name"] for q in o.get("/api/people").json()] == ["X"]


def test_an_empty_file_input_is_not_a_photo(client_as):
    # What a browser sends for a file input left empty: one nameless part.
    r = client_as(owner).post("/api/people", data={"name": "E"},
                              files=[("photos", ("", io.BytesIO(b""), "application/octet-stream"))])
    assert r.status_code == 200, r.text
    assert r.json()["photos"] == []


def test_blank_person_name_is_400(client_as):
    o = client_as(owner)
    assert o.post("/api/people", data={"name": "   "}).status_code == 400
    p = o.post("/api/people", data={"name": "X"}).json()
    assert o.patch(f"/api/people/{p['id']}", data={"name": " "}).status_code == 400
    assert o.get("/api/people").json()[0]["name"] == "X"


def test_patch_changes_only_what_is_sent(client_as):
    o = client_as(owner)
    p = o.post("/api/people", data={"name": "Virat Kohli", "player_name": "V Kohli"}).json()
    r = o.patch(f"/api/people/{p['id']}", data={"listen_for": "Kohli"})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["listen_for"], r.json()["player_name"]) == (
        "Virat Kohli", "Kohli", "V Kohli")


def test_other_users_people_are_404(client_as):
    p = _person_with_photo(client_as(owner))
    photo = p["photos"][0]["id"]
    g = client_as(guest)
    assert g.get("/api/people").json() == []
    assert g.get(f"/api/people/{p['id']}/photos/{photo}").status_code == 404
    assert g.patch(f"/api/people/{p['id']}", data={"name": "Mine"}).status_code == 404
    assert g.post(f"/api/people/{p['id']}/photos",
                  files={"photo": ("x.jpg", io.BytesIO(b"x"), "image/jpeg")}).status_code == 404
    assert g.delete(f"/api/people/{p['id']}/photos/{photo}").status_code == 404
    assert g.delete(f"/api/people/{p['id']}").status_code == 404
    # Indistinguishable from a person that never existed.
    assert (g.delete(f"/api/people/{p['id']}").json()
            == g.delete("/api/people/doesnotexist").json())
    # Nothing was changed underneath the owner.
    still = client_as(owner).get("/api/people").json()
    assert [(q["name"], len(q["photos"])) for q in still] == [("Ram Charan", 1)]


def test_devices_and_test_notification_are_per_user(client_as, monkeypatch):
    sent_to: list[list[str]] = []
    monkeypatch.setattr("app.push.send", lambda tokens, **kw: (sent_to.append(tokens) or (len(tokens), None)))
    assert client_as(owner).post("/api/devices", data={"token": "tok-a"}).status_code == 200
    assert client_as(guest).post("/api/devices", data={"token": "tok-b"}).status_code == 200
    assert client_as(guest).post("/api/test-notification").json() == {"ok": True, "sent": 1}
    assert sent_to == [["tok-b"]]
