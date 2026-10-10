"""Model configuration and provider calls for the AI review agents.

Secrets are read only from environment variables named in models.json. They are
never returned by the settings API or included in error text.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("MODELS_FILE", ROOT / "models.json"))
ROLES = ("backend_reviewer", "syntax_reviewer", "workflow_runner")
VALID_PROVIDERS = {"openai-compatible", "anthropic", "gemini"}
MAX_RESPONSE_CHARS = 48_000


def _default_config() -> dict[str, Any]:
    return {
        "models": [],
        "assignments": {role: "" for role in ROLES},
    }


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        models = data.get("models", [])
        assignments = data.get("assignments", {})
        if not isinstance(models, list) or not isinstance(assignments, dict):
            raise ValueError("Invalid model configuration")
        clean_models = []
        for model in models:
            if not isinstance(model, dict):
                continue
            try:
                clean_models.append(validate_model(model))
            except ValueError:
                continue
        return {
            "models": clean_models,
            "assignments": {
                role: str(assignments.get(role, ""))[:100] for role in ROLES
            },
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return _default_config()


def validate_model(model: dict[str, Any]) -> dict[str, Any]:
    name = str(model.get("name", "")).strip()[:100]
    provider = str(model.get("provider", "openai-compatible")).strip()
    base_url = str(model.get("base_url", "")).strip().rstrip("/")
    model_id = str(model.get("model_id", "")).strip()[:200]
    api_key_env = str(model.get("api_key_env", "")).strip()
    if not name or not model_id:
        raise ValueError("Model name and model ID are required.")
    if provider not in VALID_PROVIDERS:
        raise ValueError("Unsupported provider.")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", api_key_env):
        raise ValueError("API key environment name must use uppercase letters, digits, and underscores.")
    if (
        not base_url.startswith("https://")
        or "@" in base_url
        or "?" in base_url
        or "#" in base_url
    ):
        raise ValueError("Provider base URL must use HTTPS.")
    if any(ch in base_url for ch in "\r\n"):
        raise ValueError("Invalid base URL.")
    return {
        "name": name,
        "provider": provider,
        "base_url": base_url,
        "model_id": model_id,
        "api_key_env": api_key_env,
        "enabled": bool(model.get("enabled", False)),
    }


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    models = config.get("models", [])
    assignments = config.get("assignments", {})
    if not isinstance(models, list) or len(models) > 100:
        raise ValueError("Provide up to 100 model entries.")
    cleaned = [validate_model(item) for item in models]
    names = [item["name"].casefold() for item in cleaned]
    if len(names) != len(set(names)):
        raise ValueError("Model names must be unique.")
    ids = {model["name"] for model in cleaned}
    role_map = {}
    for role in ROLES:
        selected = str(assignments.get(role, ""))
        if selected and selected not in ids:
            raise ValueError(f"Choose an existing model for {role}.")
        role_map[role] = selected
    safe = {"models": cleaned, "assignments": role_map}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(safe, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)
    return safe


def public_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    return {
        "models": [
            {**model, "key_configured": bool(os.getenv(model["api_key_env"]))}
            for model in config["models"]
        ],
        "assignments": config["assignments"],
        "roles": list(ROLES),
    }


def _model_order(role: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    enabled = [
        model for model in config["models"]
        if model.get("enabled") and os.getenv(model.get("api_key_env", ""))
    ]
    selected = config["assignments"].get(role)
    ordered = []
    if selected:
        ordered.extend(m for m in enabled if m["name"] == selected)
    ordered.extend(m for m in enabled if m["name"] != selected)
    return ordered


def _extract_text(response: dict[str, Any], provider: str) -> str:
    if provider == "anthropic":
        content = response.get("content", [])
        return "\n".join(
            str(block.get("text", "")) for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if provider == "gemini":
        candidates = response.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        return "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
    choices = response.get("choices", [])
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content)


async def _request(model: dict[str, Any], messages: list[dict[str, str]]) -> str:
    key = os.getenv(model["api_key_env"], "")
    if not key:
        raise RuntimeError("API key is not configured.")
    provider = model["provider"]
    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        url = model["base_url"] + "/v1/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body = {
            "model": model["model_id"],
            "max_tokens": 4096,
            "messages": [
                {"role": item["role"], "content": item["content"]}
                for item in messages if item["role"] != "system"
            ],
            "system": next((m["content"] for m in messages if m["role"] == "system"), ""),
        }
    elif provider == "gemini":
        url = f"{model['base_url']}/v1beta/models/{model['model_id']}:generateContent"
        headers["x-goog-api-key"] = key
        contents = [
            {"role": "model" if item["role"] == "assistant" else "user",
             "parts": [{"text": item["content"]}]}
            for item in messages if item["role"] != "system"
        ]
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": 4096}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
    else:
        url = model["base_url"] + "/chat/completions"
        headers["Authorization"] = f"Bearer {key}"
        body = {"model": model["model_id"], "messages": messages, "temperature": 0.2}

    async with httpx.AsyncClient(timeout=httpx.Timeout(75, connect=15)) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.is_error:
        # Never forward response bodies or request URLs: providers can echo
        # credentials or sensitive prompts in their error details.
        raise RuntimeError(f"Provider returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Provider returned an unreadable response.") from exc
    text = _extract_text(data, provider).strip()
    if not text:
        raise RuntimeError("Provider returned an empty response.")
    return text[:MAX_RESPONSE_CHARS]


async def ask_model(
    role: str,
    messages: list[dict[str, str]],
    config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Try the assigned model, then other enabled/key-configured models."""
    config = config or load_config()
    models = _model_order(role, config)
    if not models:
        raise RuntimeError(
            "No enabled model has a configured API key. Add a model and set its key in .env."
        )
    failures = []
    for model in models[:4]:
        for attempt in range(2):
            try:
                return await _request(model, messages), model["name"]
            except (httpx.TimeoutException, httpx.NetworkError):
                error = "Network timeout or connection error."
            except RuntimeError as exc:
                error = str(exc)
            except Exception:
                error = "Unexpected provider error."
            failures.append(f"{model['name']}: {error}")
            if attempt == 0 and error.startswith("Provider returned HTTP 5"):
                continue
            break
    raise RuntimeError("All configured models failed: " + " | ".join(failures))


def json_from_model_text(text: str) -> Any:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise
