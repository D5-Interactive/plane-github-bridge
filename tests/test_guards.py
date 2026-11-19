import pytest

from bridge.guards import (comment_already_posted, find_similar_issue, issue_already_created,
                           link_already_present, state_already_set)
from bridge.store import Link, Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "b.db"))


def test_issue_already_created(store):
    assert issue_already_created(store, "item-1") is None
    store.save(Link("item-1", "proj", "acme/a", 7, "https://github.com/acme/a/issues/7"))
    assert issue_already_created(store, "item-1").issue_number == 7


def test_link_already_present():
    url = "https://github.com/acme/a/issues/7"
    assert link_already_present(["https://GitHub.com/acme/a/issues/7/"], url)
    assert not link_already_present(["https://github.com/acme/a/issues/70"], url)
    assert not link_already_present([], url)
    assert not link_already_present(None, url)


def test_state_already_set():
    assert state_already_set("s-done", "s-done")
    assert not state_already_set("s-todo", "s-done")
    assert not state_already_set(None, None)


def test_comment_already_posted_reads_without_recording(store):
    assert not comment_already_posted(store, "item-1", "push:abc")
    assert not comment_already_posted(store, "item-1", "push:abc")   # still not recorded
    assert store.post_is_new("item-1", "push:abc")
    assert comment_already_posted(store, "item-1", "push:abc")
    assert not comment_already_posted(store, "item-2", "push:abc")


BODY = "Some text\n\n---\nSynced from Plane work item [ALL-5](https://plane.example.com/x). Edit it there."


def test_find_similar_issue_by_item_ref():
    issues = [
        {"number": 1, "title": "Other", "body": BODY.replace("ALL-5", "ALL-50")},
        {"number": 2, "title": "Renamed on GitHub", "body": BODY},
    ]
    assert find_similar_issue(issues, "Original title", "ALL-5")["number"] == 2


def test_find_similar_issue_by_bridge_title():
    issues = [
        {"number": 3, "title": "Fix it", "body": "written by a person"},
        {"number": 4, "title": "Fix it", "body": "Synced from Plane work item [X-1](u)"},
    ]
    assert find_similar_issue(issues, " Fix it ", "ALL-9")["number"] == 4


def test_find_similar_issue_none_and_ignores_prs():
    issues = [
        {"number": 5, "title": "Fix it", "body": "written by a person"},
        {"number": 6, "title": "Fix it", "body": BODY, "pull_request": {"url": "u"}},
    ]
    assert find_similar_issue(issues, "Fix it", "ALL-5") is None
    assert find_similar_issue([], "Fix it", "ALL-5") is None
    assert find_similar_issue([{"number": 7, "title": None, "body": None}], "", "") is None
