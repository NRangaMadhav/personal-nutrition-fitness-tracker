import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
import requests
from groq import APIError
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from pydantic import BaseModel, ValidationError
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import get_settings
from .schemas import ProviderChoice

logger = logging.getLogger(__name__)

class ProviderUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: str = "ai_unavailable"):
        super().__init__(message)
        self.code = code


class StructuredOutputUnsupported(ValueError):
    pass


STRUCTURED_OUTPUT_MARKERS = (
    "tool", "function", "structured", "json_schema", "response_format", "guided_json", "guided_decoding"
)


def check_nvidia_response(response, *args, **kwargs):
    # NVIDIA's integration otherwise converts HTTP failures into bare Exception,
    # including response bodies. Fail at the transport boundary with typed errors.
    if response.status_code in (400, 422) and any(
        marker in response.text.lower() for marker in STRUCTURED_OUTPUT_MARKERS
    ):
        raise StructuredOutputUnsupported("The NVIDIA model rejected structured-output mode.")
    if response.status_code == 404:
        raise ProviderUnavailable(
            "nvidia model is unavailable for inference with this API account. "
            "Check NVIDIA_MODEL/NVIDIA_VISION_MODEL; model-list presence does not guarantee inference access.",
            code="ai_model_unavailable",
        )
    if response.status_code in (401, 403):
        raise ProviderUnavailable(
            "nvidia rejected the configured credentials or model access permissions.", code="ai_access_denied"
        )
    if response.status_code == 429:
        raise ProviderUnavailable(
            "nvidia rate limit reached. Please wait and retry, or select another provider.", code="ai_rate_limited"
        )
    response.raise_for_status()
    if response.status_code == 202 and "NVCF-REQID" not in response.headers:
        raise ProviderUnavailable("nvidia returned an invalid asynchronous response. Please retry.")
    if response.status_code == 200:
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            raise ProviderUnavailable("nvidia returned an invalid JSON response. Please retry.") from None
        if isinstance(payload, dict) and "error" in payload and "choices" not in payload:
            raise ProviderUnavailable("nvidia could not complete the request. Please retry.")
    return response


class BoundedNvidiaSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.hooks["response"].append(check_nvidia_response)
        self.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=1,
                    connect=1,
                    read=0,
                    status=1,
                    status_forcelist=(502, 503, 504),
                    allowed_methods=frozenset(("GET", "POST")),
                    backoff_factor=0.2,
                    respect_retry_after_header=False,
                    raise_on_status=False,
                )
            ),
        )

    def request(self, method, url, **kwargs):
        # requests also uses the connect timeout while writing the image request body.
        kwargs["timeout"] = (30, 60)
        return super().request(method, url, **kwargs)


def transport_failure(name: str, model: str, exc: Exception) -> ProviderUnavailable:
    code = "ai_connection_error"
    cause = exc
    for _ in range(10):
        if isinstance(cause, TimeoutError) and "write" in str(cause).lower():
            code = "ai_upload_timeout"
            break
        if isinstance(cause, (requests.exceptions.Timeout, httpx.TimeoutException, TimeoutError)):
            code = "ai_timeout"
        nested = (
            getattr(cause, "reason", None) or cause.__cause__ or cause.__context__
            or next((arg for arg in reversed(cause.args) if isinstance(arg, BaseException)), None)
        )
        if not isinstance(nested, BaseException) or nested is cause:
            break
        cause = nested
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        code = "ai_access_denied"
    elif status == 404:
        code = "ai_model_unavailable"
    elif status == 429:
        code = "ai_rate_limited"
    elif status is not None:
        code = "ai_unavailable"
    logger.warning(
        "AI transport failed: provider=%s model=%s code=%s error=%s status=%s",
        name, model, code, type(exc).__name__, status,
    )
    return ProviderUnavailable(
        f"{name} could not complete the request ({code}). Please retry or choose another provider.", code=code
    )


@dataclass(frozen=True)
class AdapterResult:
    data: BaseModel
    provider: str
    model: str


class ProviderAdapter(Protocol):
    name: str
    model: str

    def invoke(self, messages: list[BaseMessage], schema: type[BaseModel]) -> AdapterResult: ...


def parse_json(content, schema: type[BaseModel]) -> BaseModel:
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    if not isinstance(content, str):
        raise ValueError("The model did not return textual JSON.")
    text = content.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        if not match:
            raise ValueError("Incomplete JSON code block.")
        text = match.group(1)
    return schema.model_validate_json(text)


class LangChainAdapter:
    def __init__(self, name: str, model: str, key: str):
        self.name = name
        self.model = model
        if name == "groq":
            self.client = ChatGroq(
                model=model, api_key=key, temperature=0, timeout=httpx.Timeout(30, connect=5), max_retries=1
            )
            self.structured_supported = True
        else:
            # This reasoning model supports direct mode, leaving the output budget for the JSON draft.
            model_kwargs = (
                {"chat_template_kwargs": {"enable_thinking": False}}
                if model == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning" else {}
            )
            self.client = ChatNVIDIA(
                model=model, api_key=key, temperature=0, max_completion_tokens=4096, timeout=60,
                model_kwargs=model_kwargs,
            )
            self.client._client.get_session_fn = BoundedNvidiaSession
            self.client._client.interval = 1
            self.structured_supported = (
                getattr(self.client._client.model, "supports_structured_output", False) is True
            )

    def invoke(self, messages: list[BaseMessage], schema: type[BaseModel]) -> AdapterResult:
        instructions = [
            message.content for message in messages if isinstance(message, SystemMessage)
        ]
        instructions.append(
            "Return only JSON matching this schema. Never omit required keys; use null for unknown values "
            "where the schema permits it.\n" + json.dumps(schema.model_json_schema())
        )
        messages = [
            SystemMessage(content="\n\n".join(instructions)),
            *(message for message in messages if not isinstance(message, SystemMessage)),
        ]
        try:
            structured = (
                self.client.with_structured_output(schema)
                if getattr(self, "structured_supported", True) else None
            )
        except (NotImplementedError, TypeError, ValueError):
            structured = None
        if structured is not None:
            try:
                parsed = structured.invoke(messages)
                if not isinstance(parsed, schema):
                    parsed = schema.model_validate(parsed)
                return AdapterResult(parsed, self.name, self.model)
            except (NotImplementedError, OutputParserException, ValidationError, StructuredOutputUnsupported):
                # Some deployed models expose tools but cannot emit this schema.
                pass
            except (APIError, RequestException, httpx.HTTPError, ValueError, TimeoutError) as exc:
                reason = str(exc).lower()
                if not any(
                    marker in reason for marker in STRUCTURED_OUTPUT_MARKERS
                ):
                    raise transport_failure(self.name, self.model, exc) from None
        for attempt in range(2):
            try:
                response = self.client.invoke(messages)
            except (APIError, RequestException, httpx.HTTPError, OutputParserException, ValueError, TimeoutError) as exc:
                raise transport_failure(self.name, self.model, exc) from None
            try:
                parsed = parse_json(response.content, schema)
            except (ValueError, OutputParserException) as exc:
                logger.warning("AI output validation failed: provider=%s model=%s attempt=%s error=%s",
                               self.name, self.model, attempt + 1, type(exc).__name__)
                if attempt:
                    raise ProviderUnavailable(
                        f"{self.name} returned invalid structured data. Please retry or choose another provider.",
                        code="ai_invalid_output",
                    ) from None
                if isinstance(exc, ValidationError):
                    issues = "; ".join(
                        f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                        for error in exc.errors()[:12]
                    )
                else:
                    issues = "The response was not a complete JSON object matching the required schema."
                messages = [
                    *messages,
                    AIMessage(content=response.content),
                    HumanMessage(content=(
                        f"Your response failed validation: {issues}. Return a corrected complete JSON object only, "
                        "matching the original schema. Include every required field; use null for unknown values "
                        "where allowed. Remove fields marked extra_forbidden from their invalid locations. "
                        "Do not invent missing personal measurements or change the input facts."
                    )),
                ]
                continue
            return AdapterResult(parsed, self.name, self.model)
        raise ProviderUnavailable(f"{self.name} could not produce validated output.")


def get_adapter(name: str, *, vision: bool) -> ProviderAdapter:
    settings = get_settings()
    key = getattr(settings, f"{name}_api_key")
    model = getattr(settings, f"{name}_{'vision_model' if vision else 'model'}")
    if not key or not model:
        capability = "vision" if vision else "text"
        raise ProviderUnavailable(
            f"{name} {capability} credentials/model are not configured.", code="ai_not_configured"
        )
    try:
        return LangChainAdapter(name, model, key)
    except (ValueError, APIError, RequestException, httpx.HTTPError):
        raise ProviderUnavailable(f"{name} could not initialize the configured model.") from None


def invoke_provider(
    messages: list[BaseMessage],
    schema: type[BaseModel],
    *,
    provider: ProviderChoice = "auto",
    vision: bool = False,
) -> AdapterResult:
    primary = get_settings().ai_primary_provider
    if primary not in ("nvidia", "groq"):
        raise ProviderUnavailable("AI_PRIMARY_PROVIDER must be nvidia or groq.")
    candidates = [primary, "groq" if primary == "nvidia" else "nvidia"] if provider == "auto" else [provider]
    errors = []
    for name in candidates:
        try:
            return get_adapter(name, vision=vision).invoke(messages, schema)
        except ProviderUnavailable as exc:
            errors.append(exc)
    raise ProviderUnavailable(" ".join(str(exc) for exc in errors), code=errors[0].code) from None
