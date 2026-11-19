import sys

from app import people, push, storage, worker


def test_alerts_go_only_to_the_jobs_owner(two_users, monkeypatch):
    a, b = two_users
    storage.save_device("tok-a", a)
    storage.save_device("tok-b", b)
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
    assert "C:/evil" not in cmd


def _job_with_photos(user, monkeypatch, tmp_path):
    monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(push, "send", lambda tokens, **kw: (len(tokens), None))
    job_id = storage.create_job(user, "face", {"url": "u"})
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / "p.jpg").write_bytes(b"x")
    other = tmp_path / "saved-person"
    other.mkdir()
    (other / "keep.jpg").write_bytes(b"x")
    return job_id, folder, other


def test_job_photo_copy_is_deleted_when_the_job_ends(two_users, monkeypatch, tmp_path):
    job_id, folder, other = _job_with_photos(two_users[0], monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "build_command", lambda job: [
        sys.executable, "-c", "print('{\"event\": \"finished\", \"outcome\": \"expired\"}')"])
    worker.run_job(job_id)
    assert not folder.exists()
    assert (other / "keep.jpg").exists()
    assert storage.get_job(job_id)["status"] == storage.EXPIRED


def test_job_photo_copy_is_deleted_when_the_watcher_dies_silently(two_users, monkeypatch, tmp_path):
    job_id, folder, _ = _job_with_photos(two_users[0], monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "build_command", lambda job: [sys.executable, "-c", "pass"])
    worker.run_job(job_id)
    assert not folder.exists()
    assert storage.get_job(job_id)["status"] == storage.FAILED


def test_job_photo_copy_is_deleted_even_if_the_watcher_cannot_start(two_users, monkeypatch, tmp_path):
    job_id, folder, _ = _job_with_photos(two_users[0], monkeypatch, tmp_path)

    def boom(job):
        raise KeyError("url")

    monkeypatch.setattr(worker, "build_command", boom)
    worker.run_job(job_id)
    assert not folder.exists()
    assert storage.get_job(job_id)["status"] == storage.FAILED
