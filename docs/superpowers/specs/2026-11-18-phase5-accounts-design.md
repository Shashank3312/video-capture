# Phase 5 design: accounts, saved people, PostgreSQL

Date: 2026-11-18. Status: approved by the owner, 2026-11-18.

## Why

The owner intends to publish the app to the public. Today it is a
single-user app with no sign-in: anyone who reaches the tunnel sees
every job, can cancel any of them, and every alert goes to every phone
that ever registered (`storage.list_device_tokens()` returns all
tokens). Phase 5 (`implementation_plan.txt`) makes it safe for several
people to use at once. It does not make it ready for the public -
hosting, shared jobs, per-user limits and the legal review are Phases
6-7 and stay there.

## Goals

- Sign in with Google (Firebase Authentication).
- Each user sees, cancels, restarts and is alerted about only their own
  jobs. One account may have several phones.
- Saved people ("favorite-person profiles"): a name plus reference
  photos, saved once and reused. Starting a watch from a saved person
  pre-fills the photos (face watch), the phrase to listen for, and the
  player name (cricket).
- A saved person can be deleted, and deleting removes their photos from
  disk, not just from the list.
- Storage moves from SQLite to PostgreSQL.

## Non-goals (later phases)

- Per-user limits, shared jobs for the same stream (Phase 6).
- Cloud hosting, a fixed public address, the legal review, a retention
  policy beyond "delete when the user deletes" (Phase 7).
- The bugs recorded on 2026-11-18 in the plan: 24/7-stream seek links,
  paused matches missing from the picker, the video+audio CPU limit.
  Each is its own change.

## Anti-goals - it fails if any of these happen, even if it "works"

- One user can see, cancel, restart or be alerted about another user's
  job, or open another user's saved person or photos.
- A deleted saved person's photos remain anywhere on disk.
- A job can end without resolving to matched/expired/failed (the rule
  from Phase 4 still holds).
- The watcher scripts change. They stay account-unaware subprocesses
  speaking the same `--json` events; accounts live entirely in `app/`.

## Constraints

- Windows laptop, no Docker. PostgreSQL is installed natively
  (EnterpriseDB installer); the app finds it via `DATABASE_URL` in
  `.env`, so a hosted database later is a config change.
- Firebase is already configured (web config + service account), and
  `firebase-admin` is installed. Google sign-in must be enabled in the
  Firebase console (Authentication -> Sign-in method).
- **Firebase only allows sign-in from domains listed under
  Authentication -> Settings -> Authorized domains, with no wildcards.**
  The free tunnel hostname changes on every restart, so each new one
  must be added there by hand (about 30 seconds). Accepted for
  development; a fixed address is a Phase 6/7 deployment decision.
  `start_app.bat`'s instructions must say so, as they already do for
  the phone's home-screen link.
- The repo is public: no credentials, photos or database files may be
  committed. `DATABASE_URL` lives in `.env` (already ignored);
  `.env.example` gains a placeholder.
- No test suite exists yet. CLAUDE.md asked for real tests once the
  scripts became a service; this phase is where that starts (below).

## Approaches considered

1. **Firebase sign-in + PostgreSQL with plain SQL** - CHOSEN. The page
   signs in with Google and sends the Firebase ID token with every
   request; the server verifies it with `firebase_admin`. `storage.py`
   keeps its current plain-SQL style, rewritten for PostgreSQL.
   Smallest change, familiar code.
2. Same, with SQLAlchemy + Alembic migrations. More structure for
   future schema changes, but a larger rewrite whose machinery doesn't
   pay off at this size.
3. Firestore instead of PostgreSQL. No server to run, library already
   installed - but the plan specifies PostgreSQL, and Phase 6's "who
   else is watching this stream" queries suit SQL.

The owner chose 1.

## Design

### Sign-in and access control

- `index.html` adds `firebase-auth-compat.js` at the same version as the
  existing Firebase scripts (10.12.2). Signed out, the page shows only a
  "Sign in with Google" button. Signed in, it shows the user's email and
  a "Sign out" link.
- Every `fetch` to `/api/*` sends `Authorization: Bearer <ID token>`
  (`currentUser.getIdToken()`, which refreshes expired tokens itself).
- Server: a FastAPI dependency verifies the token with
  `firebase_admin.auth.verify_id_token`, then checks the email is
  verified and on the allow-list `ALLOWED_EMAILS` (comma-separated, in
  `.env`). Missing or invalid token -> 401; valid but not allowed -> 403
  with a message the page shows ("this app is invite-only for now").
  The first request from a user creates their `users` row.
- **Why an allow-list:** until Phase 6 adds per-user limits, open
  sign-up would let any Google account start multi-GB jobs on the
  owner's laptop.
- **It fails closed** (builder's call, from review): `ALLOWED_EMAILS`
  unset or empty lets nobody in, and the server says so at startup.
  Opening it to everyone takes an explicit `ALLOWED_EMAILS=*` - that is
  the public-launch switch. Forgetting a setting must never be what
  opens the app to strangers.
- Every `/api/*` route requires sign-in except `/api/health`. Static
  files, `/firebase-config.js` and the service worker stay public (the
  page must load before anyone can sign in).
- Another user's job or saved person returns **404, not 403**, so ids
  can't be probed for existence.

### Data model (PostgreSQL)

```
users         id TEXT PK (Firebase uid), email, display_name, created_at
devices       token TEXT PK, user_id -> users, created_at
people        id TEXT PK, user_id -> users, name, listen_for,
              player_name, created_at
person_photos id TEXT PK, person_id -> people ON DELETE CASCADE,
              filename, created_at
jobs          as today (id, kind, status, params JSONB, event_title,
              link, outcome, progress, matched_at, created_at,
              finished_at, notified, alerts_log JSONB)
              + user_id -> users, + person_id -> people ON DELETE SET NULL
```

- A device token re-registered by a different user moves to that user
  (a shared phone, a sign-out/sign-in) - upsert on token.
- Timestamps stay ISO-8601 UTC as today, or become `TIMESTAMPTZ`; the
  page's existing display must keep working either way.
- Schema is created at startup with `CREATE TABLE IF NOT EXISTS`, the
  same pattern `init_db()` uses now. No migration of `app/jobs.db`: its
  rows are test jobs; the file is left in place, unused.

### Saved people and their photos

- Photos live on disk at `app/uploads/people/<person_id>/` (already
  under the ignored `app/uploads/`); the database holds filenames.
  Upload filenames are reduced to `.name` as today, and every path is
  built from ids, never from user input.
- What a saved person is (builder's call, from review): a required
  `name`, plus optional `listen_for` (the phrase Whisper should hear)
  and `player_name` (the Cricbuzz name, e.g. "V Kohli"), each
  defaulting to `name` when blank, since they genuinely differ for
  cricketers. **Photos are optional**: a cricketer or a name-only
  person has none. Only a face watch requires the person to have at
  least one photo, and the page offers face watching only for people
  who do.
- Adding a person: name, the two optional fields, and zero or more
  photos. Each photo goes through the existing `/api/check-photo`
  preview first, so the user sees the exact face crop - this was added
  for a real failure (sunglasses) and must stay in the flow.
- Editing (builder's call, from review): photos can be added to or
  removed from a saved person - replacing one bad photo is the common
  case - and the three name fields can be changed. Removing a photo
  deletes its file. One-off watches keep today's single photo upload.
- Deleting a person deletes the rows and the whole folder, then
  confirms. If a job using that person is running, it keeps running on
  its own copy (next point).
- A job started from a saved person **copies** the photos into the
  per-job folder `app/uploads/<job_id>/`, which is what the watcher
  already reads (`--photos-dir`). Jobs stay isolated from later edits
  and deletions. Per-job copies are deleted when the job finishes, which
  restores the original promise for anything not deliberately saved.
- A one-off watch ("someone new") works as today, with an optional
  "save this person" tick box.
- "Watch again" (builder's call, from review) - today `restart_job`
  reuses the old `photos_dir`, which no longer exists once the copy is
  deleted:
  - From a saved person (`person_id` set, person still exists): re-copy
    the person's CURRENT photos into the new job's folder. Using an
    improved photo set is what someone restarting would want.
  - One-off face watch, or its saved person was deleted: "Watch again"
    is not offered; the page says to save the person, or start a new
    watch with a photo. Keeping one-off photos around just to allow a
    restart would undo the deletion promise.
  - Name-only and cricket watches restart as today (no photos).

### Alerts go only to the owner

- `storage.list_device_tokens(user_id)` takes the owner; `_notify` and
  `_notify_outcome` in `worker.py` pass the job's `user_id`.
  `/api/test-notification` sends only to the caller's devices.
- `/api/health` keeps working signed out but stops reporting a global
  device count (it would leak how many users exist); it reports only
  whether push is configured.

### YouTube access for other users' jobs

- **The `cookies` form field on `POST /api/jobs` is removed** (builder's
  call, from review). It is a server file path passed straight to
  yt-dlp's `--cookies`, which reads AND writes that file - with several
  users, anyone on the list could point it at any path on the laptop.
  Jobs use only the server's own `default_cookies()`.
- **YouTube watches are owner-only** (owner's decision, option B).
  Every YouTube job runs on the owner's signed-in YouTube session, so
  YouTube would see other users' watches as the owner's account -
  risking that account being flagged, and one block stopping everyone.
  Until Phase 7 solves YouTube access properly:
  - `OWNER_EMAILS` in `.env` (comma-separated; fails closed like the
    allow-list) names who may start face and name watches (`kind`
    video/audio). Anyone else gets 403 on those kinds, server-side -
    hiding the buttons is not the protection.
  - The page shows other users only the Cricket option. Saved people
    still work for them (name and player name; photos are allowed but
    only become useful once they can run face watches).
  - Opening YouTube watches to everyone is a Phase 7 decision, not a
    config flip to make casually.

### Page changes

```
[signed out]                     [signed in]
Don't Miss The Moment            Don't Miss The Moment   you@gmail.com · Sign out
                                 Saved people
 [ Sign in with Google ]           Ram Charan  (3 photos)   [Watch] [Delete]
                                   + Add a person
                                 What should I do?  (as today)
                                   Who: [Ram Charan v] / someone new
                                 Your watches  (only yours)
```

### Testing

The first automated tests in the repo, with pytest:
- Storage against a separate test database (`TEST_DATABASE_URL`).
- API ownership: two fake users (the auth dependency overridden), and
  each one gets 404 on the other's job and person, sees only their own
  lists, and a job's notification targets only its owner's tokens.
- Access rules: empty `ALLOWED_EMAILS` admits nobody; a non-owner gets
  403 on a video/audio job but can start a cricket one.
- Deleting a person removes their folder from disk.

Then the real check, by hand: two Google accounts on the allow-list,
each starts a watch from a phone; each sees only their own jobs, and
each alert arrives only on the phone of the account that started it.

## Left to the builder

- Connection handling (`psycopg` 3 with a small pool is the expected
  choice; the worker writes from threads, as today).
- Exact page layout and wording, within the sketch above.
- Whether `signInWithPopup` or `signInWithRedirect` (popups can be
  blocked in an installed home-screen app; verify on the phone).
- Removing tokens Firebase reports as no longer registered (one of the
  three registered tokens is already stale) - nice to have.

## Owner setup (cannot be done by the builder)

1. Install PostgreSQL for Windows, create a database, put its URL in
   `.env` as `DATABASE_URL`.
2. Firebase console: enable the Google sign-in provider; add the
   current tunnel hostname to Authorized domains.
3. Put the allowed Google email(s) in `.env` as `ALLOWED_EMAILS`, and
   the owner's in `OWNER_EMAILS`.
