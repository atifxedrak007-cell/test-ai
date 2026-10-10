# CodePilot — Multi-AI Code Review & Workflow

A small FastAPI app for Termux (Android, no root) and Linux. It accepts source
files or a ZIP, plans a constrained workflow, streams subprocess logs to a
mobile-first web UI, and runs separate backend and syntax reviews.

## What it does

- Upload multiple files or a ZIP (25 MB uploaded / 100 MB extracted limit).
- Reject unsafe ZIP paths, encrypted ZIP entries, and symbolic links.
- Ask the assigned Workflow Runner model for a JSON plan. The model chooses
  only among approved step types and project files; it cannot send shell text.
- Run supported Python or JavaScript steps with timeouts and live logs.
- Run backend and syntax reviewers after the workflow; keep project/run/review
  history in SQLite.
- Manage OpenAI-compatible, Anthropic, and Gemini model entries and role
  assignments. Keys are read from environment variables; they are not saved in
  `models.json` or shown in the UI.
- Save tagged AI code blocks over 150 lines or 8 KB as individual files under
  `outputs/`. Each has its own preview, copy, and download controls.
- Start a Cloudflare Quick Tunnel automatically when `cloudflared` is found.

## Important security boundary

This app is intended for your own device and code you trust. It is **not an OS
sandbox**: a program or test that you run can access files and resources
available to your Android/Linux user. The runner does not accept arbitrary
commands from the AI, and child processes do not inherit the app's model-key
environment variables, but these controls do not isolate malicious code.
Review uploads before running them.

A Quick Tunnel makes the app reachable from the public internet. Set a strong
`APP_PASSWORD` before using it, keep API keys in `.env`, and do not share the
tunnel URL. Without a password, anyone with the link may use your configured
model APIs. The built-in rate limit is process-local and is only a basic
throttle, not a production abuse-prevention system.

An API key was included in the notes supplied with this app request. Revoke
that key at its provider and use a newly issued key in your local `.env`; this
project does not contain or use the disclosed value.

## Termux setup

Run these commands in Termux:

```sh
pkg update
pkg install python nano cloudflared
mkdir -p ~/codepilot
cd ~/codepilot
# Copy the supplied source files into this directory.
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
nano .env
python app.py
```

In `.env`, replace the `APP_PASSWORD`, `APP_SECRET`, and model-key placeholders.
Use the **newly rotated** provider key for `UNOROUTER_API_KEY`. Keep `.env`
private; it is excluded by `.gitignore`. If the `cloudflared` package is not
available in your Termux repository, install Cloudflare's `cloudflared` binary
using its current Android/Termux instructions. The app prints
`pkg install cloudflared` when it cannot find the executable and continues
locally at `http://127.0.0.1:8000`.

## Linux setup

```sh
cd /path/to/codepilot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
nano .env
python app.py
```

Install `cloudflared` separately if you want a public Quick Tunnel. Without it,
the app stays available locally at `http://127.0.0.1:8000`.

## Model configuration

`models.json` contains only non-secret metadata:

```json
{
  "name": "Example",
  "provider": "openai-compatible",
  "base_url": "https://provider.example/v1",
  "model_id": "model-name",
  "api_key_env": "PROVIDER_API_KEY",
  "enabled": true
}
```

Add `PROVIDER_API_KEY=...` to `.env`, restart the app, then assign the model to
one or more agent roles in Settings. Never put key values in this file, the
browser UI, uploaded code, screenshots, or terminal commands that may be saved
in shell history. Provider errors are summarized without printing request
headers, URLs, or response bodies.

The initial `models.json` catalog is based on the model IDs and endpoint in the
supplied notes. Those entries were not independently verified and availability
can change. Three text models are enabled and assigned to separate roles;
disable or replace any entry that your provider no longer supports.

## Workflow limits

- Python: `requirements.txt` accepts simple package/version specifiers only;
  installs use binary wheels. Syntax check uses `compileall`; tests use
  `pytest` when a `tests/` directory exists; the run step looks for
  `main.py`, `app.py`, or `manage.py`.
- JavaScript: npm install uses `--ignore-scripts`; syntax check uses `node
  --check`; npm tests run when `package.json` exists; the run step looks for
  `index.js`, `server.js`, `app.js`, or `main.js`.
- Each process step has a 90-second limit. The Stop button terminates the
  workflow process group on POSIX systems.
- Other languages are detected, but automatic execution is currently
  unsupported. The app will not invent a shell command for them.
- History and uploaded files are stored locally in `history.sqlite3`,
  `workspace/`, and `outputs/`. Back them up or remove them directly on your
  device as needed.

## Troubleshooting

- **No AI responses:** verify the API-key variable in `.env`, confirm the
  corresponding model is enabled and assigned in Settings, and check that its
  HTTPS base URL and model ID are current.
- **Tunnel URL missing:** verify `cloudflared` is installed and can run from
  your shell. The local server still starts if it is unavailable.
- **Port 8000 is occupied:** stop the other process before starting this app;
  the Quick Tunnel command is configured for port 8000.
- **No password prompt:** the browser may still have a session cookie. Log out,
  or clear site cookies. If `APP_PASSWORD` is unset, password protection is
  intentionally off and the UI shows a warning.
