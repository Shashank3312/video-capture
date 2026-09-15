# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What the project is

An app ("Don't Miss The Moment") that watches a live event on the
user's behalf and sends a push notification the moment his favorite
person is about to appear/play/perform, instead of him having to
watch the whole thing. A later phase adds an actual phone call (via
Twilio) instead of just a notification.

`implementation_plan.txt` is the source of truth for the full design
and phased build order - read it before planning any new work here.
It's kept up to date as decisions get made (e.g. which API to use),
not just as an initial plan, so re-read it if it's been a while.

Two watcher tracks:
- **Track A (sports)**: polls a live scoreboard/data API (no video,
  no ML) to detect when the favorite player is currently
  batting/bowling/etc. Cheapest track, built first.
- **Track B (general stream watcher)**: pulls a live video stream,
  samples frames, and face-matches against reference photos, for local
  events and pre-release/political events. Also runs speech-to-text on
  the stream's audio to catch the person's NAME being said - which
  measures as the more reliable of the two signals.

Build order is deliberately phased (Phase 0 through Phase 8 in
`implementation_plan.txt`): single-user MVP (Phases 0-4) before any
multi-user/scaling work (Phases 5+), and Track A before Track B. Don't
skip ahead.

**Where it stands.** Phases 0-4 are built and verified for both
tracks: a job started from a phone alerts, the notification arrives,
and tapping it opens the live event (the stream at the moment it
happened for Track B, the Cricbuzz scoreboard for Track A). Phase 1's
manual accuracy check (below) is also now closed out, so there's
nothing outstanding in Phases 0-4 - Phase 5 is next.

## Track A: sports scoreboard watcher (`track_a_sports/`)

Data source is the **Cricbuzz Cricket API via RapidAPI** (host
`cricbuzz-cricket.p.rapidapi.com`), reached only through `RAPIDAPI_KEY`
in `.env`. This was not the first choice - CricketData.org's free tier
turned out not to expose current batsman/bowler at all, and a free
no-key wrapper (pycricbuzz) turned out to be dead. It's an unofficial
mirror of Cricbuzz's own data, not a documented public API - same
licensing caveat as Track B's video sources, to be revisited before
any public launch (see `implementation_plan.txt` section 6/Phase 7).

Field meanings driving the matching logic were confirmed against real
live responses, not official docs, and have real gotchas future work
should know about:
- A batsman is currently at the crease when `outdec == "batting"` -
  but a batsman who was *not out when an innings ended* (all out or
  declared) keeps that same value forever. `current_innings()` in
  `watch.py` accounts for this by also requiring `wickets < 10` and
  `not isdeclared`, and scanning innings most-recent-first.
- There's no explicit "current bowler" field. It's inferred: on the
  first poll, whoever has a fractional `overs` value (e.g. `"22.3"`)
  is mid-over right now; on later polls, whoever's ball count
  increased the *most* since the last poll (not just "increased at
  all" - a slow poll can span more than one delivery).
- Squad endpoints (`/mcenter/v1/{id}/team/{teamId}`) group players by
  an explicit `category`: `"playing XI"`, `"bench"`, `"support
  staff"`. Only `"playing XI"` counts for "is this player even in the
  match" validation.

Scripts:
- `list_live_matches.py` - prints in-progress matches with their
  `match_id`.
- `explore_api.py` - dumps raw API responses; the way to investigate
  the data shape before changing matching logic, rather than guessing.
- `watch.py` - the actual watcher. Validates the player is in either
  team's playing XI before polling (exits immediately with a message
  if not), then polls the scorecard, alerts once on the transition
  into batting/bowling (immediately if already batting/mid-over when
  it starts watching), prints `Task completed.`, and exits. It's
  single-shot by design, not a continuous monitor. `--json` makes it
  emit events the Phase 4 worker can read, and `--max-minutes` lets a
  job give up rather than polling forever. Both tracks must keep
  speaking the same event format: the worker only reads lines that
  parse as JSON, so a watcher printing prose is a watcher the app
  cannot hear, however well it works at a terminal.

Other sports (beyond cricket) need their own API research when work
reaches them. Kabaddi was researched and paused - not a feasibility
problem, a cost one (see `implementation_plan.txt` section 6 for the
specific providers and prices already checked, so that research isn't
repeated).

### Running it

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r track_a_sports\requirements.txt
```

Copy `.env.example` to `.env` and fill in `RAPIDAPI_KEY` (subscribe to
the "Cricbuzz Cricket" API by cricketapilive on RapidAPI first).

```
.venv\Scripts\python.exe track_a_sports\list_live_matches.py
.venv\Scripts\python.exe track_a_sports\watch.py <match_id> "<player name>" --interval 60
```

There's no test suite, linter, or build step yet - this is still
early, script-level prototyping (Phase 0). Verification so far has
been running scripts against real live matches and checking the
output by hand; add real tests when this gets wrapped into the
service in Phase 4.

## Track B: general stream watcher (`track_b_video/`)

Phases 1, 2 and 3 are built (`implementation_plan.txt` section 5):
- `watch_local_video.py` (Phase 1) takes reference photo(s) and a
  local video file, and prints the timestamps where that face appears.
- `watch_live_stream.py` (Phase 2) watches a real YouTube Live stream
  and alerts once per appearance, verified against a live news stream.
- `listen_for_name.py` + `name_mentions.py` (Phase 3) find a spoken
  name in a recording; `--listen-for` adds the same to the live
  watcher, and `--audio-only` runs it with no video and no photos.
- `benchmark_detectors.py` compares detector backends on real footage.

The per-frame matching lives in `match_frame()` in
`watch_local_video.py` and is imported by the live watcher rather than
duplicated - change it once, both paths follow.

Approach, and why:
- Uses **DeepFace** (`DeepFace.represent()`) to get one face embedding
  per reference photo, then compares every sampled frame's face
  embedding against all reference embeddings with
  `deepface.modules.verification.find_distance()`. The cutoff is
  `match_threshold()`, NOT DeepFace's `find_threshold()` - see the
  threshold note under Phase 2 for the measurements behind that.
- Samples the video at a fixed interval (default 1s), not every frame
  - most of a video is redundant for this purpose.
- **Every** face in a sampled frame is compared, not just the first one
  DeepFace returns. Real footage routinely has several people on
  screen (up to 15 in one benchmarked frame), and checking only the
  first face missed most real appearances - on a benchmark clip mtcnn
  found 9 matching frames checking all faces vs. 2 checking only the
  first. It costs nothing extra: DeepFace already computes an
  embedding for every detected face regardless.
- Detection and embedding are deliberately split (`extract_faces()`,
  then one batched `represent()` call with `detector_backend="skip"`).
  Detection is cheap and finds everyone; embedding is what costs, and
  it scales with face count. Splitting them allows skipping faces
  below `--min-face-area` before paying for them: 160s -> 62s on a
  57-frame clip, same matches. Keep the embedding call batched - one
  `represent()` per face instead of one per frame measured 6.1s vs
  2.5s on a 16-face frame, because DeepFace batches the whole list
  into a single forward pass.
- That size floor is NOT an audience/crowd-skipping rule by face
  count. Skipping "crowded" frames was the first idea and it is wrong
  for this domain: on the project's own test footage the strongest
  matches are in 14- and 16-face frames, because a stage or press
  shot has the target *in* the crowd. Size works where count doesn't,
  since distant audience faces are small and the person on stage
  isn't. See the note on `DEFAULT_MIN_FACE_AREA` before changing it.
- Consecutive matching samples are grouped into "appearance" segments
  in the summary, mirroring Track A's debounce idea: report continuous
  appearances, not one alert per sampled hit.
- Default detector backend is `yolov11m`, chosen from real measured
  numbers via `benchmark_detectors.py`, not published benchmarks -
  same accuracy as the previous `mtcnn` default but meaningfully
  faster. `retinaface` is the most sensitive but ~16s/frame here,
  which rules it out for Phase 2's live streams; `yolov11n` is the
  speed option. Don't pick a detector for this project without
  re-running that benchmark.
- Reference photos larger than 1600px per side are downscaled before
  detection. Detector cost scales with image size, and a
  full-resolution 3400x5100 poster made the script look completely
  hung for many minutes on a single photo.
- Multiple varied reference photos (different angle/lighting) are
  expected, per Phase 1's plan - a photo with no detectable face is
  skipped with a warning, not fatal unless none are usable. `.webp` is
  accepted alongside jpg/jpeg/png.

Test assets (`track_b_video/test_data/`: reference photos and test
videos) are gitignored and must never be committed - this repo is
public and these are real people's images/footage. Same legal/privacy
caveat as Track A's data source applies here too (see
`implementation_plan.txt` section 6/Phase 7): revisit before any
public launch.

### Running it

```
.venv\Scripts\python.exe -m pip install -r track_b_video\requirements.txt
```

Set `PYTHONUTF8=1` in the shell before running anything DeepFace-related
- its logger prints emoji, which crashes with `UnicodeEncodeError` on
Windows' default console encoding otherwise (a real bug hit during
setup, not a hypothetical).

**Do not let `opencv-python` upgrade to 5.x.** It's pinned to 4.x in
`requirements.txt` for a reason: the 5.0.0.93 wheel has no
`cv2.CascadeClassifier` and ships no haarcascade data files. That
breaks the `opencv` and `ssd` backends outright, and also breaks
`yolov11`/`yolov12`, because DeepFace falls back to the cascade
classifier to locate eyes for alignment whenever a detector returns no
eye landmarks (see `deepface/modules/detection.py`, and the "for v11
keypoints are always None" comment in its `Yolo.py`). This was
misdiagnosed for a long time as a missing XML data file; it is a
wheel-version problem, and downgrading is the fix.

Drop reference photos into `track_b_video/test_data/reference_photos/`
and a test clip into `track_b_video/test_data/videos/`, then:

```
.venv\Scripts\python.exe track_b_video\watch_local_video.py <video_path> --interval 1.0
```

`benchmark_detectors.py` compares detector backends on real footage -
speed, how many frames they find a face in, and how many frames match,
with every detector scored on byte-identical frames. Re-run it rather
than guessing (or trusting published benchmarks) whenever the detector
choice is in question:

```
.venv\Scripts\python.exe track_b_video\benchmark_detectors.py <video_path> --interval 2.0
```

### Phase 2: the live watcher

```
.venv\Scripts\python.exe track_b_video\watch_live_stream.py <youtube_url> --max-minutes 5 --cookies <path\to\cookies.txt>
```

Things that will otherwise cost an hour to rediscover:
- **YouTube demands a signed-in session.** A plain request gets "Sign
  in to confirm you're not a bot". Export a cookies.txt with a "Get
  cookies.txt" extension **while on youtube.com** - an export taken on
  any other tab carries Google cookies but not YouTube's `LOGIN_INFO`,
  and is rejected exactly the same way. That file is a live account
  session: it's gitignored, and it must stay that way.
- **Ask yt-dlp for `bestvideo`, never `best`.** Live streams come as
  separate video-only and audio-only renditions with no combined one,
  so `best` (which means "carries both") matches nothing and fails
  with "Requested format is not available". Audio isn't needed until
  Phase 3 anyway.
- **No ffmpeg install needed** - the opencv-python wheel bundles it,
  so `cv2.VideoCapture` opens the HLS URL directly.
- `OPENCV_FFMPEG_LOGLEVEL` is set before `import cv2`, because YouTube
  rotates CDN hosts mid-stream and FFmpeg otherwise drowns the output
  in "Cannot reuse HTTP connection" warnings.
- **Always read the newest frame, never the next one.** `LiveFrameReader`
  runs a thread that continuously drains the capture and keeps only the
  latest frame. Reading in order would put the watcher further behind
  the live edge with every frame until it alerts about something that
  happened minutes ago - which would defeat the entire point.
- **The match threshold is 0.55, not DeepFace's 0.68** (see
  `TUNED_THRESHOLDS` in `watch_local_video.py`). DeepFace's number is
  tuned on clean photo-to-photo benchmarks; on compressed video frames
  it is too permissive. A good, clear photo of Putin matched Donald
  Tusk on a news stream at 0.633 - different people, same demographic,
  same press-conference lighting. Measured against a face taken from
  the stream itself, 0.50-0.62 removed every false alarm while keeping
  all 13 true matches; below 0.45 real matches start disappearing.
  Re-measure before changing it, and note it's set for VGG-Face only,
  since that's what it was measured on.
- **A reference photo with covered eyes is close to useless.** A photo
  in sunglasses produced an embedding with so little identity in it
  that a STRANGER scored 0.396 against it while the person themselves
  scored 0.531 in their own video - the ranking inverted, which no
  threshold can fix. `/api/check-photo` now shows the user the exact
  crop that will be searched for, because nothing in the app could
  otherwise reveal that.
- Face matching remains the weakest part of this project. Name
  detection measured 5 real mentions and 0 false positives on the same
  footage; treat the audio signal as the more trustworthy one.

### Phase 3: listening for a name

Three modes, and the mode is the user's choice, not a default we
impose: video only, video + `--listen-for TEXT`, or `--audio-only`
with `--listen-for TEXT` (no reference photos needed at all).
`--alert-mode once|cooldown|every` controls how often it reports a
recurring word.

```
.venv\Scripts\python.exe track_b_video\listen_for_name.py <media_path> "Ram Charan"
.venv\Scripts\python.exe track_b_video\watch_live_stream.py <url> --audio-only --listen-for "Ram Charan" --cookies <cookies.txt>
```

- **Translate, don't transcribe.** Measured on a Telugu clip:
  transcribing took 565s for 113s of audio and returned nothing
  usable; translating to English took 90s, faster than real time, and
  read cleanly. English output also romanises names, which is how a
  user would type them.
- **Match names fuzzily** (`name_mentions.py`). Whisper wrote
  "Ramcharan" as one word, so a plain search for "ram charan" found
  nothing, and the `base` model wrote "Sucumar" for Sukumar.
  Normalising away case/spaces/punctuation fixes the first, the
  similarity threshold the second.
- **Don't drop below the `small` model.** tiny (2.9x real time) was
  incoherent and base (2.6x) mangled names; small runs at 1.3-2.2x,
  still ahead of real time, and got every name tried.
- Transcription runs on its own thread, because a window takes several
  seconds and doing it inline would freeze face matching - video is
  the primary signal and has to stay responsive.
- Audio renditions are found by looking for formats with **no video
  codec**. Don't also require `acodec` to be set: YouTube's HLS audio
  formats report it as `None`, so requiring it silently excludes
  exactly the formats you want.

## Verification: there is no test suite

Nothing here is checked by automated tests, and the phases don't ask
for them - they ask for honest measurement against real footage. That
has been the pattern throughout, and it has repeatedly overturned
plausible assumptions:

- A detector was picked by benchmarking 9 of them on byte-identical
  frames, not from published numbers (`benchmark_detectors.py`).
- The crowd-skipping rule was built by face COUNT, then rewritten by
  face SIZE after real footage showed the strongest matches were in
  14- and 16-face frames.
- The match threshold was moved off DeepFace's default only after
  measuring true and false distances on a live stream.
- Memory was called stable at 30 minutes, then shown to drift
  ~300 MB/hour over a full hour.

So when changing anything in the matching path, the expectation is a
measurement on real footage, with the numbers written into the comment
next to the constant. Several constants carry their evidence inline
for exactly this reason - `DEFAULT_MIN_FACE_AREA`, `TUNED_THRESHOLDS`,
the detector table in `watch_local_video.py`'s docstring - and they
should not be changed without re-measuring.

Ad-hoc verification scripts get written into the scratchpad rather
than the repo; only `benchmark_detectors.py` was worth keeping.

## Phase 4: the notifying service (`app/`)

FastAPI + SQLite + a mobile web page. Start a watch job from a phone,
a worker runs it, and a push notification arrives when the person
shows up.

```
start_app.bat          <- starts both, prints where to find the URL
```

or by hand:

```
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
<cloudflared> tunnel --url http://localhost:8000
```

**The tunnel URL expires and changes.** Cloudflare's free quick tunnels
die after several hours (`Unauthorized: Tunnel not found` in the
tunnel window) - the server keeps running fine, so the symptom is just
"the link stopped working". Restart the tunnel, take the new URL, and
re-add it to the phone's home screen; the old icon points at a dead
address. Nothing needs changing in Google Cloud as long as the API
key's allowed referrers use `*.trycloudflare.com/*` rather than one
exact hostname. A fixed address is a Phase 6 deployment question.

- `storage.py` - jobs in SQLite. Every job ends as **matched /
  expired / failed** with a reason that agrees with the outcome.
  Reporting "matched" while the reason read "the limit ran out" was a
  real bug; so was letting an expired job end silently. A job that
  finds nothing still notifies - "they never turned up" is the answer
  the user was waiting for.
- `worker.py` - runs watchers as **subprocesses**, reading their
  `--json` events. Not threads: the models are hundreds of megabytes
  and a process exit frees them reliably, a crash takes the job rather
  than the server, and cancelling is just a terminate.
- `push.py` - FCM. Optional: with no service account key the app still
  runs jobs, it just says notifications are off.
- `cricket.py` - live matches for the picker, because a Cricbuzz match
  id is an internal number no user can know. The API answers **204 No
  Content with an empty body** when no cricket is on, which is a
  success code, so `raise_for_status()` passes and `.json()` then dies
  with "Expecting value: line 1 column 1" - a parse error that reads
  like a broken API rather than a quiet afternoon.
- `POST /api/check-photo` returns the exact face crop a reference
  photo yields, so a user can see what will actually be searched for
  before starting a job. Added because a photo in sunglasses was
  accepted silently and then matched strangers better than the real
  person, and nothing in the app could reveal why.
- A sports job's notification link falls back to Cricbuzz's live-score
  page (`_scoreboard_url()` in `worker.py`) - a real cricket alert
  shipped with `link=None` until this existed, because the link logic
  only ever checked for a stream `seek_url` or a `url` param, both
  Track B concepts. The bare match id resolves with no slug needed.

Alerts carry **where in the stream** they happened, not just the wall
clock: yt-dlp's `release_timestamp` gives the stream's start, so an
alert reports "2:14:07 into the stream" and the notification links to
`?t=<seconds>`. This matters more than it sounds - a notification is
read minutes later, by which point a link to the stream lands on the
live edge, which is never where the thing happened.

Things that cost hours and will again:
- **A restricted Google API key blocks the tunnel domain.** The
  browser says "Installations: Create Installation request failed ...
  referer ... are blocked" and never mentions the API key. Allowed
  referrers (Google Cloud console → Credentials) must include wherever
  the app is served from - `*.trycloudflare.com/*` while testing.
- **Firebase rejects a non-HTTPS tap-through link and drops the whole
  notification with it.** The app learns its public address from
  incoming requests because a tunnel hostname changes every restart.
- **`new Notification(...)` is an illegal constructor on Android
  Chrome** - use `registration.showNotification()`. This is why a
  notification arriving while the page was open never appeared.
- The Firebase **web config and VAPID key are public** (they ship to
  every browser); the **service account JSON is not**. It's found by
  reading its contents, not its filename - the real download was
  called "firebase admin SDK.json" and matched no sensible pattern.
- Secret scanning will flag the Google API key wherever it's
  committed, so the web config is served from an untracked
  `firebase-web-config.json` (template: `.example.json`).

No auth: anyone who reaches the tunnel can start jobs. That's Phase 5.

## Git workflow

This repo is scoped to just this project (`Shashank3312/video-capture`
on GitHub, remote `origin`) — independent of any outer/home-directory
git repo, which should never be touched from here.

Commit and push regularly as work happens, not just at the end of a
session, so nothing is ever lost and any point can be reverted to:
- After finishing a meaningful chunk of work (a phase from the plan, a
  working script, a fix, a notable file added) — not after every tiny
  edit, but don't let uncommitted work pile up either.
- Write clean, specific commit messages that explain *why*, not just
  *what* (e.g. "Add cricket scoreboard polling for Track A" rather
  than "update files").
- Push to `origin` right after committing, so GitHub always reflects
  local state.
- Never force-push, reset --hard, or rewrite history without asking
  first — the whole point of this workflow is to never lose work.

## Open decisions

`implementation_plan.txt` section 6 lists decisions intentionally left
open or already resolved with notes on why (sports API choice, TTS
provider, legal boundaries, Kabaddi's cost blocker) — check there and
don't re-research or re-assume before reading it.
