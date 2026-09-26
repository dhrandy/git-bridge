from pathlib import Path
from unittest.mock import Mock

import pytest

from app import BridgeError, GitBridge, create_app


@pytest.fixture
def client(tmp_path):
    bridge = GitBridge("example-owner", ("demo",), "fake-github-token", tmp_path,
                       "Example Author", "author@example.invalid")
    app = create_app(bridge=bridge, bridge_token="test-bridge-token")
    app.testing = True
    return app.test_client(), bridge


def valid_payload():
    return {"repo": "demo", "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "message": "Fix greeting"}


def test_form_render(client):
    browser, _ = client
    for path in ("/", "/log"):
        response = browser.get(path)
        assert response.status_code == 200
        assert b'name="access_code"' in response.data
        assert b'width=device-width' in response.data
        assert b"demo" not in response.data
        assert b"Git Bridge" not in response.data
        assert b"token" not in response.data.lower()
        assert b"Access code" in response.data
        assert b"The Page That Got Away" in response.data
        assert b"404" in response.data
        assert b"WARNING" not in response.data
        assert response.data.index(b"404") < response.data.index(b"Access code")
        assert b'name="patch"' not in response.data
        assert b"No pushes yet" not in response.data


def test_token_prompt_creates_session_and_pushes_without_token(client):
    browser, bridge = client
    response = browser.post("/", data={"access_code": "test-bridge-token"})
    assert response.status_code == 200
    assert b'<option value="demo"' in response.data
    assert "Secure" in response.headers["Set-Cookie"]
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert b'<option value="demo"' in browser.get("/").data
    assert b"No pushes yet" in browser.get("/log").data
    bridge.push = Mock(return_value={"repo": "demo", "commit": "b" * 40,
                                     "url": "https://github.com/example-owner/demo/commit/" + "b" * 40})
    response = browser.post("/", data=valid_payload())
    assert response.status_code == 200
    assert b"Push succeeded" in response.data
    bridge.push.assert_called_once()


def test_wrong_token_never_renders_details(client):
    browser, bridge = client
    bridge.push = Mock()
    for path, data in (("/", {"access_code": "wrong"}),
                       ("/", {**valid_payload(), "token": "wrong"}),
                       ("/log", {"access_code": "wrong"})):
        response = browser.post(path, data=data)
        assert response.status_code == 403
        assert b"Invalid access code" in response.data
        assert b"demo" not in response.data
        assert b"Git Bridge" not in response.data
        assert b"token" not in response.data.lower()
        assert b"Access code" in response.data
        assert b"The Page That Got Away" in response.data
        assert b"404" in response.data
        assert b"WARNING" not in response.data
        assert response.data.index(b"404") < response.data.index(b"Access code")
        assert b'name="patch"' not in response.data
    assert b"demo" not in browser.get("/").data
    bridge.push.assert_not_called()


def test_token_required_for_form_and_api(client):
    browser, bridge = client
    bridge.push = Mock()
    assert browser.post("/", data=valid_payload()).status_code == 403
    assert browser.post("/api/v1/push", json=valid_payload()).status_code == 401
    assert browser.post("/log", data={"token": "wrong"}).status_code == 403
    bridge.push.assert_not_called()


def test_whitelist_rejection(client):
    browser, _ = client
    payload = valid_payload()
    payload["repo"] = "private-other-repo"
    response = browser.post("/api/v1/push", json=payload,
                            headers={"Authorization": "Bearer test-bridge-token"})
    assert response.status_code == 400
    assert "not allowed" in response.json["error"]


@pytest.mark.parametrize("patch", ["hello", "--- a/x\n+++ b/x\n", "GIT binary patch\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"])
def test_bad_patch_rejection(client, patch):
    browser, _ = client
    payload = valid_payload()
    payload["patch"] = patch
    response = browser.post("/api/v1/push", json=payload,
                            headers={"Authorization": "Bearer test-bridge-token"})
    assert response.status_code == 400


def test_happy_path_mocked_git(client):
    browser, bridge = client
    sha = "a" * 40
    commit = "b" * 40
    calls = []

    def run(args, cwd=None, input_text=None):
        calls.append((args, cwd, input_text))
        if args[1] == "ls-remote":
            return f"{sha}\trefs/heads/main"
        if args[1:3] == ["remote", "get-url"]:
            return "https://github.com/example-owner/demo.git"
        if args[1] == "rev-parse":
            return sha if args[-1] == "refs/remotes/origin/main" else commit
        return ""

    bridge._run = run
    payload = valid_payload()
    response = browser.post("/api/v1/push", json=payload,
                            headers={"Authorization": "Bearer test-bridge-token"})
    assert response.status_code == 201
    assert response.json["commit"] == commit
    assert response.json["url"] == f"https://github.com/example-owner/demo/commit/{commit}"
    assert [args[:3] for args, _, _ in calls if args[:2] == ["git", "apply"]] == [
        ["git", "apply", "--check"], ["git", "apply", "-"]]
    assert calls[-1][0] == ["git", "push", "origin", "main"]
    assert commit in (bridge.data_dir / "audit.jsonl").read_text()
    log = browser.post("/log", data={"token": "test-bridge-token"})
    assert commit.encode() in log.data


def test_form_normalizes_crlf_patch(client):
    browser, bridge = client
    commit = "b" * 40
    bridge.push = Mock(return_value={"repo": "demo", "commit": commit,
                                     "url": f"https://github.com/example-owner/demo/commit/{commit}"})
    payload = valid_payload()
    payload["patch"] = payload["patch"].replace("\n", "\r\n")
    payload["token"] = "test-bridge-token"
    response = browser.post("/", data=payload)
    assert response.status_code == 200
    assert b"Push succeeded" in response.data
    # The bridge receives the patch with LF newlines, as if typed on any OS.
    bridge.push.assert_called_once_with("demo", valid_payload()["patch"], "Fix greeting")


def test_head_mismatch(client):
    browser, bridge = client
    heads = iter(["a" * 40, "c" * 40])

    def run(args, cwd=None, input_text=None):
        if args[1] == "ls-remote":
            return f"{next(heads)}\trefs/heads/main"
        if args[1] == "rev-parse":
            return "a" * 40 if args[-1] != "HEAD" else "b" * 40
        if args[1:3] == ["remote", "get-url"]:
            return "https://github.com/example-owner/demo.git"
        return ""

    bridge._run = run
    response = browser.post("/api/v1/push", json=valid_payload(),
                            headers={"Authorization": "Bearer test-bridge-token"})
    assert response.status_code == 409
    assert "HEAD mismatch" in response.json["error"]
