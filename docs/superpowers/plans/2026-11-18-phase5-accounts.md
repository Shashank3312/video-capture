# Phase 5: Accounts, Saved People, PostgreSQL - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several people use the app safely - Google sign-in behind an allow-list, per-user jobs/devices/saved people, alerts only to the job owner's phones - with storage moved from SQLite to PostgreSQL.

**Architecture:** The phone page signs in with Firebase Auth (Google) and sends the ID token on every `/api/*` call; a FastAPI dependency verifies it with `firebase_admin` and yields a `User`. Storage is plain SQL over `psycopg` 3 with a connection pool, split into `db.py` (pool + schema), `storage.py` (users, devices, jobs) and `people.py` (saved people + their photo files). The watcher scripts are untouched.

**Tech Stack:** Python 3.13, FastAPI, psycopg 3 + psycopg_pool, PostgreSQL (native Windows install), firebase-admin 7.5.0, Firebase JS compat SDK 10.12.2, pytest + httpx (FastAPI TestClient).

**Spec:** `docs/superpowers/specs/2026-11-18-phase5-accounts-design.md` - read it first; this plan argues from it.

## Global Constraints

- Watcher scripts (`track_a_sports/`, `track_b_video/`) are NOT modified. They stay account-unaware subprocesses speaking `--json` events.
- Every job still ends `matched` / `expired` / `failed` - never silently.
- `ALLOWED_EMAILS` and `OWNER_EMAILS` (comma-separated, in `.env`) **fail closed**: unset or empty admits nobody. Only the literal `*` means everyone.
- Face and name watches (`kind` `video`/`audio`) are owner-only, enforced server-side with 403. Cricket (`sports`) is open to every allowed user.
- Another user's job, person or photo returns **404**, never 403.
- `POST /api/jobs` no longer accepts a `cookies` field; jobs use only `worker.default_cookies()`.
- Paths on disk are built from ids only: saved photos at `app/uploads/people/<person_id>/<photo_id><ext>`, job copies at `app/uploads/<job_id>/`. `<ext>` is one of `.jpg .jpeg .png .webp` or the upload is rejected.
- Timestamps stay ISO-8601 UTC strings (`TEXT`), as today, so the page's display keeps working.
- Firebase JS scripts all at version `10.12.2`.
- Nothing secret or personal is committed: `.env`, photos, databases stay ignored. The repo is public.
- Commit messages explain why; push to `origin` after each task (CLAUDE.md).

## Review Focus

1. A phone left open past the 1-hour ID-token lifetime: the page must keep working (`getIdToken()` refreshes) and a real 401 must send the user back to the sign-in screen, not show a raw error. Server test: an expired token gives 401 (Task 2).
2. A photo upload named `../../evil.py` or with a non-image extension: rejected with 400, and nothing written outside the person's folder (Task 3).
3. Deleting a saved person while a face job started from them is running: the job keeps its own copy and keeps running (Task 4).
4. A phone that signs out and signs in as a different user: its token moves, and the first user's alerts stop going to it (Task 1).
5. A non-owner who calls `POST /api/jobs` with `kind=video` directly, bypassing the hidden buttons: 403. Cancelling someone else's running job: 404 and the process is untouched (Task 4).

---

### Task 0: Owner setup (manual - the executor asks the owner and waits)

Cannot be done by an agent: it needs an installer with admin rights and the owner's Firebase console.

- [ ] **Step 1:** Owner installs PostgreSQL for Windows (EnterpriseDB installer, current major version), remembering the `postgres` password.
- [ ] **Step 2:** Create two databases: `moment` (the app) and `moment_test` (tests only - tests wipe it).
- [ ] **Step 3:** Add to `.env`:
  ```
  DATABASE_URL=postgresql://postgres:<password>@localhost:5432/moment
  TEST_DATABASE_URL=postgresql://postgres:<password>@localhost:5432/moment_test
  ALLOWED_EMAILS=<owner's gmail>
  OWNER_EMAILS=<owner's gmail>
  ```
- [ ] **Step 4:** Firebase console -> Authentication -> Sign-in method: enable Google.
- [ ] **Step 5:** Verify: `.venv\Scripts\python.exe -c "import os,dotenv;dotenv.load_dotenv();print(bool(os.environ.get('DATABASE_URL')))"` prints `True`.

---

### Task 1: PostgreSQL storage for users, devices and jobs, plus the test harness

**Files:**
- Modify: `app/requirements.txt` (add `psycopg[binary]`, `psycopg_pool`, `pytest`, `httpx`, pinned to the versions installed)
- Create: `app/db.py`
- Rewrite: `app/storage.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_storage.py`

**Interfaces:**
- Produces (`app/db.py`):
  - `connection()` - context manager yielding a pooled `psycopg.Connection` with `row_factory=dict_row`; commits on clean exit.
  - `init_db() -> None` - creates all five tables from the spec (`users`, `devices`, `people`, `person_photos`, `jobs`) with `CREATE TABLE IF NOT EXISTS`. `jobs.params` and `jobs.alerts_log` are `JSONB` (`alerts_log` default `'[]'`); `jobs.user_id` references `users`, `jobs.person_id` references `people ON DELETE SET NULL`; `person_photos.person_id` references `people ON DELETE CASCADE`.
  - `DATABASE_URL` read from the environment lazily (after `load_dotenv(PROJECT_ROOT/".env")`, which must not override an already-set variable) on first use, so tests can point it elsewhere first.
- Produces (`app/storage.py`), keeping the module's state constants (`PENDING` ... `FINISHED_STATES`) unchanged:
  - `upsert_user(user_id: str, email: str, display_name: str | None) -> None`
  - `create_job(user_id: str, kind: str, params: dict, person_id: str | None = None) -> str`
  - `update_job(job_id: str, **fields) -> None` - dict/list values stored as JSONB; finishing states still stamp `finished_at`.
  - `get_job(job_id: str, user_id: str | None = None) -> dict | None` - `user_id=None` is for the worker (no owner filter); a given `user_id` returns None for anyone else's job.
  - `list_jobs(user_id: str, limit: int = 50) -> list[dict]`
  - `add_alert(job_id: str, entry: dict) -> None` - atomic append (`alerts_log = alerts_log || %s::jsonb`), replacing today's read-modify-write.
  - `save_device(token: str, user_id: str) -> None` - upsert on token, so a token moves to the latest user.
  - `list_device_tokens(user_id: str) -> list[str]`
- Remove the SQLite `_write_lock` and `DB_PATH`; `app/jobs.db` is left on disk, unused.

- [ ] **Step 1: Write the test harness `tests/conftest.py`**

Before any `app` import: load `.env`, then `os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]` (fail with a clear message if `TEST_DATABASE_URL` is missing - never fall back to the real database). Autouse fixture `clean_db`: `db.init_db()`, then `TRUNCATE users, devices, people, person_photos, jobs CASCADE`. Fixture `two_users`: upserts `("uid-a", "a@example.com")` and `("uid-b", "b@example.com")` and returns their ids.

- [ ] **Step 2: Write the failing tests `tests/test_storage.py`**

```python
def test_jobs_are_scoped_to_their_owner(two_users):
    a, b = two_users
    job = storage.create_job(a, "sports", {"match_id": "1", "player": "X"})
    assert storage.get_job(job, a)["params"] == {"match_id": "1", "player": "X"}
    assert storage.get_job(job, b) is None
    assert storage.get_job(job)["user_id"] == a          # worker view
    assert [j["id"] for j in storage.list_jobs(a)] == [job]
    assert storage.list_jobs(b) == []

def test_finishing_stamps_finished_at(two_users):
    job = storage.create_job(two_users[0], "sports", {})
    storage.update_job(job, status=storage.MATCHED, outcome="matched")
    assert storage.get_job(job)["finished_at"]

def test_alerts_append_in_order(two_users):
    job = storage.create_job(two_users[0], "sports", {})
    storage.add_alert(job, {"at": "1"}); storage.add_alert(job, {"at": "2"})
    assert [a["at"] for a in storage.get_job(job)["alerts_log"]] == ["1", "2"]

def test_device_tokens_belong_to_one_user(two_users):
    a, b = two_users
    storage.save_device("tok-1", a)
    assert storage.list_device_tokens(a) == ["tok-1"]
    storage.save_device("tok-1", b)                     # signed in as someone else
    assert storage.list_device_tokens(a) == []
    assert storage.list_device_tokens(b) == ["tok-1"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage.py -v`
Expected: FAIL (import error / wrong signatures).

- [ ] **Step 4: Install the dependencies, implement `app/db.py` and rewrite `app/storage.py` to the interfaces above**

`ConnectionPool(DATABASE_URL, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True)`. Keep the module docstrings' style: say why, including why SQLite was replaced.

- [ ] **Step 5: Run to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit and push** (`app/requirements.txt app/db.py app/storage.py tests/`)

---

### Task 2: Sign-in verification and the allow-lists

**Files:**
- Create: `app/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `storage.upsert_user` (Task 1); `push.configure()` (existing - initialises the Firebase admin app).
- Produces (`app/auth.py`):
  - `@dataclass(frozen=True) class User: id: str; email: str; is_owner: bool`
  - `email_allowed(email: str, setting: str | None) -> bool` - `*` allows all; unset/blank allows none; otherwise case-insensitive membership after splitting on commas and stripping.
  - `current_user(authorization: str | None = Header(None)) -> User` - FastAPI dependency. No/garbled `Bearer` header or any `firebase_admin.auth` verification error (including expired) -> 401 `"sign in first"`. `email_verified` false, or email not in `ALLOWED_EMAILS` -> 403 `"this app is invite-only for now"`. Otherwise `upsert_user(...)` and return `User(uid, email, is_owner=email_allowed(email, OWNER_EMAILS))`.
  - `check_access_settings() -> str | None` - a warning string when `ALLOWED_EMAILS` is unset/blank ("nobody can sign in until ALLOWED_EMAILS is set in .env"), else None. Called from startup in Task 4.

- [ ] **Step 1: Write the failing tests `tests/test_auth.py`**

```python
@pytest.mark.parametrize("setting,email,expected", [
    (None, "a@x.com", False), ("", "a@x.com", False), ("  ", "a@x.com", False),
    ("*", "anyone@x.com", True),
    ("a@x.com, B@x.com", "b@x.com", True), ("a@x.com", "c@x.com", False),
])
def test_email_allowed_fails_closed(setting, email, expected):
    assert auth.email_allowed(email, setting) is expected

def test_missing_token_is_401(): ...            # current_user(None) raises HTTPException 401
def test_expired_token_is_401(monkeypatch): ... # verify_id_token raises auth.ExpiredIdTokenError -> 401
def test_unverified_email_is_403(monkeypatch): ...
def test_not_on_allow_list_is_403(monkeypatch): ...
def test_allowed_user_is_created_and_owner_flag_set(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "a@x.com,b@x.com"); monkeypatch.setenv("OWNER_EMAILS", "a@x.com")
    # verify_id_token patched to return {"uid": "u1", "email": "a@x.com", "email_verified": True, "name": "A"}
    user = auth.current_user("Bearer good")
    assert user == auth.User("u1", "a@x.com", True)
    # and storage now has a users row for u1
```

Patch `firebase_admin.auth.verify_id_token` (as imported by `app.auth`) and `push.configure` (return None) in every test - no network.

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_auth.py -v` - Expected: FAIL (no module).
- [ ] **Step 3:** Implement `app/auth.py` to the interfaces above. Read the env vars at call time, not import time.
- [ ] **Step 4:** Run again - Expected: all pass.
- [ ] **Step 5: Commit and push.**

---

### Task 3: Saved people and their photo files

**Files:**
- Create: `app/people.py`
- Test: `tests/test_people.py`

**Interfaces:**
- Consumes: `db.connection()` (Task 1).
- Produces (`app/people.py`):
  - `UPLOADS_DIR: Path` = `app/uploads` (moved here from `main.py`; tests monkeypatch it to `tmp_path`); `people_dir() -> Path` returns `UPLOADS_DIR / "people"`.
  - `PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")`
  - `class BadPhoto(ValueError)` - raised for a disallowed extension.
  - Person dicts: `{"id", "name", "listen_for", "player_name", "photos": [{"id"}], "created_at"}` where `listen_for`/`player_name` are the EFFECTIVE values (stored value, or `name` when blank).
  - `create_person(user_id: str, name: str, listen_for: str | None = None, player_name: str | None = None) -> dict`
  - `list_people(user_id: str) -> list[dict]`
  - `get_person(person_id: str, user_id: str) -> dict | None`
  - `update_person(person_id: str, user_id: str, *, name: str | None = None, listen_for: str | None = None, player_name: str | None = None) -> dict | None` - None arguments leave a field unchanged; an empty string clears an optional field back to "defaults to name".
  - `add_photo(person_id: str, user_id: str, filename: str, data: BinaryIO) -> dict | None` - extension from `Path(filename).suffix.lower()`, must be in `PHOTO_EXTENSIONS` else `BadPhoto`; written to `people_dir()/<person_id>/<photo_id><ext>`; returns `{"id"}`; None if the person isn't the user's.
  - `photo_path(person_id: str, photo_id: str, user_id: str) -> Path | None`
  - `delete_photo(person_id: str, photo_id: str, user_id: str) -> bool` - row and file.
  - `delete_person(person_id: str, user_id: str) -> bool` - rows (cascade) and `shutil.rmtree` of the folder.
  - `copy_photos_to(person_id: str, dest: Path) -> int` - copies every photo file into `dest` (created), returns the count. No owner check: callers check first.

- [ ] **Step 1: Write the failing tests `tests/test_people.py`** (autouse fixture: `monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)`)

```python
def test_effective_names_default_to_name(two_users):
    p = people.create_person(two_users[0], "Virat Kohli", player_name="V Kohli")
    assert (p["listen_for"], p["player_name"]) == ("Virat Kohli", "V Kohli")

def test_people_are_private(two_users):
    a, b = two_users
    p = people.create_person(a, "X")
    assert people.get_person(p["id"], b) is None
    assert people.list_people(b) == []
    assert people.add_photo(p["id"], b, "x.jpg", io.BytesIO(b"img")) is None
    assert people.delete_person(p["id"], b) is False

def test_photo_names_come_from_ids_not_uploads(two_users, tmp_path):
    p = people.create_person(two_users[0], "X")
    with pytest.raises(people.BadPhoto):
        people.add_photo(p["id"], two_users[0], "../../evil.py", io.BytesIO(b"x"))
    photo = people.add_photo(p["id"], two_users[0], "../../face.JPG", io.BytesIO(b"img"))
    path = people.photo_path(p["id"], photo["id"], two_users[0])
    assert path.parent == tmp_path / "people" / p["id"] and path.read_bytes() == b"img"
    assert path.name == photo["id"] + ".jpg"

def test_deleting_a_person_removes_their_files(two_users, tmp_path):
    p = people.create_person(two_users[0], "X")
    people.add_photo(p["id"], two_users[0], "a.png", io.BytesIO(b"1"))
    assert people.delete_person(p["id"], two_users[0]) is True
    assert not (tmp_path / "people" / p["id"]).exists()

def test_update_changes_only_what_is_given(two_users):
    p = people.create_person(two_users[0], "Virat Kohli", player_name="V Kohli")
    p = people.update_person(p["id"], two_users[0], listen_for="Kohli")
    assert (p["name"], p["listen_for"], p["player_name"]) == ("Virat Kohli", "Kohli", "V Kohli")
    p = people.update_person(p["id"], two_users[0], player_name="")   # cleared -> defaults to name
    assert p["player_name"] == "Virat Kohli"
    assert people.update_person(p["id"], two_users[1], name="X") is None

def test_deleting_one_photo_removes_its_file(two_users): ...
def test_copy_photos_to(two_users, tmp_path): ...   # 2 photos -> returns 2, both files in dest
```

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_people.py -v` - Expected: FAIL.
- [ ] **Step 3:** Implement `app/people.py` to the interfaces above.
- [ ] **Step 4:** Run again - Expected: all pass.
- [ ] **Step 5: Commit and push.**

---

### Task 4: The API - every route signed in, owned, and owner-only where required

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `auth.current_user`, `auth.User`, `auth.check_access_settings` (Task 2); `storage.*` (Task 1); `people.*` (Task 3); `worker.start_job`, `worker.cancel_job` (existing).
- Produces (HTTP, all `/api/*` below take `user: User = Depends(current_user)` unless marked public):
  - `GET /api/health` (public) -> `{"ok": true, "push_configured": bool}` - no device count.
  - `GET /api/me` -> `{"email", "is_owner"}`
  - `POST /api/devices` (form `token`) -> `save_device(token, user.id)`
  - `POST /api/check-photo` - unchanged behaviour, now signed-in only.
  - `POST /api/jobs` form: `kind`, `url`, `match_id`, `player`, `listen_for`, `max_minutes`, `alert_mode`, `photo`, plus new `person_id`, `save_person` ("1"), `person_name`. **No `cookies`.** Rules, in order:
    1. `kind` in (`video`,`audio`) and not `user.is_owner` -> 403 `"face and name watches are owner-only for now"`.
    2. `person_id` given: `people.get_person(person_id, user.id)` or 404. Fill blanks from it: `audio` -> `listen_for`; `sports` -> `player` (its `player_name`); `video` -> the person must have >= 1 photo (else 400 `"this person has no photos yet"`), and the job's photos come from `people.copy_photos_to(person_id, UPLOADS_DIR/<job_id>)`.
    3. Existing validation (sports needs match_id+player, etc.), before any row is created - as today.
    4. `save_person == "1"` without `person_id`: `person_name` required (400 otherwise); create the person with `listen_for`/`player_name` taken from the form, and the uploaded `photo` (if any) added to them; the job then records that `person_id`.
    5. `storage.create_job(user.id, kind, params, person_id)`; photos into `UPLOADS_DIR/<job_id>/` with `params["photos_dir"]` set, as today.
  - `GET /api/jobs` -> `list_jobs(user.id)`, each job with an added `"restartable": bool` = not (`kind == "video"` and `person_id` is None).
  - `GET /api/jobs/{id}` -> 404 unless the user's.
  - `POST /api/jobs/{id}/cancel` -> 404 unless the user's (check BEFORE `worker.cancel_job`); then as today.
  - `POST /api/jobs/{id}/restart` -> 404 unless the user's; 409 `"save this person to watch them again"` if not restartable; a `video` restart re-copies the person's CURRENT photos into the new job's folder; otherwise reuses params as today. Owner rule 1 applies to restarts too.
  - People: `GET /api/people`; `POST /api/people` (form `name`, `listen_for`, `player_name`, `photos: list[UploadFile] = []`); `PATCH /api/people/{id}` (form fields, absent = unchanged); `POST /api/people/{id}/photos` (form `photo`); `GET /api/people/{id}/photos/{photo_id}` (`FileResponse`); `DELETE /api/people/{id}/photos/{photo_id}`; `DELETE /api/people/{id}`. Each 404 unless the user's; `BadPhoto` -> 400 `"photos must be .jpg, .jpeg, .png or .webp"`.
  - `POST /api/test-notification` -> sends only to `list_device_tokens(user.id)`.
  - Startup: `init_db()`; print `check_access_settings()` when it returns a warning.
  - `UPLOADS_DIR` comes from `people.UPLOADS_DIR`.

- [ ] **Step 1: Write the failing tests `tests/test_api.py`**

Fixtures: `owner = User("uid-a", "a@example.com", True)`, `guest = User("uid-b", "b@example.com", False)` (rows created via `two_users`); `client_as(user)` builds `TestClient(app)` with `app.dependency_overrides[auth.current_user] = lambda: user`; `monkeypatch.setattr(worker, "start_job", started.append)` and `monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)` in every test, so no watcher ever runs.

```python
def test_signed_out_is_401():           # no override: GET /api/jobs -> 401
def test_health_is_public_and_counts_nothing():
    assert TestClient(app).get("/api/health").json().keys() == {"ok", "push_configured"}

def test_guest_cannot_start_face_or_name_watches(client_as):
    for kind in ("video", "audio"):
        r = client_as(guest).post("/api/jobs", data={"kind": kind, "url": "https://youtu.be/x", "listen_for": "x"})
        assert r.status_code == 403
    r = client_as(guest).post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "X"})
    assert r.status_code == 200

def test_cookies_field_is_ignored(client_as):    # owner posts cookies=C:\\x; stored params has no "cookies"

def test_other_users_jobs_are_invisible(client_as):
    job = client_as(owner).post("/api/jobs", data={"kind": "sports", "match_id": "1", "player": "X"}).json()["id"]
    g = client_as(guest)
    assert g.get(f"/api/jobs/{job}").status_code == 404
    assert g.post(f"/api/jobs/{job}/cancel").status_code == 404
    assert g.post(f"/api/jobs/{job}/restart").status_code == 404
    assert g.get("/api/jobs").json() == []

def test_cancel_checks_owner_before_touching_the_process(client_as, monkeypatch):
    # worker.cancel_job patched to record calls; guest cancel -> 404 and no call recorded

def test_saved_person_fills_in_a_cricket_watch(client_as):
    p = client_as(guest).post("/api/people", data={"name": "Virat Kohli", "player_name": "V Kohli"}).json()
    job = client_as(guest).post("/api/jobs", data={"kind": "sports", "match_id": "1", "person_id": p["id"]}).json()["id"]
    assert storage.get_job(job)["params"]["player"] == "V Kohli"

def test_face_watch_from_person_copies_photos_and_survives_person_deletion(client_as, tmp_path):
    # owner creates person with one photo; starts video job with person_id;
    # deletes the person; tmp_path/<job_id> still holds 1 file; GET /api/jobs shows restartable False

def test_one_off_face_watch_is_not_restartable(client_as):    # restart -> 409
def test_save_person_from_a_one_off_watch(client_as):          # save_person=1, person_name="X", sports -> person listed, job.person_id set
def test_bad_photo_type_is_400(client_as):                     # POST /api/people/{id}/photos with evil.py -> 400
def test_other_users_people_are_404(client_as):                # GET photo / PATCH / DELETE as guest on owner's person -> 404
```

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_api.py -v` - Expected: FAIL.
- [ ] **Step 3:** Implement the routes in `app/main.py` to the interfaces above. Update the module docstring: no longer "no accounts and no auth".
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -v` - Expected: all tests in `tests/` pass.
- [ ] **Step 5: Commit and push.**

---

### Task 5: The worker - alert only the owner, no caller cookies, clean up job photos

**Files:**
- Modify: `app/worker.py` (`build_command`, `_notify`, `_notify_outcome`, `run_job`)
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `storage.list_device_tokens(user_id)`, `storage.get_job` (Task 1); `people.UPLOADS_DIR` (Task 3).
- Produces: no new public names. Behaviour:
  - `_notify` / `_notify_outcome` send to `storage.list_device_tokens(job["user_id"])`.
  - `build_command` uses `default_cookies()` only (ignores any `params["cookies"]` left in old rows).
  - `run_job` deletes `people.UPLOADS_DIR / job_id` (`shutil.rmtree(..., ignore_errors=True)`) after the job is fully resolved, on every path out - including early returns and the watcher crashing. Saved people's folders are never touched.

- [ ] **Step 1: Write the failing tests `tests/test_worker.py`**

```python
def test_alerts_go_only_to_the_jobs_owner(two_users, monkeypatch):
    a, b = two_users
    storage.save_device("tok-a", a); storage.save_device("tok-b", b)
    sent = []
    monkeypatch.setattr(push, "send", lambda tokens, **kw: (sent.append(tokens), (len(tokens), None))[1])
    job = storage.get_job(storage.create_job(a, "sports", {"match_id": "1", "player": "X"}))
    worker._notify(job, {"kind": "sport", "heard": "X has come in to bat"})
    worker._notify_outcome(job, storage.EXPIRED, "time ran out")
    assert sent == [["tok-a"], ["tok-a"]]

def test_caller_cookies_are_never_used(monkeypatch):
    monkeypatch.setattr(worker, "default_cookies", lambda: "server-cookies.txt")
    cmd = worker.build_command({"kind": "audio", "params": {"url": "u", "listen_for": "x", "cookies": "C:/evil"}})
    assert cmd[cmd.index("--cookies") + 1] == "server-cookies.txt"

def test_job_photo_copy_is_deleted_when_the_job_ends(two_users, monkeypatch, tmp_path):
    # people.UPLOADS_DIR -> tmp_path; job folder tmp_path/<id> with a file;
    # build_command patched to a command that prints one {"event":"finished","outcome":"expired"} line
    # (e.g. [sys.executable, "-c", "print('{\"event\": \"finished\", \"outcome\": \"expired\"}')"]);
    # push.send patched out; run_job(id) -> folder gone, job status "expired".

def test_job_photo_copy_is_deleted_even_if_the_watcher_cannot_start(two_users, monkeypatch, tmp_path):
    # build_command raises KeyError -> job "failed", folder gone
```

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_worker.py -v` - Expected: FAIL.
- [ ] **Step 3:** Implement the changes in `app/worker.py`.
- [ ] **Step 4:** Run `.venv\Scripts\python.exe -m pytest -v` - Expected: all pass.
- [ ] **Step 5: Commit and push.**

---

### Task 6: The phone page - sign-in, saved people, owner-only modes

**Files:**
- Modify: `app/static/index.html`, `app/static/app.js`

**Interfaces:**
- Consumes: every HTTP route from Task 4.
- Produces (in `app.js`):
  - `api(path, options = {}) -> Promise<Response>` - `fetch` with `Authorization: Bearer ${await firebase.auth().currentUser.getIdToken()}`; on a 401 it signs out and shows the sign-in screen; on a 403 from sign-in it shows the server's message. Every existing `fetch("/api/...")` call goes through it.
  - Signed-out screen: title + "Sign in with Google" (`GoogleAuthProvider`). Try `signInWithPopup`; if it fails with `auth/popup-blocked` or the page runs standalone (`matchMedia("(display-mode: standalone)")`), use `signInWithRedirect`.
  - Signed-in header: email + "Sign out". Notifications are set up only after sign-in (device registration needs the user).
  - `GET /api/me` decides the modes: non-owners see only "Cricket" (the face/name buttons are hidden, not just disabled).
  - "Saved people" section per the spec's sketch: list (name, photo count, Watch, Delete with `confirm()`), "+ Add a person" form (name, optional phrase, optional player name, photos - each photo shown through the existing `/api/check-photo` preview before saving), and per-person photo thumbnails (fetched through `api()` as blobs, since `<img>` can't send the token) each with a remove button.
  - Job form: a "Who" picker (saved people, or "someone new"); choosing a person sends `person_id` and pre-fills/hides fields it covers; "someone new" shows today's fields plus a "save this person" box and name field.
  - "Watch again" only when `job.restartable`; otherwise the text "save this person to watch them again".
- `index.html`: add `firebase-auth-compat.js` at `10.12.2`, before `/firebase-config.js`.

- [ ] **Step 1:** Implement the above. Keep the file's existing style (no framework, no build step) and its comments' habit of saying why.
- [ ] **Step 2: Verify by hand on the laptop browser** (server running, `ALLOWED_EMAILS` = owner, `localhost` is authorized in Firebase by default):
  - Signed out: only the sign-in button; `/api/jobs` in devtools returns 401.
  - Sign in as the owner: all three modes; add a person with 2 photos (crop previews shown), remove one, start a cricket watch from them - the player field is pre-filled.
  - Delete the person: gone from the list, and `app/uploads/people/<id>` no longer exists.
- [ ] **Step 3: Commit and push.**

---

### Task 7: Docs, setup instructions, and the two-account check on a phone

**Files:**
- Modify: `.env.example` (add `DATABASE_URL`, `TEST_DATABASE_URL`, `ALLOWED_EMAILS`, `OWNER_EMAILS` with placeholders and one-line comments, including "empty = nobody")
- Modify: `start_app.bat` (after the tunnel URL instructions: add it to Firebase -> Authentication -> Settings -> Authorized domains, or sign-in fails)
- Modify: `CLAUDE.md` (Phase 5 section: what changed, the fail-closed lists, owner-only YouTube and why, `pytest` now exists and how to run it, the authorized-domains gotcha; update "Where it stands")
- Modify: `implementation_plan.txt` (Phase 5 status with what was verified)

- [ ] **Step 1:** Write the doc changes.
- [ ] **Step 2: The real check, with the owner** (needs a second Google account on `ALLOWED_EMAILS`, the tunnel, and its hostname authorized):
  - Owner's phone: sign in, start a cricket watch for a batsman at the crease -> notification arrives on the owner's phone only.
  - Second account (other phone, or the same phone after signing out): sees none of the owner's jobs or people; only the Cricket mode is offered; its cricket watch alerts only its own phone.
  - Both outcomes recorded in `implementation_plan.txt`.
- [ ] **Step 3:** Run `.venv\Scripts\python.exe -m pytest -v` one final time - Expected: all pass.
- [ ] **Step 4: Commit and push.**
