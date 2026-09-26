"""A small, deliberately narrow browser-to-GitHub patch bridge."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request, session


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
<label for="token">Bridge token</label><input id="token" name="token" type="password" autocomplete="off">
<button type="submit">Apply and push</button></form>
<p><small>Do not paste credentials into the patch or commit message.</small></p>
<p><a href="/log">Audit log</a></p></main></body></html>"""

TOKEN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>404 - The page that got away</title>
<style>
  :root{
    --ink:#14202a;
    --cream:#f6efdc;
    --lake:#3d7a8c;
    --deep:#25505f;
    --bobber:#d8492b;
    --mustard:#e8b93c;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    background:#1c2a30;
    min-height:100vh;display:flex;align-items:center;justify-content:center;
    padding:20px 14px;
    font-family:"Comic Sans MS","Chalkboard SE","Comic Neue","Segoe Print",cursive,sans-serif;
    color:var(--ink);
  }
  .board{
    width:100%;max-width:610px;
    background:var(--cream);
    border:4px solid var(--ink);
    box-shadow:9px 9px 0 rgba(0,0,0,.5);
    position:relative;overflow:hidden;
    padding:20px 24px 18px;
  }
  .dots{
    position:absolute;inset:0;pointer-events:none;
    background-image:radial-gradient(circle, rgba(20,32,42,.10) 1px, transparent 1.3px);
    background-size:9px 9px;
  }
  .inner{position:relative;z-index:2}
  .shop{
    text-align:center;
    font-size:12px;letter-spacing:5px;text-transform:uppercase;
    color:var(--deep);
  }
  h1{
    text-align:center;
    font-family:"Arial Black","Arial Narrow Bold",Impact,sans-serif;
    font-size:clamp(26px,6.4vw,40px);
    text-transform:uppercase;letter-spacing:2px;
    color:var(--cream);
    -webkit-text-stroke:1.8px var(--ink);
    text-shadow:3px 3px 0 var(--lake), 5px 5px 0 var(--ink);
    margin-top:6px;
  }
  .hero{
    position:relative;
    margin:12px auto 0;
    border:4px solid var(--ink);
    background:var(--lake);
    box-shadow:6px 6px 0 rgba(20,32,42,.8);
    overflow:hidden;
  }
  .hero svg{display:block;width:100%;height:auto}
  .hero .tag{
    position:absolute;top:10px;left:10px;
    background:var(--mustard);border:3px solid var(--ink);
    padding:3px 10px;font-size:13px;text-transform:uppercase;letter-spacing:1px;
    transform:rotate(-2deg);
  }
  .hero .bubble{
    position:absolute;top:12px;right:12px;max-width:200px;
    background:#fff;border:3px solid var(--ink);border-radius:14px;
    padding:7px 11px;font-size:14px;line-height:1.35;
  }
  .hero .bubble:after{
    content:"";position:absolute;left:22px;bottom:-10px;
    width:13px;height:13px;background:#fff;
    border-right:3px solid var(--ink);border-bottom:3px solid var(--ink);
    transform:skewX(30deg) rotate(45deg);
  }
  .tales{
    margin:14px auto 0;max-width:480px;
    font-size:15px;line-height:1.75;
  }
  .tales .kicker{
    display:inline-block;background:var(--deep);color:var(--cream);
    border:2.5px solid var(--ink);
    font-size:12px;letter-spacing:3px;text-transform:uppercase;
    padding:2px 10px;margin-bottom:8px;transform:rotate(-1deg);
  }
  .tales b{text-transform:uppercase;letter-spacing:1px}
  .tales .measure{color:var(--bobber);font-weight:700}
  .tackle{
    margin:16px auto 0;max-width:460px;
    border:3px solid var(--ink);
    background:var(--mustard);
    box-shadow:4px 4px 0 rgba(20,32,42,.8);
    padding:10px 14px 12px;
  }
  .tackle .head{
    text-align:center;font-size:11px;letter-spacing:4px;text-transform:uppercase;
    opacity:.75;margin-bottom:8px;
  }
  .access{display:flex;gap:9px;justify-content:center;align-items:center}
  .access input{
    font-family:inherit;font-size:14px;
    padding:6px 10px;width:170px;
    border:2.5px solid var(--ink);background:#fff;outline:none;
  }
  .access input:focus{box-shadow:3px 3px 0 var(--lake)}
  .access button{
    font-family:"Arial Black",Impact,sans-serif;font-size:14px;letter-spacing:2px;
    padding:7px 16px;border:2.5px solid var(--ink);
    background:var(--bobber);color:#fff;cursor:pointer;
    box-shadow:3px 3px 0 var(--ink);
  }
  .access button:active{transform:translate(3px,3px);box-shadow:none}
  .fine{
    text-align:center;margin-top:12px;font-size:11px;letter-spacing:2px;
    text-transform:uppercase;opacity:.65;
  }
  @media (max-width:560px){
    .hero{padding:10px 10px 0}
    .hero .tag{position:static;display:inline-block;margin:0 0 8px}
    .hero .bubble{position:static;max-width:none;margin:0 0 10px;border-radius:12px}
    .hero .bubble:after{display:none}
  }

  .visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
  [role=alert]{color:#b3402a;font-weight:700;font-size:14px;margin-top:8px;text-align:center}
</style>
</head>
<body>
<div class="board">
  <div class="dots"></div>
  <div class="inner">
    <div class="shop">Rusty Hook Bait &amp; Tackle &middot; Est. whenever</div>
    <h1>404: The Page That Got Away</h1>
    <div class="hero">
      <span class="tag">Big-fish story No. 404</span>
      <div class="bubble">It was THIS big. Swam off with your whole page.</div>
      <svg viewBox="0 0 600 260" xmlns="http://www.w3.org/2000/svg" aria-label="fisherman, bent hook, and huge fish">
        <!-- water bands -->
        <rect x="0" y="150" width="600" height="110" fill="#25505f"/>
        <path d="M0 150 q40 -12 80 0 t80 0 t80 0 t80 0 t80 0 t80 0 t80 0 t80 0" fill="none" stroke="#f6efdc" stroke-width="4"/>
        <!-- halftone band on water -->
        <g fill="rgba(20,32,42,.25)">
          <circle cx="30" cy="180" r="2.4"/><circle cx="70" cy="196" r="2.4"/><circle cx="110" cy="178" r="2.4"/><circle cx="150" cy="200" r="2.4"/><circle cx="190" cy="184" r="2.4"/><circle cx="230" cy="198" r="2.4"/><circle cx="520" cy="182" r="2.4"/><circle cx="560" cy="198" r="2.4"/><circle cx="470" cy="204" r="2.4"/>
        </g>
        <!-- boat -->
        <path d="M60 150 L190 150 L172 190 L82 190 Z" fill="#f6efdc" stroke="#14202a" stroke-width="4"/>
        <!-- fisherman -->
        <circle cx="120" cy="98" r="13" fill="#f6efdc" stroke="#14202a" stroke-width="3.5"/>
        <path d="M104 92 q16 -14 34 -2 l0 -6 q-18 -12 -34 4z" fill="#14202a"/>
        <path d="M120 111 L120 140" stroke="#14202a" stroke-width="5" stroke-linecap="round"/>
        <path d="M120 118 L96 132" stroke="#14202a" stroke-width="4" stroke-linecap="round"/>
        <path d="M120 118 L150 108" stroke="#14202a" stroke-width="4" stroke-linecap="round"/>
        <!-- rod, bent hard -->
        <path d="M150 108 q60 -34 118 6" fill="none" stroke="#14202a" stroke-width="3.6" stroke-linecap="round"/>
        <!-- line into water -->
        <path d="M268 114 q10 22 2 44" fill="none" stroke="#f6efdc" stroke-width="2.4" stroke-dasharray="6 5"/>
        <!-- bent hook -->
        <g transform="translate(258 186)">
          <path d="M6 -20 q-16 6 -12 22 q3 12 16 10" fill="none" stroke="#d8492b" stroke-width="5" stroke-linecap="round"/>
          <path d="M6 -20 l10 -8" stroke="#d8492b" stroke-width="5" stroke-linecap="round"/>
        </g>
        <!-- huge fish silhouette -->
        <g>
          <path d="M330 214 q70 -52 168 -22 q26 8 44 24 q-18 16 -44 24 q-98 30 -168 -26z" fill="#14202a"/>
          <path d="M330 214 l-42 -22 q6 22 0 44z" fill="#14202a"/>
          <circle cx="508" cy="206" r="5" fill="#f6efdc"/>
          <path d="M400 196 q30 18 0 36" fill="none" stroke="#25505f" stroke-width="4"/>
          <!-- splash lines near hook -->
          <g stroke="#f6efdc" stroke-width="3" stroke-linecap="round">
            <path d="M292 178 l10 -14"/>
            <path d="M312 186 l16 -10"/>
          </g>
        </g>
        <!-- bobber -->
        <circle cx="210" cy="146" r="8" fill="#d8492b" stroke="#14202a" stroke-width="3"/>
        <path d="M202 146 a8 8 0 0 1 16 0z" fill="#f6efdc"/>
        <!-- sun -->
        <circle cx="316" cy="46" r="22" fill="#e8b93c" stroke="#14202a" stroke-width="3.5"/>
        <g stroke="#14202a" stroke-width="3" stroke-linecap="round">
          <path d="M316 12 v-9"/><path d="M345 28 l7 -7"/><path d="M349 55 h9"/><path d="M287 28 l-7 -7"/>
        </g>
        <!-- birds -->
        <g stroke="#14202a" stroke-width="2.6" fill="none" stroke-linecap="round">
          <path d="M70 40 q8 -8 16 0 q8 -8 16 0"/>
          <path d="M130 26 q7 -7 14 0 q7 -7 14 0"/>
        </g>
      </svg>
    </div>
    <div class="tales">
      <span class="kicker">The official report</span>
      <div><b>Species:</b> <i>pagus notfoundus</i> &mdash; rare, slippery, possibly mythical.</div>
      <div><b>Size:</b> <span class="measure">THIS big</span> (witnesses disagree; the tale grows hourly).</div>
      <div><b>Bait used:</b> one (1) broken link. It snapped the line at the homepage.</div>
      <div><b>Last seen:</b> heading for deeper water with your page in its mouth.</div>
    </div>
    <div class="tackle">
      <div class="head">Bait counter &middot; regulars only</div>
      <form class="access" method="post" autocomplete="off">
        <label for="access_code" class="visually-hidden">Access code</label>
        <input type="password" id="access_code" name="access_code" placeholder="Access code" required autocomplete="off">
        <button type="submit">GO</button>
      </form>
      {% if error %}<p role="alert">{{ error }}</p>{% endif %}
    </div>
    <div class="fine">no license required &middot; catch &amp; release &middot; mostly release</div>
  </div>
</div>
</body>
</html>"""

LOG_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Push log</title>
<style>body{font:16px system-ui,sans-serif;background:#101821;color:#edf3f8;max-width:760px;margin:2rem auto;padding:1rem}a{color:#9ad4ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><h1>Push log</h1><a href="/">Back</a><pre>{{ entries }}</pre></body></html>"""

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
    # Rotating the bridge token also invalidates browser sessions.
    app.secret_key = hashlib.sha256(("git-bridge-session:" + secret).encode()).digest()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_SAMESITE="Lax")

    def authorized(value: str | None) -> bool:
        return isinstance(value, str) and hmac.compare_digest(value, secret)

    def browser_authorized() -> bool:
        return session.get("authenticated") is True

    def caller_label() -> str:
        # Identify the caller without ever logging a secret: a short hash of the
        # presented key distinguishes keys, and sessions are labeled as such.
        header = request.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else None
        if presented is None:
            presented = request.form.get("access_code") or request.form.get("token") or None
        if presented is not None:
            return "key sha256:" + hashlib.sha256(presented.encode()).hexdigest()[:10]
        if session.get("authenticated"):
            return "browser session"
        return "no credentials"

    @app.after_request
    def log_request(response):
        # One structured line per request on stdout, so it lands in the
        # container logs (Dockhand shows these). Health checks are skipped;
        # they would drown out real use.
        if request.path == "/api/v1/health":
            return response
        record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "action": f"{request.method} {request.path}",
                  "caller": caller_label(),
                  "result": "success" if response.status_code < 400 else "failed",
                  "status": response.status_code}
        repo = request.form.get("repo") if request.form else None
        if repo is None and request.is_json:
            body = request.get_json(silent=True)
            repo = body.get("repo") if isinstance(body, dict) else None
        if isinstance(repo, str):
            record["repo"] = repo
        if response.status_code >= 400:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                record["error"] = body["error"][:200]
        print("git-bridge request: " + json.dumps(record, ensure_ascii=False), flush=True)
        return response

    def token_prompt(status: int = 200):
        return render_template_string(TOKEN_PAGE, error="Invalid access code" if status == 403 else None), status

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
        if not browser_authorized():
            return token_prompt()
        return render_template_string(PAGE, repos=bridge.repos, outcome=None, selected_repo=None, patch=None, message=None)

    @app.post("/")
    def form_push():
        submitted = request.form.get("access_code", request.form.get("token"))
        token_valid = authorized(submitted)
        if (submitted is not None and not token_valid) or not (token_valid or browser_authorized()):
            return token_prompt(403)
        if token_valid:
            session["authenticated"] = True
        if not any(field in request.form for field in ("repo", "patch", "message")):
            return form()
        patch = request.form.get("patch")
        if isinstance(patch, str):
            # Browsers submit textarea newlines as CRLF. Git applies that fine,
            # but diff --check reports the embedded CR as trailing whitespace.
            patch = patch.replace("\r\n", "\n").replace("\r", "\n")
        values = {"selected_repo": request.form.get("repo"),
                  "patch": patch, "message": request.form.get("message")}
        try:
            result = bridge.push(request.form.get("repo"), patch, request.form.get("message"))
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
        if request.method == "POST":
            submitted = request.form.get("access_code", request.form.get("token"))
            token_valid = authorized(submitted)
            if (submitted is not None and not token_valid) or not (token_valid or browser_authorized()):
                return token_prompt(403)
            if token_valid:
                session["authenticated"] = True
        elif not browser_authorized():
            return token_prompt()
        path = bridge.data_dir / "audit.jsonl"
        entries = path.read_text(encoding="utf-8") if path.exists() else "No pushes yet."
        return render_template_string(LOG_PAGE, entries=entries)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8080, debug=False)