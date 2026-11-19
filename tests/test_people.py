import io

import pytest

from app import people


@pytest.fixture(autouse=True)
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)


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
    p = people.update_person(p["id"], two_users[0], player_name="")
    assert p["player_name"] == "Virat Kohli"
    assert people.update_person(p["id"], two_users[1], name="X") is None


def test_deleting_one_photo_removes_its_file(two_users, tmp_path):
    a, b = two_users
    p = people.create_person(a, "X")
    keep = people.add_photo(p["id"], a, "k.jpg", io.BytesIO(b"k"))
    gone = people.add_photo(p["id"], a, "g.png", io.BytesIO(b"g"))
    gone_path = people.photo_path(p["id"], gone["id"], a)
    assert people.delete_photo(p["id"], gone["id"], b) is False
    assert gone_path.exists()
    assert people.delete_photo(p["id"], gone["id"], a) is True
    assert not gone_path.exists()
    assert people.photo_path(p["id"], gone["id"], a) is None
    assert people.delete_photo(p["id"], gone["id"], a) is False
    assert [x["id"] for x in people.get_person(p["id"], a)["photos"]] == [keep["id"]]


def test_copy_photos_to(two_users, tmp_path):
    a = two_users[0]
    p = people.create_person(a, "X")
    ids = [people.add_photo(p["id"], a, n, io.BytesIO(n.encode()))["id"] for n in ("1.jpg", "2.png")]
    dest = tmp_path / "job" / "photos"
    assert people.copy_photos_to(p["id"], dest) == 2
    assert sorted(f.name for f in dest.iterdir()) == sorted([ids[0] + ".jpg", ids[1] + ".png"])
