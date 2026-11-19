"""
Phase 5: the saved-people routes ("favorite-person profiles").

Kept out of main.py only to keep that file readable; the rules are the
same as every other /api route. Every route here takes the signed-in
user, and every lookup goes through people.* with that user's id, so
someone else's person is not found rather than forbidden - a 404 that
reads exactly like an id that never existed, so ids can't be probed.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import auth, people
from .auth import User

router = APIRouter(prefix="/api/people")

NO_SUCH_PERSON = "no such person"
BAD_PHOTO = "photos must be .jpg, .jpeg, .png or .webp"


def is_allowed_photo(filename: str | None) -> bool:
    """The same extension rule people.add_photo enforces, checked early
    so a bad upload can be refused before anything has been created."""
    return Path(filename or "").suffix.lower() in people.PHOTO_EXTENSIONS


def require_name(name: str | None, field: str = "name") -> str:
    # people.update_person stores whatever it's given, and a person with
    # a blank name would also make their listen_for/player_name blank.
    if name is None or not name.strip():
        raise HTTPException(400, f"{field} can't be blank")
    return name.strip()


def _uploaded(photos: list[UploadFile | str]) -> list[UploadFile]:
    # A browser's empty file input still sends a part, with no filename,
    # which arrives as the string "" - a plain list[UploadFile] would
    # reject the whole form with a 422 for it.
    return [p for p in photos if not isinstance(p, str) and p.filename]


@router.get("")
def list_people(user: User = Depends(auth.current_user)):
    return people.list_people(user.id)


@router.post("")
def create_person(
    name: str = Form(...),
    listen_for: str = Form(None),
    player_name: str = Form(None),
    photos: list[UploadFile | str] = File([]),
    user: User = Depends(auth.current_user),
):
    name = require_name(name)
    photos = _uploaded(photos)
    # All or nothing: refuse a bad file before the person exists, rather
    # than leave a half-made person behind a 400.
    if not all(is_allowed_photo(p.filename) for p in photos):
        raise HTTPException(400, BAD_PHOTO)
    person = people.create_person(user.id, name, listen_for, player_name)
    try:
        for photo in photos:
            people.add_photo(person["id"], user.id, photo.filename, photo.file)
    except Exception:
        # Real faces: a failed save must not leave some of them on disk
        # under a person the user was told wasn't created.
        people.delete_person(person["id"], user.id)
        raise
    return people.get_person(person["id"], user.id)


@router.patch("/{person_id}")
def update_person(
    person_id: str,
    name: str = Form(None),
    listen_for: str = Form(None),
    player_name: str = Form(None),
    user: User = Depends(auth.current_user),
):
    """Absent fields are left alone; a blank listen_for/player_name
    clears it back to following the name."""
    if name is not None:
        name = require_name(name)
    person = people.update_person(
        person_id, user.id, name=name, listen_for=listen_for, player_name=player_name
    )
    if person is None:
        raise HTTPException(404, NO_SUCH_PERSON)
    return person


@router.post("/{person_id}/photos")
def add_photo(person_id: str, photo: UploadFile, user: User = Depends(auth.current_user)):
    try:
        added = people.add_photo(person_id, user.id, photo.filename or "", photo.file)
    except people.BadPhoto:
        raise HTTPException(400, BAD_PHOTO)
    if added is None:
        raise HTTPException(404, NO_SUCH_PERSON)
    return added


@router.get("/{person_id}/photos/{photo_id}")
def get_photo(person_id: str, photo_id: str, user: User = Depends(auth.current_user)):
    path = people.photo_path(person_id, photo_id, user.id)
    if path is None or not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path)


@router.delete("/{person_id}/photos/{photo_id}")
def delete_photo(person_id: str, photo_id: str, user: User = Depends(auth.current_user)):
    if not people.delete_photo(person_id, photo_id, user.id):
        raise HTTPException(404, "no such photo")
    return {"ok": True}


@router.delete("/{person_id}")
def delete_person(person_id: str, user: User = Depends(auth.current_user)):
    """Deletes the rows and the whole photo folder. A job already
    running on this person keeps going on its own copy of the photos."""
    if not people.delete_person(person_id, user.id):
        raise HTTPException(404, NO_SUCH_PERSON)
    return {"ok": True}
