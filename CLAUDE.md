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
- **Track B (general stream watcher)**: not yet built. Will pull a
  live video stream, sample frames, and face-match against a
  reference photo, for local events and pre-release/political events.
  Has a planned opt-in add-on that also runs speech-to-text on the
  stream's audio to catch the person's name being mentioned, as a
  lower-confidence secondary signal.

Build order is deliberately phased (Phase 0 through Phase 8 in
`implementation_plan.txt`): single-user MVP (Phases 0-4) before any
multi-user/scaling work (Phases 5+), and Track A before Track B. Don't
skip ahead.

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
  single-shot by design, not a continuous monitor.

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
