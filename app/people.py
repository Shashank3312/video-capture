"""
Phase 5: a user's saved people ("favorite-person profiles") and their
reference photos on disk.

A person is saved once and reused by every job, so the photos live in
one folder per person, not one per job. These are real people's faces,
so a deleted person or photo must leave no file behind.
"""

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .db import connection

APP_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = APP_DIR / "uploads"

PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class BadPhoto(ValueError):
    """The upload's extension is not an allowed image type."""


def people_dir() -> Path:
    # Read UPLOADS_DIR at call time, never at import: tests (and later
    # code) repoint it, and a captured copy would silently ignore that.
    return UPLOADS_DIR / "people"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _build(row: dict, photos: list[dict]) -> dict:
    # Blank optional fields are stored as NULL and resolved here, so
    # renaming a person keeps following the name until overridden.
    return {
        "id": row["id"],
        "name": row["name"],
        "listen_for": row["listen_for"] or row["name"],
        "player_name": row["player_name"] or row["name"],
        "photos": photos,
        "created_at": row["created_at"],
    }


def _photos_of(conn, person_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {pid: [] for pid in person_ids}
    if person_ids:
        rows = conn.execute(
            "SELECT id, person_id FROM person_photos "
            "WHERE person_id = ANY(%s) ORDER BY created_at, id",
            (person_ids,),
        ).fetchall()
        for r in rows:
            out[r["person_id"]].append({"id": r["id"]})
    return out


def create_person(user_id: str, name: str, listen_for: str | None = None,
                  player_name: str | None = None) -> dict:
    person_id = _new_id()
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO people (id, user_id, name, listen_for, player_name, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (person_id, user_id, name, listen_for or None, player_name or None, _now()),
        ).fetchone()
    return _build(row, [])


def list_people(user_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM people WHERE user_id = %s ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
        photos = _photos_of(conn, [r["id"] for r in rows])
    return [_build(r, photos[r["id"]]) for r in rows]


def get_person(person_id: str, user_id: str) -> dict | None:
    with connection() as conn:
        # Ownership in SQL: someone else's person is simply not found.
        row = conn.execute(
            "SELECT * FROM people WHERE id = %s AND user_id = %s", (person_id, user_id)
        ).fetchone()
        if row is None:
            return None
        photos = _photos_of(conn, [person_id])
    return _build(row, photos[person_id])


def update_person(person_id: str, user_id: str, *, name: str | None = None,
                  listen_for: str | None = None,
                  player_name: str | None = None) -> dict | None:
    # None means "leave alone"; "" means "clear back to the name".
    sets, params = [], []
    for column, value in (("name", name), ("listen_for", listen_for),
                          ("player_name", player_name)):
        if value is not None:
            sets.append(f"{column} = %s")
            params.append(value if (column == "name" or value != "") else None)
    if sets:
        with connection() as conn:
            conn.execute(
                f"UPDATE people SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
                (*params, person_id, user_id),
            )
    return get_person(person_id, user_id)


def add_photo(person_id: str, user_id: str, filename: str, data: BinaryIO) -> dict | None:
    # Only the lowercased suffix of the upload's name is ever used; the
    # rest could be a path ("../../x") and is never trusted.
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTENSIONS:
        raise BadPhoto(f"Photos must be one of: {', '.join(PHOTO_EXTENSIONS)}")
    if get_person(person_id, user_id) is None:
        return None
    photo_id = _new_id()
    folder = people_dir() / person_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (photo_id + ext)
    try:
        with open(path, "wb") as f:
            shutil.copyfileobj(data, f)
        with connection() as conn:
            conn.execute(
                "INSERT INTO person_photos (id, person_id, filename, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (photo_id, person_id, photo_id + ext, _now()),
            )
    except Exception:
        # A failed copy or insert leaves no row, so nothing would ever
        # delete this (possibly partial) face image.
        path.unlink(missing_ok=True)
        raise
    return {"id": photo_id}


def _photo_row(conn, person_id: str, photo_id: str, user_id: str) -> dict | None:
    return conn.execute(
        "SELECT ph.id, ph.filename FROM person_photos ph "
        "JOIN people p ON p.id = ph.person_id "
        "WHERE ph.id = %s AND ph.person_id = %s AND p.user_id = %s",
        (photo_id, person_id, user_id),
    ).fetchone()


def photo_path(person_id: str, photo_id: str, user_id: str) -> Path | None:
    with connection() as conn:
        row = _photo_row(conn, person_id, photo_id, user_id)
    return people_dir() / person_id / row["filename"] if row else None


def delete_photo(person_id: str, photo_id: str, user_id: str) -> bool:
    with connection() as conn:
        row = _photo_row(conn, person_id, photo_id, user_id)
        if row is None:
            return False
        conn.execute("DELETE FROM person_photos WHERE id = %s", (photo_id,))
    (people_dir() / person_id / row["filename"]).unlink(missing_ok=True)
    return True


def delete_person(person_id: str, user_id: str) -> bool:
    with connection() as conn:
        deleted = conn.execute(
            "DELETE FROM people WHERE id = %s AND user_id = %s", (person_id, user_id)
        ).rowcount
    if not deleted:
        return False
    shutil.rmtree(people_dir() / person_id, ignore_errors=True)
    return True


def copy_photos_to(person_id: str, dest: Path) -> int:
    """Copy every photo into `dest`. No owner check: callers check first."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT filename FROM person_photos WHERE person_id = %s ORDER BY created_at, id",
            (person_id,),
        ).fetchall()
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for r in rows:
        src = people_dir() / person_id / r["filename"]
        if src.exists():
            shutil.copy2(src, dest / r["filename"])
            count += 1
    return count
