"""Session-scoped HTTP access. Never cache credentials or user-owned responses."""

import os
from pathlib import Path

import httpx
from dotenv import dotenv_values


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def backend_url():
    settings = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    return (os.getenv("BACKEND_URL") or settings.get("BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")


def error_message(response):
    if response.status_code >= 500:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        ai_errors = {
            "ai_upload_timeout": "Uploading the image to the AI provider timed out. Please retry on a stable connection.",
            "ai_timeout": "The AI provider took too long to respond. Please retry shortly.",
            "ai_connection_error": "The connection to the AI provider failed. Check your connection and retry.",
            "ai_model_unavailable": "The selected AI model is unavailable. Check its configuration or choose a compatible provider.",
            "ai_access_denied": "The AI provider rejected its API credentials or model permissions. Check the server configuration.",
            "ai_rate_limited": "The AI provider's rate limit was reached. Please wait before retrying.",
            "ai_invalid_output": "The AI response did not match the required nutrition format. Please retry or clarify the meal description.",
            "ai_not_configured": "No model is configured for these inputs on the selected provider. Images need a vision-capable model; use NVIDIA for photos.",
            "ai_image_invalid": "The image could not be prepared for AI. Try uploading a valid smaller JPEG, PNG, or WebP.",
            "ai_unavailable": "The AI provider could not finish the request. Please retry shortly.",
        }
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in ai_errors:
            return ai_errors[code] + " No tracking records were confirmed. Your input is retained."
        return (
            "The service could not finish this request. For AI, the selected model may be "
            "unavailable. Nothing is saved automatically; keep your input and try again, "
            "choose another provider, or log manually."
        )
    try:
        detail = response.json().get("detail", "The request could not be completed.")
    except (ValueError, AttributeError):
        return f"The service returned an unexpected response ({response.status_code}). Please try again."
    if isinstance(detail, list):
        messages = []
        for error in detail:
            if isinstance(error, dict):
                field = ".".join(str(part) for part in error.get("loc", []) if part != "body")
                messages.append(f"{field}: {error.get('msg', 'Invalid value')}")
        return "; ".join(messages) or "Please check the values and try again."
    if isinstance(detail, dict):
        return str(detail.get("message", "Please check the request and try again."))
    return str(detail)[:1500]


class ApiClient:
    def __init__(self, token=None, base_url=None, transport=None):
        self.token = token
        self.base_url = base_url or backend_url()
        self.transport = transport

    def request(self, method, path, *, ai=False, missing_ok=False, binary=False, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(180.0 if ai else 25.0, connect=10.0),
                transport=self.transport,
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(
                "The request timed out. Your input is still here. Before retrying a save, "
                "refresh the day or draft list to check whether it completed."
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError("Cannot reach the service. Please check that the backend is running and try again.") from exc
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code == 401:
            raise ApiError("Your session has expired. Please sign in again.", 401)
        if response.is_error:
            raise ApiError(error_message(response), response.status_code)
        if binary:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("The service returned an unreadable response. Please try again.") from exc

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def upload(self, image):
        return self.post("/images", files={"file": (image.name, image.getvalue(), image.type or "application/octet-stream")})["id"]
