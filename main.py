"""
GitHub Copilot → Anthropic API Proxy

Exposes GitHub Copilot as a standard Anthropic API, enabling tools like Claude Code to use it.

Key findings:
  - Claude models support direct passthrough via /v1/messages (no format conversion needed)
  - Model name format: claude-opus-4-6 → claude-opus-4.6 (hyphen → dot)
  - API base is read from endpoints.api in api-key.json (may be an enterprise domain)

Authentication flow:
  1. GitHub OAuth Device Flow → access_token
  2. access_token → Copilot API key (https://api.github.com/copilot_internal/v2/token)
  3. Copilot API key + specific headers → request GitHub Copilot API
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ============================================================
# Constants
# ============================================================

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_KEY_URL = "https://api.github.com/copilot_internal/v2/token"
GITHUB_COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_VERSION = "0.26.7"

TOKEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github_copilot_token")
ACCESS_TOKEN_FILE = os.path.join(TOKEN_DIR, "access-token")
API_KEY_FILE = os.path.join(TOKEN_DIR, "api-key.json")

# ============================================================
# Session & Directory Initialization
# ============================================================

SESSION_ID = str(uuid4())
SESSION_START = datetime.now(timezone.utc)

LOGS_DIR = Path("logs")
AUDIT_DIR = LOGS_DIR / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Logging System
# ============================================================

LOG_FILE = LOGS_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("copilot-proxy")

# ============================================================
# Audit Log System (one JSON file per session)
# ============================================================

AUDIT_FILE = AUDIT_DIR / f"session_{SESSION_START.strftime('%Y%m%d_%H%M%S')}_{SESSION_ID[:8]}.json"

_audit_data: dict = {
    "session_id": SESSION_ID,
    "started_at": SESSION_START.isoformat(),
    "requests": [],
}


def _write_audit() -> None:
    with open(AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(_audit_data, f, indent=2, ensure_ascii=False)


def audit_log(
    req_id: str,
    request_body: dict,
    copilot_model: str,
    endpoint: str,
    response_body: dict | str | None,
    status_code: int,
    duration_ms: float,
    error: str | None = None,
) -> None:
    entry = {
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_model": request_body.get("model", ""),
        "copilot_model": copilot_model,
        "endpoint": endpoint,
        "stream": request_body.get("stream", False),
        "messages_count": len(request_body.get("messages", [])),
        "request_preview": {
            "model": request_body.get("model"),
            "system": (request_body.get("system") or "")[:200],
            "last_user_msg": next(
                (m["content"][:200] if isinstance(m["content"], str) else str(m["content"])[:200]
                 for m in reversed(request_body.get("messages", []))
                 if m.get("role") == "user"),
                "",
            ),
        },
        "response": {
            "status_code": status_code,
            "body": response_body if isinstance(response_body, dict) else str(response_body)[:500] if response_body else None,
        },
        "duration_ms": round(duration_ms, 1),
        "error": error,
    }
    _audit_data["requests"].append(entry)
    _write_audit()
    logger.info(
        "request id=%s model=%s→%s endpoint=%s status=%s duration=%.0fms%s",
        req_id[:8], entry["original_model"], copilot_model, endpoint,
        status_code, duration_ms, f" ERROR={error}" if error else "",
    )


# ============================================================
# GitHub Copilot Authentication
# ============================================================

def _ensure_token_dir() -> None:
    os.makedirs(TOKEN_DIR, exist_ok=True)


def _get_github_request_headers(access_token: str | None = None) -> dict:
    headers = {
        "accept": "application/json",
        "editor-version": "vscode/1.85.1",
        "editor-plugin-version": "copilot/1.155.0",
        "user-agent": "GithubCopilot/1.155.0",
        "accept-encoding": "gzip,deflate,br",
    }
    if access_token:
        headers["authorization"] = f"token {access_token}"
    return headers


def _device_flow_login() -> str:
    """Obtain access_token via GitHub OAuth Device Flow"""
    client = httpx.Client()
    resp = client.post(
        GITHUB_DEVICE_CODE_URL,
        headers=_get_github_request_headers(),
        json={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
    )
    resp.raise_for_status()
    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]

    print("\n" + "=" * 50, flush=True)
    print(f"  Visit: {verification_uri}", flush=True)
    print(f"  Auth code: >>>  {user_code}  <<<", flush=True)
    print("=" * 50, flush=True)
    print("Polling started, please complete authorization in your browser...\n", flush=True)
    logger.info("Device Flow started, waiting for user authorization code=%s", user_code)

    for attempt in range(36):
        time.sleep(5)
        resp = client.post(
            GITHUB_ACCESS_TOKEN_URL,
            headers=_get_github_request_headers(),
            json={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        result = resp.json()
        if "access_token" in result:
            logger.info("GitHub OAuth authentication successful")
            return result["access_token"]
        elif result.get("error") == "authorization_pending":
            remaining = (36 - attempt - 1) * 5
            if attempt % 6 == 0:
                print(f"  [{remaining}s remaining] Waiting for authorization... code: {user_code}  |  {verification_uri}", flush=True)
            else:
                print(f"  [{remaining}s remaining] Waiting for authorization...", flush=True)
        else:
            logger.warning("Device Flow unexpected response: %s", result)

    raise RuntimeError("Timed out waiting for user authorization (180 seconds)")


def get_access_token() -> str:
    _ensure_token_dir()
    try:
        with open(ACCESS_TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                return token
    except IOError:
        pass
    token = _device_flow_login()
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(token)
    return token


def get_api_key() -> str:
    """Get Copilot API key, auto-refresh on expiry"""
    _ensure_token_dir()
    try:
        with open(API_KEY_FILE) as f:
            info = json.load(f)
            if info.get("expires_at", 0) > datetime.now().timestamp():
                return info["token"]
    except (IOError, json.JSONDecodeError, KeyError):
        pass

    access_token = get_access_token()
    headers = _get_github_request_headers(access_token)
    client = httpx.Client()
    resp = client.get(GITHUB_API_KEY_URL, headers=headers)

    if resp.status_code == 401:
        logger.warning("access_token has expired, re-authenticating")
        try:
            os.remove(ACCESS_TOKEN_FILE)
        except OSError:
            pass
        access_token = get_access_token()
        headers = _get_github_request_headers(access_token)
        resp = client.get(GITHUB_API_KEY_URL, headers=headers)

    resp.raise_for_status()
    info = resp.json()
    with open(API_KEY_FILE, "w") as f:
        json.dump(info, f)
    logger.info("Copilot API key refreshed, expires_at=%s", info.get("expires_at"))
    return info["token"]


def get_api_base() -> str:
    try:
        with open(API_KEY_FILE) as f:
            info = json.load(f)
            return info.get("endpoints", {}).get("api", GITHUB_COPILOT_API_BASE)
    except (IOError, json.JSONDecodeError):
        return GITHUB_COPILOT_API_BASE


def get_copilot_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "copilot-integration-id": "vscode-chat",
        "editor-version": "vscode/1.95.0",
        "editor-plugin-version": f"copilot-chat/{COPILOT_VERSION}",
        "user-agent": f"GitHubCopilotChat/{COPILOT_VERSION}",
        "openai-intent": "conversation-panel",
        "x-github-api-version": "2025-04-01",
        "x-request-id": str(uuid4()),
        "x-vscode-user-agent-library-version": "electron-fetch",
        "X-Initiator": "user",
    }


# ============================================================
# Model List Cache
# ============================================================

_models_cache: list = []
_models_cache_time: float = 0.0


def get_models(force: bool = False) -> list:
    global _models_cache, _models_cache_time
    if not force and _models_cache and time.time() - _models_cache_time < 300:
        return _models_cache
    try:
        api_key = get_api_key()
        api_base = get_api_base()
        headers = get_copilot_headers(api_key)
        resp = httpx.get(f"{api_base}/models", headers=headers, timeout=10)
        if resp.status_code == 200:
            _models_cache = resp.json().get("data", [])
            _models_cache_time = time.time()
            logger.info("Model list refreshed, total %d models", len(_models_cache))
    except Exception as e:
        logger.error("Failed to fetch model list: %s", e)
    return _models_cache


def get_model_info(model_id: str) -> dict | None:
    for m in get_models():
        if m["id"] == model_id:
            return m
    return None


def map_model_name(model: str) -> str:
    """
    Convert Claude Code model name to GitHub Copilot model name format
    claude-opus-4-6          → claude-opus-4.6
    claude-opus-4-6-20250514 → claude-opus-4.6  (strip date suffix)
    claude-haiku-4-5         → claude-haiku-4.5
    gpt-4o                   → gpt-4o (unchanged)
    """
    original = model
    model = re.sub(r"-\d{8}$", "", model)         # strip YYYYMMDD date suffix
    model = re.sub(r"(\d)-(\d+)$", r"\1.\2", model)  # 4-6 → 4.6
    if model != original:
        logger.debug("Model name mapped: %s → %s", original, model)
    return model


# ============================================================
# Format Conversion (only for non-Claude models via /chat/completions)
# ============================================================

def anthropic_to_openai(body: dict, mapped_model: str) -> dict:
    """Anthropic /v1/messages format → OpenAI /chat/completions format"""
    messages = []

    if system := body.get("system"):
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = " ".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
        messages.append({"role": role, "content": content})

    result: dict = {"model": mapped_model, "messages": messages}
    for key in ("max_tokens", "stream", "temperature", "top_p", "stop"):
        if key in body:
            result[key] = body[key]
    # top_k is Anthropic-specific, do not pass to OpenAI
    return result


def openai_to_anthropic(openai_resp: dict) -> dict:
    """OpenAI /chat/completions response → Anthropic /v1/messages format"""
    choice = openai_resp["choices"][0]
    message = choice["message"]
    usage = openai_resp.get("usage", {})
    finish_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    return {
        "id": openai_resp.get("id", f"msg_{uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": openai_resp.get("model", "unknown"),
        "content": [{"type": "text", "text": message.get("content") or ""}],
        "stop_reason": finish_map.get(choice.get("finish_reason", "stop"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def stream_openai_to_anthropic(
    openai_stream: httpx.Response, msg_id: str, model: str
) -> AsyncIterator[str]:
    """OpenAI SSE → Anthropic SSE format conversion (for non-Claude models)"""
    yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}})}\n\n"
    yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
    yield 'event: ping\ndata: {"type": "ping"}\n\n'

    finish_reason = "end_turn"
    output_tokens = 0
    finish_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}

    async for line in openai_stream.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        choice = chunk.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        text = delta.get("content") or ""
        if text:
            output_tokens += 1
            yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n"
        if fr := choice.get("finish_reason"):
            finish_reason = finish_map.get(fr, "end_turn")

    yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
    yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':finish_reason,'stop_sequence':None},'usage':{'output_tokens':output_tokens}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(title="GitHub Copilot → Anthropic API Proxy", docs_url=None, redoc_url=None)


@app.get("/health")
def health():
    return {"status": "ok", "session_id": SESSION_ID}


@app.get("/v1/models")
async def list_models():
    """Return model list in Anthropic format"""
    models = get_models()
    return {
        "data": [
            {
                "id": m["id"],
                "display_name": m.get("name", m["id"]),
                "created_at": SESSION_START.isoformat(),
                "object": "model",
            }
            for m in models
        ]
    }


@app.post("/v1/messages")
async def messages(request: Request):
    """
    Anthropic /v1/messages compatible endpoint

    Routing strategy:
    - Claude models → direct passthrough to {api_base}/v1/messages (no format conversion)
    - Other models  → convert to OpenAI format, send to {api_base}/chat/completions
    """
    req_id = str(uuid4())
    t_start = time.monotonic()
    body = await request.json()

    original_model: str = body.get("model", "")
    copilot_model = map_model_name(original_model)

    try:
        api_key = get_api_key()
    except Exception as e:
        logger.error("Authentication failed req=%s: %s", req_id[:8], e)
        audit_log(req_id, body, copilot_model, "auth", None, 401, 0, str(e))
        raise HTTPException(status_code=401, detail=f"GitHub Copilot authentication failed: {e}")

    api_base = get_api_base()
    copilot_headers = get_copilot_headers(api_key)

    # Determine which endpoint to use
    model_info = get_model_info(copilot_model)
    supported = model_info.get("supported_endpoints", []) if model_info else []
    use_messages_api = "/v1/messages" in supported

    if use_messages_api:
        # ── Claude models: direct passthrough /v1/messages ──────────────────
        endpoint = "/v1/messages"
        # Update model in body to Copilot format
        forward_body = {**body, "model": copilot_model}
        # Remove fields sent by Anthropic/Claude Code that Copilot does not support
        forward_body.pop("betas", None)
        forward_body.pop("context_management", None)
    else:
        # ── Non-Claude models: convert to OpenAI format ─────────────────
        endpoint = "/chat/completions"
        forward_body = anthropic_to_openai(body, copilot_model)

    is_stream = forward_body.get("stream", False)
    logger.debug(
        "req=%s model=%s→%s endpoint=%s stream=%s",
        req_id[:8], original_model, copilot_model, endpoint, is_stream,
    )

    url = f"{api_base}{endpoint}"

    if is_stream:
        # ── Streaming response ────────────────────────────────────────────
        async def generate():
            nonlocal t_start
            error_msg = None
            collected_text = []
            status = 200
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", url, headers=copilot_headers, json=forward_body) as resp:
                        status = resp.status_code
                        if status != 200:
                            err = await resp.aread()
                            error_msg = err.decode()
                            logger.warning("Upstream %s returned %s: %s", endpoint, status, error_msg[:200])
                            yield f"event: error\ndata: {json.dumps({'type':'error','error':{'type':'api_error','message':error_msg}})}\n\n"
                        elif use_messages_api:
                            # Claude model: direct SSE passthrough
                            async for line in resp.aiter_lines():
                                if line:
                                    yield line + "\n"
                                    if line.startswith("data:"):
                                        try:
                                            d = json.loads(line[5:])
                                            if d.get("type") == "content_block_delta":
                                                collected_text.append(d.get("delta", {}).get("text", ""))
                                        except Exception:
                                            pass
                                else:
                                    yield "\n"
                        else:
                            # Non-Claude: OpenAI SSE → Anthropic SSE conversion
                            msg_id = f"msg_{uuid4().hex[:24]}"
                            async for chunk in stream_openai_to_anthropic(resp, msg_id, original_model):
                                yield chunk
                                if '"text_delta"' in chunk:
                                    try:
                                        d = json.loads(chunk.split("data: ", 1)[1])
                                        collected_text.append(d.get("delta", {}).get("text", ""))
                                    except Exception:
                                        pass
            except Exception as e:
                error_msg = str(e)
                logger.error("Streaming request error req=%s: %s", req_id[:8], e)

            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint,
                      {"streamed_text": "".join(collected_text)[:500]},
                      status, duration, error_msg)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    else:
        # ── Non-streaming response ───────────────────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=copilot_headers, json=forward_body)
        except Exception as e:
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint, None, 500, duration, str(e))
            raise HTTPException(status_code=500, detail=str(e))

        duration = (time.monotonic() - t_start) * 1000

        if resp.status_code != 200:
            logger.warning("Upstream %s returned %s: %s", endpoint, resp.status_code, resp.text[:300])
            audit_log(req_id, body, copilot_model, endpoint,
                      resp.text[:500], resp.status_code, duration,
                      f"upstream {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        resp_json = resp.json()
        if use_messages_api:
            # Return directly (already in Anthropic format)
            result = resp_json
        else:
            result = openai_to_anthropic(resp_json)

        audit_log(req_id, body, copilot_model, endpoint, result, 200, duration)
        return JSONResponse(content=result)


# ============================================================
# Admin Endpoints
# ============================================================

@app.get("/v1/models/refresh")
async def refresh_models():
    """Force refresh model list"""
    models = get_models(force=True)
    return {"count": len(models), "models": [m["id"] for m in models]}


@app.get("/audit/sessions")
def audit_sessions():
    """List all audit session files"""
    files = sorted(AUDIT_DIR.glob("session_*.json"), reverse=True)
    result = []
    for f in files[:20]:
        try:
            data = json.loads(f.read_text())
            result.append({
                "file": f.name,
                "session_id": data.get("session_id", ""),
                "started_at": data.get("started_at", ""),
                "request_count": len(data.get("requests", [])),
            })
        except Exception:
            pass
    return result


@app.get("/audit/current")
def audit_current():
    """Return current session audit log"""
    return _audit_data


# ============================================================
# Dashboard UI
# ============================================================

@app.get("/ui", response_class=HTMLResponse)
async def dashboard():
    models = get_models()
    api_base = get_api_base()
    uptime = datetime.now(timezone.utc) - SESSION_START
    h, m = int(uptime.total_seconds() // 3600), int((uptime.total_seconds() % 3600) // 60)
    uptime_str = f"{h}h {m}m" if h else f"{m}m {int(uptime.total_seconds() % 60)}s"

    req_count = len(_audit_data["requests"])
    ok_count = sum(1 for r in _audit_data["requests"] if r["response"]["status_code"] == 200)
    err_count = req_count - ok_count
    recent = _audit_data["requests"][-20:][::-1]

    claude_models = [m for m in models if m["id"].startswith("claude")]
    other_models  = [m for m in models if not m["id"].startswith("claude")]

    def ep_tag(ep):
        cls = "tag-green" if "/v1/messages" in ep else "tag-blue"
        return f'<span class="tag {cls}">{ep}</span>'

    def model_rows(mlist):
        rows = []
        for m in mlist:
            eps = "".join(ep_tag(e) for e in (m.get("supported_endpoints") or []))
            star = '<span class="star">★</span>' if m.get("model_picker_enabled") else ""
            rows.append(
                f'<tr><td class="mono">{m["id"]}</td>'
                f'<td class="muted">{m.get("name","")}</td>'
                f'<td>{eps or "<span class=muted>—</span>"}</td>'
                f'<td>{star}</td></tr>'
            )
        return "".join(rows)

    def req_rows_html():
        if not recent:
            return '<tr><td colspan="6" class="empty">No requests yet</td></tr>'
        rows = []
        for r in recent:
            ok = r["response"]["status_code"] == 200
            status_cls = "status-ok" if ok else "status-err"
            err = f'<span class="err-msg">{r["error"][:60]}</span>' if r.get("error") else ""
            msg = r["request_preview"].get("last_user_msg", "")[:80]
            rows.append(
                f'<tr>'
                f'<td class="muted mono">{r["timestamp"][11:19]}</td>'
                f'<td class="mono">{r["original_model"]}</td>'
                f'<td class="mono accent">{r["copilot_model"]}</td>'
                f'<td><span class="{status_cls}">{r["response"]["status_code"]}</span>{err}</td>'
                f'<td class="muted">{r["duration_ms"]:.0f}ms</td>'
                f'<td class="muted ellipsis">{msg}</td>'
                f'</tr>'
            )
        return "".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copilot Proxy</title>
<style>
:root[data-theme="dark"] {{
  --bg:       #0f1117;
  --surface:  #1a1d27;
  --border:   #2a2d3a;
  --text:     #e2e4ed;
  --muted:    #6b7280;
  --accent:   #60a5fa;
  --green:    #34d399;
  --red:      #f87171;
  --code-bg:  #12151f;
  --tag-g-bg: #064e3b; --tag-g-fg: #6ee7b7;
  --tag-b-bg: #1e3a5f; --tag-b-fg: #93c5fd;
  --hover:    #21242f;
  --btn-bg:   #1d4ed8; --btn-hover: #2563eb;
  --star:     #fbbf24;
}}
:root[data-theme="light"] {{
  --bg:       #f8f9fb;
  --surface:  #ffffff;
  --border:   #e5e7eb;
  --text:     #111827;
  --muted:    #6b7280;
  --accent:   #2563eb;
  --green:    #059669;
  --red:      #dc2626;
  --code-bg:  #f1f3f7;
  --tag-g-bg: #d1fae5; --tag-g-fg: #065f46;
  --tag-b-bg: #dbeafe; --tag-b-fg: #1e40af;
  --hover:    #f3f4f6;
  --btn-bg:   #2563eb; --btn-hover: #1d4ed8;
  --star:     #d97706;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 14px; line-height: 1.5;
  background: var(--bg); color: var(--text);
  transition: background .2s, color .2s;
}}
/* ── Header ── */
.header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 24px; border-bottom: 1px solid var(--border);
  background: var(--surface); position: sticky; top: 0; z-index: 10;
}}
.header-left {{ display: flex; align-items: center; gap: 10px; }}
.header-title {{ font-size: 15px; font-weight: 600; }}
.pulse {{ width: 8px; height: 8px; border-radius: 50%; background: var(--green);
          animation: pulse 2s ease-in-out infinite; flex-shrink: 0; }}
@keyframes pulse {{ 0%,100%{{opacity:1; transform:scale(1)}} 50%{{opacity:.5; transform:scale(.85)}} }}
.header-meta {{ font-size: 12px; color: var(--muted); }}
.header-right {{ display: flex; align-items: center; gap: 8px; }}
.pill {{ font-size: 12px; color: var(--muted); background: var(--bg);
         border: 1px solid var(--border); border-radius: 99px; padding: 3px 10px; }}
.theme-btn {{
  cursor: pointer; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--text); padding: 5px 10px;
  font-size: 13px; transition: background .15s;
}}
.theme-btn:hover {{ background: var(--hover); }}
/* ── Layout ── */
.main {{ max-width: 1200px; margin: 0 auto; padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }}
/* ── Stats row ── */
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
.stat-card {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px;
}}
.stat-num {{ font-size: 26px; font-weight: 700; line-height: 1.1; }}
.stat-label {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.num-green {{ color: var(--green); }}
.num-red   {{ color: var(--red); }}
.num-blue  {{ color: var(--accent); }}
/* ── Cards ── */
.card {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  overflow: hidden;
}}
.card-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
}}
.card-title {{ font-size: 13px; font-weight: 600; letter-spacing: .3px; }}
.card-body {{ padding: 16px; }}
/* ── Config ── */
.config-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.config-label {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
.code-wrap {{ position: relative; }}
pre.code {{
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 14px; font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12.5px; color: var(--accent); line-height: 1.7;
  white-space: pre; overflow-x: auto;
}}
.copy-btn {{
  position: absolute; top: 8px; right: 8px;
  background: var(--btn-bg); color: #fff; border: none; border-radius: 4px;
  padding: 3px 9px; font-size: 11px; cursor: pointer; transition: background .15s;
}}
.copy-btn:hover {{ background: var(--btn-hover); }}
/* ── Tabs ── */
.tabs {{ display: flex; gap: 0; border-bottom: 1px solid var(--border); padding: 0 16px; }}
.tab {{
  padding: 10px 16px; font-size: 13px; cursor: pointer; border: none;
  background: none; color: var(--muted); border-bottom: 2px solid transparent;
  margin-bottom: -1px; transition: color .15s;
}}
.tab.active {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 500; }}
.tab-panel {{ display: none; padding: 0; }}
.tab-panel.active {{ display: block; }}
/* ── Tables ── */
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{
  text-align: left; padding: 9px 14px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .5px; color: var(--muted);
  border-bottom: 1px solid var(--border);
}}
td {{ padding: 9px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: var(--hover); }}
.mono    {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12.5px; }}
.muted   {{ color: var(--muted); }}
.accent  {{ color: var(--accent); }}
.ellipsis {{ max-width: 220px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
.empty   {{ text-align: center; color: var(--muted); padding: 24px; }}
/* ── Tags ── */
.tag {{ display: inline-block; font-size: 11px; border-radius: 4px;
        padding: 1px 6px; margin: 1px; font-family: monospace; }}
.tag-green {{ background: var(--tag-g-bg); color: var(--tag-g-fg); }}
.tag-blue  {{ background: var(--tag-b-bg); color: var(--tag-b-fg); }}
/* ── Status ── */
.status-ok  {{ color: var(--green); font-weight: 600; }}
.status-err {{ color: var(--red);   font-weight: 600; }}
.err-msg    {{ font-size: 11px; color: var(--red); margin-left: 6px; }}
.star       {{ color: var(--star); }}
</style>
</head>
<body>
<!-- ── Header ── -->
<div class="header">
  <div class="header-left">
    <div class="pulse"></div>
    <span class="header-title">Copilot Proxy</span>
    <span class="header-meta">session {SESSION_ID[:8]} · {api_base}</span>
  </div>
  <div class="header-right">
    <span class="pill">⏱ {uptime_str}</span>
    <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">☀ Light</button>
  </div>
</div>

<div class="main">

  <!-- ── Stats ── -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-num num-blue">{req_count}</div>
      <div class="stat-label">Total Requests</div>
    </div>
    <div class="stat-card">
      <div class="stat-num num-green">{ok_count}</div>
      <div class="stat-label">Success</div>
    </div>
    <div class="stat-card">
      <div class="stat-num num-red">{err_count}</div>
      <div class="stat-label">Errors</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{len(models)}</div>
      <div class="stat-label">Available Models</div>
    </div>
  </div>

  <!-- ── Claude Code Configuration ── -->
  <div class="card">
    <div class="card-header">
      <span class="card-title">Claude Code Configuration</span>
      <span style="font-size:12px;color:var(--muted)">Copy and paste into terminal to use</span>
    </div>
    <div class="card-body">
      <div class="config-grid">
        <div>
          <div class="config-label">Option 1: Persistent environment variables</div>
          <div class="code-wrap">
            <pre class="code" id="cfg1">export ANTHROPIC_BASE_URL=http://localhost:8082
export ANTHROPIC_AUTH_TOKEN=dummy
claude</pre>
            <button class="copy-btn" onclick="copy('cfg1')">Copy</button>
          </div>
        </div>
        <div>
          <div class="config-label">Option 2: Single launch</div>
          <div class="code-wrap">
            <pre class="code" id="cfg2">ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_AUTH_TOKEN=dummy claude</pre>
            <button class="copy-btn" onclick="copy('cfg2')">Copy</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Models & Requests (Tabs) ── -->
  <div class="card">
    <div class="tabs">
      <button class="tab active" onclick="showTab(this,'models')">Models ({len(models)})</button>
      <button class="tab" onclick="showTab(this,'requests')">Recent Requests ({len(recent)})</button>
    </div>

    <!-- Models tab -->
    <div id="tab-models" class="tab-panel active">
      <table>
        <thead><tr><th>Model ID</th><th>Name</th><th>Endpoint</th><th></th></tr></thead>
        <tbody>
          <tr><td colspan="4" style="padding:8px 14px;font-size:11px;font-weight:600;color:var(--muted);background:var(--bg)">
            CLAUDE — supports /v1/messages direct passthrough
          </td></tr>
          {model_rows(claude_models)}
          <tr><td colspan="4" style="padding:8px 14px;font-size:11px;font-weight:600;color:var(--muted);background:var(--bg)">
            Other Models
          </td></tr>
          {model_rows(other_models)}
        </tbody>
      </table>
    </div>

    <!-- Requests tab -->
    <div id="tab-requests" class="tab-panel">
      <table>
        <thead><tr><th>Time</th><th>Original Model</th><th>Copilot Model</th><th>Status</th><th>Duration</th><th>Last Message</th></tr></thead>
        <tbody>{req_rows_html()}</tbody>
      </table>
    </div>
  </div>

</div><!-- /main -->

<script>
// ── Theme toggle ──
const html = document.documentElement;
const btn  = document.getElementById('themeBtn');
const saved = localStorage.getItem('theme') || 'dark';
applyTheme(saved);

function applyTheme(t) {{
  html.dataset.theme = t;
  btn.textContent = t === 'dark' ? '☀ Light' : '☾ Dark';
  localStorage.setItem('theme', t);
}}
function toggleTheme() {{
  applyTheme(html.dataset.theme === 'dark' ? 'light' : 'dark');
}}

// ── Tab switch ──
function showTab(el, name) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  el.classList.add('active');
}}

// ── Copy ──
function copy(id) {{
  const el = document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(() => {{
    const btn = el.nextElementSibling;
    btn.textContent = 'Copied ✓';
    setTimeout(() => btn.textContent = 'Copy', 1500);
  }});
}}

// ── Auto-refresh every 30s ──
setTimeout(() => location.reload(), 30000);
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("=== GitHub Copilot → Anthropic Proxy Starting ===")
    logger.info("Session ID: %s", SESSION_ID)
    logger.info("Audit file: %s", AUDIT_FILE)

    logger.info("Initializing GitHub Copilot authentication...")
    try:
        api_key = get_api_key()
        api_base = get_api_base()
        logger.info("Authentication successful, API base: %s", api_base)
        # Preload model list
        models = get_models()
        logger.info("Loaded %d models", len(models))
    except Exception as e:
        logger.warning("Authentication failed at startup (will retry on first request): %s", e)

    print("\n" + "=" * 55)
    print("  Dashboard: http://localhost:8082/ui")
    print("  Configure Claude Code:")
    print("    export ANTHROPIC_BASE_URL=http://localhost:8082")
    print("    export ANTHROPIC_AUTH_TOKEN=dummy")
    print("=" * 55 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="warning")
