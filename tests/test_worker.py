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


def test_an_error_while_handling_events_fails_the_job_without_hanging(two_users, monkeypatch, tmp_path):
    import time
    job_id, folder, _ = _job_with_photos(two_users[0], monkeypatch, tmp_path)
    code = ("import time\n"
            "print('{\"event\": \"started\", \"title\": \"t\"}', flush=True)\n"
            "print('{\"event\": \"alert\", \"at\": \"now\"}', flush=True)\n"
            "time.sleep(30)")
    monkeypatch.setattr(worker, "build_command", lambda job: [sys.executable, "-c", code])
    real = worker._handle_event

    def handle(job, event):
        if event.get("event") == "alert":
            raise RuntimeError("push exploded")
        real(job, event)

    monkeypatch.setattr(worker, "_handle_event", handle)
    start = time.time()
    worker.run_job(job_id)
    assert time.time() - start < 15
    job = storage.get_job(job_id)
    assert job["status"] == storage.FAILED
    assert "RuntimeError" in job["progress"]
    assert not folder.exists()


def test_cleanup_never_removes_the_uploads_dir_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(people, "UPLOADS_DIR", tmp_path)
    (tmp_path / "keep.jpg").write_bytes(b"x")
    worker.run_job("")
    assert (tmp_path / "keep.jpg").exists()
