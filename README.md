# copilot-4-claude-code

A local proxy server that exposes **GitHub Copilot** as a standard **Anthropic API**, enabling tools like [Claude Code](https://claude.ai/code) to use it as a backend.

## How It Works

```
Claude Code  →  Anthropic API (localhost:8082)  →  GitHub Copilot API
```

1. The proxy runs a FastAPI server locally on port `8082`
2. Claude Code sends requests to the proxy using the standard Anthropic `/v1/messages` format
3. The proxy authenticates with GitHub Copilot and forwards requests appropriately:
   - **Claude models** → direct passthrough to Copilot's `/v1/messages` endpoint (no conversion needed)
   - **Other models** (GPT-4o, etc.) → converted from Anthropic format to OpenAI format and sent to `/chat/completions`

## Authentication Flow

1. GitHub OAuth Device Flow → `access_token`
2. `access_token` → Copilot API key via `https://api.github.com/copilot_internal/v2/token`
3. Copilot API key + required headers → GitHub Copilot API requests

Tokens are cached locally in `.github_copilot_token/` and auto-refreshed on expiry.

## Requirements

- Python 3.10+
- A GitHub account with an active **GitHub Copilot** subscription

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Start the proxy

```bash
python main.py
```

On first run, you will be prompted to authenticate via GitHub OAuth Device Flow:

```
==================================================
  Visit: https://github.com/login/device
  Auth code: >>>  XXXX-XXXX  <<<
==================================================
```

Once authenticated, the proxy starts on port `8082`.

### Configure Claude Code

**Option 1: Persistent environment variables**
```bash
export ANTHROPIC_BASE_URL=http://localhost:8082
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

**Option 2: Single launch**
```bash
ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_AUTH_TOKEN=dummy claude
```

## Dashboard

A web dashboard is available at **http://localhost:8082/ui** showing:

- Live request statistics (total, success, errors)
- Available models and their supported endpoints
- Recent request log with model mapping, status, and duration
- Claude Code configuration snippets

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `GET /v1/models` | List available models (Anthropic format) |
| `POST /v1/messages` | Main proxy endpoint (Anthropic format) |
| `GET /v1/models/refresh` | Force refresh model list |
| `GET /audit/sessions` | List audit session files |
| `GET /audit/current` | Current session audit log |
| `GET /ui` | Web dashboard |

## Model Name Mapping

Claude Code model names are automatically mapped to Copilot format:

| Claude Code | Copilot |
|---|---|
| `claude-opus-4-6` | `claude-opus-4.6` |
| `claude-opus-4-6-20250514` | `claude-opus-4.6` |
| `claude-haiku-4-5` | `claude-haiku-4.5` |
| `gpt-4o` | `gpt-4o` (unchanged) |

## Logging & Audit

- Application logs: `logs/app.log`
- Per-session audit logs: `logs/audit/session_YYYYMMDD_HHMMSS_<id>.json`

Each audit entry captures the original model, mapped model, endpoint, status code, duration, and a preview of the request/response.

## Project Structure

```
.
├── main.py              # Proxy server (FastAPI)
├── requirements.txt     # Python dependencies
├── logs/
│   ├── app.log          # Application log
│   └── audit/           # Per-session audit JSON files
└── .github_copilot_token/
    ├── access-token     # GitHub OAuth access token
    └── api-key.json     # Copilot API key (auto-refreshed)
```
