# Git Bridge (beta)

Git Bridge accepts a unified diff through a plain HTML form, applies it to a permitted GitHub repository, commits, and pushes to `main`. It is for browser-capable agents that can submit forms but cannot run Git or make API calls. A JSON API is included for later programmatic use. It is **not** a general Git hosting or code review service.

## Security and limits

- Put it behind HTTPS and your existing access gateway (for example, authentik forward-auth on a reverse proxy). The app works without that gateway but still requires `BRIDGE_TOKEN` to unlock the browser form and for API pushes. A signed browser session keeps the form available after unlock; the form also accepts the token on each push. Use a long random token and fill the password field from your password manager. The API uses `Authorization: Bearer <BRIDGE_TOKEN>`.
- `REPOS` is an exact allowlist. The owner is set in `GITHUB_OWNER`; Git URLs can only target `https://github.com/<owner>/<allowed-repo>.git`. Use a fine-grained GitHub PAT with **Contents: read and write** only for those repositories. Never put the PAT into the form. It is passed to Git through a askpass helper process, not a URL or command argument. Treat Docker environment access as privileged.
- Git runs with fixed argument lists and no shell. The app requires text unified diffs and runs `git apply --check`, `git diff --check`, a commit with the configured identity, and a final remote HEAD comparison before pushing. It uses a lock per repository. A conflicting remote change or invalid patch fails visibly; no force push.
- Push attempts append JSON lines (UTC timestamp, repo, commit, message, status, and push error if one occurred) to `/data/audit.jsonl`; authorized viewers can see them at `/log`. Every request also writes one structured line to stdout (timestamp, endpoint, caller, repo when present, and result), so container logs show who used the bridge and when. Callers appear as a short hash of the key they presented or as `browser session`; secrets are never logged. Health checks are skipped. The form and log responses disable browser caching. The health endpoint is public and contains no secrets. Keep the log volume private and back it up if you depend on it. The commit message and diff may include sensitive material, so review what you submit.
- A broken network may return a push error after the remote accepted it. Check GitHub before retrying. The bridge does not automatically roll back a partial local apply; the next request resets the cached checkout to the remote main branch. It does not run tests or review patches; pushing code is a privileged action. Restrict who gets the bridge token and who can reach the app.

## Docker Compose

Clone this repository and run `docker compose up -d --build` in its directory. The Dockerfile builds a small Python image with Git; no host-specific mounts or devices are needed. Docker Compose uses a named volume for cached checkouts and the audit log. The published port defaults to `42873` (change `BRIDGE_PORT` if needed). Point an HTTPS reverse proxy to that port; protect it with your access gateway. **Do not expose this port directly to the public internet.** For a proxy on the same Docker host, bind the published port to loopback instead by changing the port mapping to `127.0.0.1:${BRIDGE_PORT:-42873}:8080`.

Set these variables in your Docker manager or shell before importing/running the stack:

| Variable | Value |
| --- | --- |
| `BRIDGE_TOKEN` | Long, random shared secret for form and API |
| `GITHUB_TOKEN` | Fine-grained PAT for the allowed repos, Contents read/write |
| `GITHUB_OWNER` | GitHub owner login, for example `example-owner` |
| `REPOS` | Comma-separated exact names, for example `project-one,project-two` |
| `GIT_AUTHOR_NAME` | Commit author name |
| `GIT_AUTHOR_EMAIL` | Commit author email |
| `BRIDGE_PORT` | Optional host port, default `42873` |

The same block is in `compose.yaml`, ready to paste into a stack editor:

```yaml
services:
  git-bridge:
    build: .
    ports:
      - "${BRIDGE_PORT:-42873}:8080"
    environment:
      BRIDGE_TOKEN: ${BRIDGE_TOKEN:?set a long random bridge token}
      GITHUB_TOKEN: ${GITHUB_TOKEN:?set a fine-grained GitHub PAT}
      GITHUB_OWNER: ${GITHUB_OWNER:?set GitHub account name}
      REPOS: ${REPOS:?set comma-separated repository names}
      GIT_AUTHOR_NAME: ${GIT_AUTHOR_NAME:?set commit author name}
      GIT_AUTHOR_EMAIL: ${GIT_AUTHOR_EMAIL:?set commit author email}
      DATA_DIR: /data
    volumes:
      - bridge_data:/data
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
volumes:
  bridge_data:
```

No `.env` file is needed if your Docker manager supplies these variables in its environment settings; a local `.env` is also supported by Docker Compose. Do not commit `.env`. For Dockhand running under CasaOS, import this folder as a buildable stack (the Dockerfile and app must be available as build context), put the variables in the stack's environment settings, and route your HTTPS reverse proxy to the host's chosen port. If Dockhand only accepts pasted YAML and cannot provide the local build context, build the image from this repository first and replace `build: .` with the published image reference. Plain Docker Compose remains the default setup; CasaOS and Dockhand are optional managers.

### Optional reverse proxy and authentik

Configure your reverse proxy to pass the app's HTTP traffic to the container's published port. Enable authentik forward-auth on the proxy route, requiring your desired users or group. Keep the app's own bridge token enabled as a second check. The exact forward-auth setup depends on your proxy; don't assume a particular NAS or Docker manager. Use TLS, block direct external access to the container port, and set a request-size limit near 1.2 MB if supported. For a proxy on a different machine, restrict the published port by firewall to that proxy.

## Use

Open `/`, fill the bridge token prompt, then select the exact repository, paste a text unified diff, enter a one-line commit message, and submit. The push form can also take the token directly. The result shows a GitHub commit link or the Git error (including apply check, remote HEAD conflict, or push failure). `/log` requires the bridge token to display push attempts. `GET /api/v1/health` returns `{"status":"ok"}`. API clients can `POST /api/v1/push` with JSON fields `repo`, `patch`, and `message`, plus the bearer token. A successful request returns JSON with the repository, commit SHA, and URL. Don't reuse a patch after a success: it will no longer apply cleanly.

### Paste-ready agent connection prompt

> Use the Git Bridge web form at `<HTTPS_BRIDGE_URL>` to propose code changes to an allowed repository. Fill the bridge token prompt from the approved secret manager, then select `<REPO_NAME>`, paste a unified text diff, write a clear one-line commit message, and submit the form. Do not make direct Git or API calls. Read the result page and report the commit link or exact error. Do not paste tokens into the patch or commit message. Ask for review before submitting if your operating rules require it.

## Test locally

Use Python 3.12+ with Git installed. Install the app and test dependencies, then run tests before building:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

The tests use temporary directories and a mocked Git layer; they do not reach GitHub or need real tokens. For manual browser testing, set the required environment variables to **test-only** values and run `python app.py`. A true end-to-end push needs a disposable repository and separately scoped PAT; never test against a production repo first.
