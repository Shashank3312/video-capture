# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This is a pre-code planning stage. The only file in this repository is
`implementation_plan.txt`, which is the source of truth for what this
project is and how it's meant to be built. Read it before doing any
work here — there is no existing code, build system, test suite, or
architecture to infer anything from yet.

## What the project is (summary — see implementation_plan.txt for full detail)

An app that watches a live event on the user's behalf (sports match,
pre-release/political event stream, or a local event video) and sends
a push notification the moment the user's favorite person is about to
appear/play/perform, instead of the user having to watch the whole
thing. A later phase adds an actual phone call (via Twilio) instead of
just a notification.

Two watcher tracks, chosen deliberately after weighing tradeoffs (see
plan for the reasoning):
- **Track A (sports)**: polls a live scoreboard/data API (no video,
  no ML) to detect when the favorite player is currently
  batting/bowling/serving/etc.
- **Track B (general stream watcher)**: used for both local events and
  pre-release/political events. Pulls a live video stream, samples
  frames, and face-matches against a reference photo. Has an optional
  opt-in add-on that also runs speech-to-text on the stream's audio to
  detect the person's name being mentioned, as a lower-confidence
  secondary signal.

## Build order

The plan is explicitly phased (Phase 0 through Phase 8 in
`implementation_plan.txt`), starting with the cheapest, ML-free
prototype (Track A sports scoreboard polling) before touching video or
face recognition. Do not skip ahead to multi-user/scaling work
(Phases 5+) before the single-user MVP (Phases 0-4) is working and
validated — this was a deliberate decision, not an oversight.

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
open (which sports API, which speech-to-text provider, legal
boundaries on which streams to process) — don't assume answers to
these; ask or research when that phase is actually reached.
