"""A small, deliberately narrow browser-to-GitHub patch bridge."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request


REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_PATCH_BYTES = 1_000_000
MAX_MESSAGE_LENGTH = 240

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Git Bridge</title><style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#101821;color:#edf3f8}
*{box-sizing:border-box}body{margin:0;padding:1rem}main{max-width:760px;margin:1.5rem auto}
h1{font-size:clamp(1.5rem,6vw,2.2rem);margin-bottom:.3rem}p{line-height:1.5;color:#b8c9d4}
label{display:block;margin:1.15rem 0 .4rem;font-weight:600}
input,select,textarea,button{font:inherit;width:100%;max-width:100%;border-radius:.5rem;padding:.7rem;border:1px solid #748b9c;background:#182633;color:#fff}
textarea{min-height:15rem;resize:vertical;font-family:ui-monospace,monospace;font-size:.85rem}
button{margin-top:1.2rem;background:#80c8ff;color:#071622;border:0;font-weight:700;cursor:pointer;min-height:44px}
a{color:#9ad4ff;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#182633;padding:1rem;border-radius:.5rem}
small{color:#b8c9d4}
</style></head><body><main>
<h1>Git Bridge</h1><p>Apply a unified diff and push a commit to an allowed repository.</p>
{% if outcome %}<section role="status"><h2>{{ outcome.title }}</h2><p>{{ outcome.detail }}</p>
{% if outcome.url %}<p><a href="{{ outcome.url }}" rel="noopener noreferrer">View commit</a></p>{% endif %}
{% if outcome.error %}<pre>{{ outcome.error }}</pre>{% endif %}</section>{% endif %}
<form action="/" method="post" autocomplete="off">
<label for="repo">Repository</label><select id="repo" name="repo" required>
{% for repo in repos %}<option value="{{ repo }}" {% if repo == selected_repo %}selected{% endif %}>{{ repo }}</option>{% endfor %}</select>
<label for="patch">Unified diff</label><textarea id="patch" name="patch" required spellcheck="false" autocapitalize="off">{{ patch or "" }}</textarea>
<label for="message">Commit message</label><input id="message" name="message" required maxlength="240" value="{{ message or "" }}">
<label for="token">Bridge token</label><input id="token" name="token" type="password" required autocomplete="off">
<button type="submit">Apply and push</button></form>
<p><small>The token is required for each push. Do not paste credentials into the patch or commit message.</small></p>
<p><a href="/log">Audit log</a></p></main></body></html>"""

LOG_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Push log</title>
<style>body{font:16px system-ui,sans-serif;background:#101821;color:#edf3f8;max-width:760px;margin:2rem auto;padding:1rem}input,button{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin:.4rem 0}a{color:#9ad4ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><h1>Push log</h1><a href="/">Back</a>
<form method="post" action="/log"><label for="token">Bridge token</label><input type="password" id="token" name="token" required autocomplete="off"><button>View log</button></form>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}{% if entries is not none %}<pre>{{ entries }}</pre>{% endif %}</body></html>"""


class BridgeError(Exception):
    """A safe-to-display failure from a controlled operation."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GitBridge:
    def __init__(self, owner: str, repos: tuple[str, ...], token: str, data_dir: Path,
                 author_name: str, author_email: str):
        self.owner = owner
        self.repos = repos
        self.github_token = token
        self.data_dir = data_dir
        self.author_name = author_name
        self.author_email = author_email
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Git runs this fixed program to obtain HTTPS credentials. The PAT never
        # appears in a Git URL, process argument, checked-in file, or audit log.
        askpass = self.data_dir / ".askpass"
        askpass.write_text(
            '#!/usr/bin/env python3\nimport os, sys\n'
            'print("x-access-token" if "Username" in sys.argv[1] '
            'else os.environ["GITHUB_TOKEN"])\n', encoding="utf-8")
        askpass.chmod(0o700)
        self.askpass = askpass

    def _environment(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_") and key not in ("GITHUB_TOKEN", "SSH_ASKPASS")}
        env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.hooksPath",
                   GIT_CONFIG_VALUE_0=os.devnull,
                   GIT_CONFIG_KEY_1="credential.helper", GIT_CONFIG_VALUE_1="",
                   GITHUB_TOKEN=self.github_token, GIT_ASKPASS=str(self.askpass),
                   GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME=self.author_name,
                   GIT_AUTHOR_EMAIL=self.author_email, GIT_COMMITTER_NAME=self.author_name,
                   GIT_COMMITTER_EMAIL=self.author_email)
        return env

    def _run(self, args: list[str], cwd: Path | None = None, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(args, cwd=cwd, input=input_text, text=True,
                                    capture_output=True, env=self._environment(),
                                    timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"Git operation failed: {type(exc).__name__}", 502) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "Git exited with an error").strip()
            detail = detail.replace(self.github_token, "[redacted]")
            raise BridgeError(f"{' '.join(args[:2])} failed:\n{detail[:4000]}", 409)
        return result.stdout.strip()

    def _remote_head(self, url: str) -> str:
        lines = self._run(["git", "ls-remote", url, "refs/heads/main"]).splitlines()
        if len(lines) != 1:
            raise BridgeError("Remote main branch not found", 409)
        head = lines[0].split()[0]
        if not SHA.fullmatch(head):
            raise BridgeError("Remote returned an invalid HEAD", 502)
        return head

    def _validate(self, repo: Any, patch: Any, message: Any) -> None:
        if not isinstance(repo, str) or repo not in self.repos:
            raise BridgeError("Repository is not allowed")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_LENGTH or "\n" in message or "\r" in message:
            raise BridgeError("Commit message must be one line, 1-240 characters")
        if not isinstance(patch, str) or not patch or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise BridgeError("Patch must be a nonempty unified diff under 1 MB")
        if "\x00" in patch or not re.search(r"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", patch):
            raise BridgeError("Patch must be a text unified diff with a hunk")
        if not re.search(r"(?m)^--- (?:a/|/dev/null)", patch) or not re.search(r"(?m)^\+\+\+ (?:b/|/dev/null)", patch):
            raise BridgeError("Patch must contain unified diff file headers")
        if re.search(r"(?m)^(?:GIT binary patch|Binary files |literal \d+)$", patch):
            raise BridgeError("Binary patches are not allowed")

    def push(self, repo: Any, patch: Any, message: Any) -> dict[str, str]:
        self._validate(repo, patch, message)
        url = f"https://github.com/{self.owner}/{repo}.git"
        checkout = self.data_dir / "repos" / repo
        checkout.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / f"{repo}.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = self._remote_head(url)
            if checkout.is_symlink():
                raise BridgeError("Cached checkout is a symlink", 500)
            if not checkout.exists():
                self._run(["git", "clone", "--no-checkout", "--branch", "main", "--", url, str(checkout)])
            else:
                if not (checkout / ".git").is_dir():
                    raise BridgeError("Cached checkout is not a Git repository", 500)
                existing_url = self._run(["git", "remote", "get-url", "origin"], checkout)
                push_url = self._run(["git", "remote", "get-url", "--push", "origin"], checkout)
                if existing_url != url or push_url != url:
                    raise BridgeError("Cached checkout remote does not match", 500)
            self._run(["git", "fetch", "--no-tags", "origin", "main"], checkout)
            fetched = self._run(["git", "rev-parse", "refs/remotes/origin/main"], checkout)
            if fetched != expected:
                raise BridgeError(f"HEAD mismatch: expected {expected}, fetched {fetched}", 409)
            self._run(["git", "reset", "--hard"], checkout)
            self._run(["git", "clean", "-fd"], checkout)
            self._run(["git", "checkout", "-B", "main", expected], checkout)
            self._run(["git", "reset", "--hard", expected], checkout)
            # stdin is not shell input. Git validates the patch's paths and hunks.
            self._run(["git", "apply", "--check", "-"], checkout, patch)
            self._run(["git", "apply", "-"], checkout, patch)
            self._run(["git", "diff", "--check"], checkout)
            self._run(["git", "add", "-A"], checkout)
            self._run(["git", "diff", "--cached", "--check"], checkout)
            self._run(["git", "commit", "-m", message], checkout)
            commit = self._run(["git", "rev-parse", "HEAD"], checkout)
            if not SHA.fullmatch(commit):
                raise BridgeError("Git returned an invalid commit hash", 500)
            current = self._remote_head(url)
            if current != expected:
                raise BridgeError(f"HEAD mismatch: expected {expected}, remote is {current}", 409)
            try:
                self._run(["git", "push", "origin", "main"], checkout)
            except BridgeError as exc:
                self._audit(repo, commit, message, status="failed", error=str(exc))
                raise
            self._audit(repo, commit, message, status="succeeded")
            return {"repo": repo, "commit": commit,
                    "url": f"https://github.com/{self.owner}/{repo}/commit/{commit}"}

    def _audit(self, repo: str, commit: str, message: str, status: str,
               error: str | None = None) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "repo": repo,
                  "commit": commit, "message": message, "status": status}
        if error:
            record["error"] = error
        # Append under the same per-repository lock; append is atomic per write.
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with (self.data_dir / "audit.jsonl").open("a", encoding="utf-8") as log:
            log.write(line)
            log.flush()
            os.fsync(log.fileno())


def configured_bridge() -> GitBridge:
    required = ("BRIDGE_TOKEN", "GITHUB_TOKEN", "GITHUB_OWNER", "REPOS",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    owner = os.environ["GITHUB_OWNER"]
    if not REPO_NAME.fullmatch(owner):
        raise RuntimeError("Invalid GITHUB_OWNER")
    repos = tuple(name.strip() for name in os.environ["REPOS"].split(","))
    if not repos or any(not REPO_NAME.fullmatch(name) or name in (".", "..") for name in repos):
        raise RuntimeError("REPOS must be a comma-separated list of repository names")
    return GitBridge(owner, repos, os.environ["GITHUB_TOKEN"],
                     Path(os.getenv("DATA_DIR", "/data")),
                     os.environ["GIT_AUTHOR_NAME"], os.environ["GIT_AUTHOR_EMAIL"])


def create_app(bridge: GitBridge | None = None, bridge_token: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1_200_000
    bridge = bridge if bridge is not None else configured_bridge()
    secret = bridge_token if bridge_token is not None else os.environ["BRIDGE_TOKEN"]
    if not secret:
        raise RuntimeError("BRIDGE_TOKEN must not be empty")

    def authorized(value: str | None) -> bool:
        return isinstance(value, str) and hmac.compare_digest(value, secret)

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
        return response

    @app.get("/")
    def form():
        return render_template_string(PAGE, repos=bridge.repos, outcome=None, selected_repo=None, patch=None, message=None)

    @app.post("/")
    def form_push():
        values = {"selected_repo": request.form.get("repo"),
                  "patch": request.form.get("patch"), "message": request.form.get("message")}
        if not authorized(request.form.get("token")):
            return render_template_string(PAGE, repos=bridge.repos, **values,
                                          outcome={"title": "Push failed", "detail": "Invalid bridge token"}), 403
        try:
            result = bridge.push(request.form.get("repo"), request.form.get("patch"), request.form.get("message"))
        except BridgeError as exc:
            return render_template_string(PAGE, repos=bridge.repos, **values,
                                          outcome={"title": "Push failed", "detail": "Git did not push the patch.", "error": str(exc)}), exc.status
        return render_template_string(PAGE, repos=bridge.repos, selected_repo=None, patch=None, message=None,
                                      outcome={"title": "Push succeeded", "detail": f"Commit {result['commit']}",
                                               "url": result["url"]})

    @app.post("/api/v1/push")
    def api_push():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not authorized(header[7:]):
            return jsonify(error="Invalid bridge token"), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected JSON object"), 400
        try:
            result = bridge.push(body.get("repo"), body.get("patch"), body.get("message"))
        except BridgeError as exc:
            return jsonify(error=str(exc)), exc.status
        return jsonify(result), 201

    @app.get("/api/v1/health")
    def health():
        return jsonify(status="ok")

    @app.route("/log", methods=["GET", "POST"])
    def audit_log():
        if request.method == "GET":
            return render_template_string(LOG_PAGE, entries=None, error=None)
        if not authorized(request.form.get("token")):
            return render_template_string(LOG_PAGE, entries=None, error="Invalid bridge token"), 403
        path = bridge.data_dir / "audit.jsonl"
        entries = path.read_text(encoding="utf-8") if path.exists() else "No pushes yet."
        return render_template_string(LOG_PAGE, entries=entries, error=None)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8080, debug=False)
