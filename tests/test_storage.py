from app import storage


def test_jobs_are_scoped_to_their_owner(two_users):
    a, b = two_users
    job = storage.create_job(a, "sports", {"match_id": "1", "player": "X"})
    assert storage.get_job(job, a)["params"] == {"match_id": "1", "player": "X"}
    assert storage.get_job(job, b) is None
    assert storage.get_job(job)["user_id"] == a  # worker view
    assert [j["id"] for j in storage.list_jobs(a)] == [job]
    assert storage.list_jobs(b) == []


def test_finishing_stamps_finished_at(two_users):
    job = storage.create_job(two_users[0], "sports", {})
    storage.update_job(job, status=storage.MATCHED, outcome="matched")
    assert storage.get_job(job)["finished_at"]


def test_alerts_append_in_order(two_users):
    job = storage.create_job(two_users[0], "sports", {})
    storage.add_alert(job, {"at": "1"})
    storage.add_alert(job, {"at": "2"})
    assert [a["at"] for a in storage.get_job(job)["alerts_log"]] == ["1", "2"]


def test_device_tokens_belong_to_one_user(two_users):
    a, b = two_users
    storage.save_device("tok-1", a)
    assert storage.list_device_tokens(a) == ["tok-1"]
    storage.save_device("tok-1", b)  # signed in as someone else
    assert storage.list_device_tokens(a) == []
    assert storage.list_device_tokens(b) == ["tok-1"]


def test_update_job_stores_dicts_as_jsonb(two_users):
    job = storage.create_job(two_users[0], "sports", {})
    storage.update_job(job, params={"k": [1, 2]})
    assert storage.get_job(job)["params"] == {"k": [1, 2]}
