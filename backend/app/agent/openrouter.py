

from __future__ import annotations

import json
import os

import re

import time

from typing import Any, Callable, Optional

import httpx

from ..audit import log_event

from ..config import Settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

RETRYABLE = {408, 409, 425, 500, 502, 503, 504}

NON_RETRYABLE = {400, 401, 403, 404, 422}

class AIServiceError(Exception):

    pass

class AIConfigurationError(AIServiceError):

    pass

class AIProviderError(AIServiceError):

    def __init__(

        self,

        message: str,

        *,

        status_code: Optional[int] = None,

        details: Any = None,

    ):

        super().__init__(message)

        self.status_code = status_code

        self.details = details

def clean_json_response(text: str) -> str:

    if not text:

        raise ValueError("AI returned an empty response.")

    text = text.strip()

    text = re.sub(r"^\`\`\`(?:json)?\s*", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s*\`\`\`$", "", text).strip()

    for opener, closer in (("", ""), ("{", "}"), ("[", "]")):

        if not opener:

            candidate = text

        else:

            start = text.find(opener)

            end = text.rfind(closer)

            candidate = (

                text[start : end + 1]

                if start != -1 and end > start

                else None

            )

        if not candidate:

            continue

        try:

            json.loads(candidate)

            return candidate

        except json.JSONDecodeError:

            continue

    raise ValueError(f"AI returned invalid JSON: {text[:300]}")

def parse_json_response(text: str) -> Any:

    return json.loads(clean_json_response(text))

def _provider_error(resp: httpx.Response) -> str:

    try:

        data = resp.json()

    except ValueError:

        return resp.text[:1000]

    err = data.get("error", data)

    if isinstance(err, dict):

        meta = err.get("metadata") or {}

        parts = [

            str(part)

            for part in (

                err.get("message"),

                meta.get("raw"),

                meta.get("provider_name"),

                meta.get("remedy_hint"),

            )

            if part

        ]

        if parts:

            return " | ".join(parts)[:2000]

    return json.dumps(data)[:2000]

class OpenRouterClient:

    def __init__(

        self,

        settings: Settings,

        http: httpx.Client | None = None,

        sleep: Callable[[float], None] = time.sleep,

        monotonic: Callable[[], float] = time.monotonic,

    ):

        self.s = settings

        self._http = http

        self._sleep = sleep

        self._monotonic = monotonic

    def model_chain(self) -> list[str]:

        models = [self.s.primary_model, *(self.s.fallback_models if self.s.llm_enable_fallbacks else ())]

        return list(

            dict.fromkeys(

                model.strip()

                for model in models

                if model and model.strip()

            )

        )

    def chat(

        self,

        messages: list[dict],

        *,

        tools: list[dict] | None = None,

        tool_choice: str | None = None,

        temperature: float = 0.1,

        max_tokens: int = 15000,

        response_format: dict | None = None,

        request_timeout: float | None = None,

    ) -> dict:

        if not self.s.openrouter_api_key:

            raise AIConfigurationError(

                "OPENROUTER_API_KEY is not configured."

            )

        models = self.model_chain()

        if not models:

            raise AIConfigurationError(

                "No OpenRouter models are configured."

            )

        base_payload: dict[str, Any] = {

            "messages": messages,

            "temperature": temperature,

            "max_tokens": max_tokens,

        }

        if tools:

            base_payload["tools"] = tools

            if tool_choice:

                base_payload["tool_choice"] = tool_choice

        if response_format:

            base_payload["response_format"] = response_format

        headers = {

            "Authorization": f"Bearer {self.s.openrouter_api_key}",

            "HTTP-Referer": self.s.app_url,

            "X-Title": "Migration Workbench",

            "Content-Type": "application/json",

        }

        status: Optional[int] = None

        details: Any = None

        owns = self._http is None

        deadline = None if request_timeout is None else self._monotonic() + max(0.1, request_timeout)

        http = self._http or httpx.Client(

            timeout=min(self.s.llm_timeout, request_timeout) if request_timeout else self.s.llm_timeout

        )

        try:

            for model in models:

                payload = {**base_payload, "model": model}

                for attempt in range(self.s.llm_max_retries + 1):

                    remaining = None if deadline is None else deadline - self._monotonic()

                    if remaining is not None and remaining <= 0:

                        status, details = 408, "Request deadline exceeded before another provider attempt could start."

                        break

                    attempt_timeout = min(self.s.llm_timeout, remaining) if remaining is not None else self.s.llm_timeout

                    t0 = time.perf_counter()

                    try:

                        resp = http.post(

                            OPENROUTER_URL,

                            headers=headers,

                            json=payload,

                            timeout=attempt_timeout,

                        )

                    except httpx.TimeoutException as exc:

                        status, details = 408, str(exc)

                        log_event(

                            None,

                            "warning",

                            "llm.timeout",

                            attempt=attempt + 1,

                            model=model,

                            latency_ms=round((time.perf_counter() - t0) * 1000),

                        )

                    except httpx.HTTPError as exc:

                        status, details = None, str(exc)

                        log_event(

                            None,

                            "warning",

                            "llm.network_error",

                            attempt=attempt + 1,

                            model=model,

                            latency_ms=round((time.perf_counter() - t0) * 1000),

                            error=type(exc).__name__,

                        )

                    else:

                        latency_ms = round((time.perf_counter() - t0) * 1000)

                        if resp.status_code < 400:

                            try:

                                data = resp.json()

                            except ValueError as exc:

                                raise AIProviderError(

                                    "OpenRouter returned a non-JSON "

                                    "success response.",

                                    status_code=resp.status_code,

                                    details=resp.text[:500],

                                ) from exc

                            if isinstance(data, dict) and "error" in data:

                                err = data["error"]

                                err_msg = (

                                    err.get("message")

                                    if isinstance(err, dict)

                                    else str(err)

                                )

                                err_code = (

                                    err.get("code")

                                    if isinstance(err, dict)

                                    else None

                                ) or 502

                                status = (

                                    int(err_code)

                                    if str(err_code).isdigit()

                                    else 502

                                )

                                details = err_msg

                                log_event(

                                    None,

                                    (

                                        "warning"

                                        if status in RETRYABLE or status == 429

                                        else "error"

                                    ),

                                    "llm.http_error",

                                    status=status,

                                    model=model,

                                    attempt=attempt + 1,

                                    latency_ms=latency_ms,

                                    details=str(details)[:300],

                                )

                                if status in NON_RETRYABLE:

                                    break

                                if status != 429 and status not in RETRYABLE:

                                    break

                            else:

                                choices = data.get("choices") or []

                                msg = (

                                    choices[0].get("message")

                                    if choices

                                    else None

                                )

                                if (

                                    not msg

                                    or (

                                        not msg.get("content")

                                        and not msg.get("tool_calls")

                                    )

                                ):

                                    if (

                                        choices

                                        and choices[0].get("finish_reason")

                                        == "length"

                                        and msg

                                        and msg.get("reasoning")

                                    ):

                                        raise AIProviderError(

                                            "AI model exhausted token limit "

                                            "during reasoning before "

                                            "completing output.",

                                            status_code=resp.status_code,

                                            details=(

                                                "Model hit token limit "

                                                "while reasoning."

                                            ),

                                        )

                                    raise AIProviderError(

                                        "OpenRouter returned an empty message.",

                                        status_code=resp.status_code,

                                        details=data,

                                    )

                                log_event(

                                    None,

                                    "info",

                                    "llm.response",

                                    model=data.get("model") or model,

                                    attempt=attempt + 1,

                                    latency_ms=latency_ms,

                                    retry_count=attempt,

                                    tool_calls=len(msg.get("tool_calls") or []),

                                )

                                return {

                                    "message": msg,

                                    "model": data.get("model") or model,

                                }

                        else:

                            status = resp.status_code

                            details = _provider_error(resp)

                            log_event(

                                None,

                                (

                                    "warning"

                                    if status in RETRYABLE or status == 429

                                    else "error"

                                ),

                                "llm.http_error",

                                status=status,

                                model=model,

                                attempt=attempt + 1,

                                latency_ms=latency_ms,

                                details=details[:300],

                            )

                            if status in NON_RETRYABLE:

                                break

                            if status != 429 and status not in RETRYABLE:

                                break

                    if attempt < self.s.llm_max_retries:

                        delay = min(2**attempt, 5)

                        if deadline is not None:

                            remaining = deadline - self._monotonic()

                            if remaining <= 0:

                                status, details = 408, "Request deadline exceeded before retry sleep."

                                break

                            delay = min(delay, remaining)

                        self._sleep(delay)

                if status in NON_RETRYABLE:

                    break

                if deadline is not None and self._monotonic() >= deadline:

                    status = 408

                    break

        finally:

            if owns:

                http.close()

        # Optional cross-provider fallback: OpenRouter remains primary.
        # Only fail over for transient/network errors, not malformed requests or auth errors.
        nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
        nvidia_model = os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v4.1-flash").strip()
        nvidia_base_url = os.getenv(
            "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).rstrip("/")
        should_fallback = status is None or status == 429 or status in RETRYABLE

        if nvidia_key and nvidia_model and should_fallback:
            fallback_payload = {**base_payload, "model": nvidia_model}
            fallback_headers = {
                "Authorization": f"Bearer {nvidia_key}",
                "Content-Type": "application/json",
            }
            fallback_http = httpx.Client(
                timeout=min(self.s.llm_timeout, request_timeout)
                if request_timeout
                else self.s.llm_timeout
            )
            try:
                fallback_started = time.perf_counter()
                fallback_resp = fallback_http.post(
                    f"{nvidia_base_url}/chat/completions",
                    headers=fallback_headers,
                    json=fallback_payload,
                    timeout=self.s.llm_timeout,
                )
                fallback_latency = round((time.perf_counter() - fallback_started) * 1000)
                if fallback_resp.status_code < 400:
                    fallback_data = fallback_resp.json()
                    choices = fallback_data.get("choices") or []
                    fallback_msg = choices[0].get("message") if choices else None
                    if fallback_msg and (
                        fallback_msg.get("content") or fallback_msg.get("tool_calls")
                    ):
                        log_event(
                            None,
                            "info",
                            "llm.fallback_response",
                            provider="nvidia",
                            model=fallback_data.get("model") or nvidia_model,
                            latency_ms=fallback_latency,
                        )
                        return {
                            "message": fallback_msg,
                            "model": fallback_data.get("model") or nvidia_model,
                        }
                    details = "NVIDIA fallback returned an empty message."
                else:
                    details = _provider_error(fallback_resp)
                    status = fallback_resp.status_code
                log_event(
                    None,
                    "warning",
                    "llm.fallback_failed",
                    provider="nvidia",
                    model=nvidia_model,
                    status=status,
                    latency_ms=fallback_latency,
                    details=str(details)[:300],
                )
            except (httpx.HTTPError, ValueError) as exc:
                details = str(exc)
                log_event(
                    None,
                    "warning",
                    "llm.fallback_failed",
                    provider="nvidia",
                    model=nvidia_model,
                    error=type(exc).__name__,
                    details=str(exc)[:300],
                )
            finally:
                fallback_http.close()

        raise AIProviderError(
            "All configured AI models/providers failed.",
            status_code=status,
            details=details,
        )
