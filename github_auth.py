from __future__ import annotations

import json
import math
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTH_ERRORS = {
    "access_denied": "GitHub sign-in was declined. You can try again.",
    "expired_token": "The sign-in code expired. Start sign-in again.",
    "incorrect_client_credentials": "Check the OAuth app Client ID.",
    "device_flow_disabled": "Enable Device Flow in your GitHub OAuth app settings.",
    "incorrect_device_code": "GitHub rejected the sign-in code. Start sign-in again.",
    "unsupported_grant_type": "GitHub rejected the device authorization request.",
}


def oauth_request(path: str, fields: dict[str, str]) -> dict[str, Any]:
    request = Request(
        f"https://github.com/login/{path}",
        data=urlencode(fields).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Telemetry",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except HTTPError as error:
        status = error.code
        error.close()
        raise ValueError(f"GitHub sign-in request failed (HTTP {status}). Try again.") from None
    except (URLError, OSError):
        raise ValueError("Cannot reach GitHub for sign-in. Check your connection and try again.") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("GitHub returned an unreadable sign-in response.") from None
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned an invalid sign-in response.")
    if "error" in payload and not isinstance(payload["error"], str):
        raise ValueError("GitHub returned an invalid sign-in error.")
    return payload


def positive_seconds(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"GitHub returned an invalid sign-in {field}.")
    return value


class GitHubDeviceAuth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "signed_out"
        self._message = "Sign in to GitHub to read your Copilot usage."
        self._client_id = ""
        self._device_code = ""
        self._user_code = ""
        self._expires = 0.0
        self._interval = 5
        self._next_poll = 0.0
        self._token: str | None = None
        self._token_expires: float | None = None
        self._revision = 0

    def _clear(self) -> None:
        self._token = None
        self._token_expires = None
        self._device_code = ""
        self._user_code = ""
        self._revision += 1

    def _expire(self) -> None:
        now = time.monotonic()
        if (
            self._state == "pending" and now >= self._expires
            or self._token_expires is not None and now >= self._token_expires
        ):
            self._clear()
            self._state = "error"
            self._message = "GitHub authorization expired. Sign in again."

    def _snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "message": self._message,
            "user_code": self._user_code if self._state == "pending" else None,
            "verification_uri": "https://github.com/login/device" if self._state == "pending" else None,
            "expires_in": max(0, int(self._expires - time.monotonic())) if self._state == "pending" else None,
            "poll_after": max(1, math.ceil(self._next_poll - time.monotonic())) if self._state == "pending" else None,
        }

    def start(self, client_id: str) -> dict[str, Any]:
        client_id = client_id.strip()
        if not client_id or len(client_id) > 100 or not client_id.isascii() or not client_id.isalnum():
            raise ValueError("Enter the public Client ID from your GitHub OAuth app, not a token or secret.")
        with self._lock:
            self._expire()
            if self._state == "pending":
                raise ValueError("A sign-in is already pending. Cancel it before starting again.")
            if self._state == "authorized":
                raise ValueError("Disconnect the current GitHub sign-in before switching accounts.")
            self._clear()
            self._client_id = client_id
            try:
                payload = oauth_request("device/code", {"client_id": client_id, "scope": "read:user"})
                if "error" in payload:
                    raise ValueError(AUTH_ERRORS.get(payload["error"], "GitHub could not start sign-in. Check your OAuth app settings."))
                device_code, user_code = payload.get("device_code"), payload.get("user_code")
                if (
                    not isinstance(device_code, str) or not device_code
                    or not isinstance(user_code, str) or not user_code
                    or payload.get("verification_uri") != "https://github.com/login/device"
                ):
                    raise ValueError("GitHub returned invalid device authorization details.")
                expires = positive_seconds(payload, "expires_in")
                self._interval = max(5, positive_seconds(payload, "interval"))
            except ValueError as error:
                self._state, self._message = "error", str(error)
                return self._snapshot()
            self._device_code, self._user_code = device_code, user_code
            self._expires = time.monotonic() + expires
            self._next_poll = time.monotonic() + self._interval
            self._state = "pending"
            self._message = "Open GitHub, enter the code below, and approve this app."
            return self._snapshot()

    def snapshot(self, poll: bool = False) -> dict[str, Any]:
        with self._lock:
            self._expire()
            if poll and self._state == "pending" and time.monotonic() >= self._next_poll:
                self._poll()
            return self._snapshot()

    def _poll(self) -> None:
        try:
            payload = oauth_request("oauth/access_token", {
                "client_id": self._client_id,
                "device_code": self._device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
        except ValueError as error:
            self._message = str(error)
            self._next_poll = time.monotonic() + self._interval
            return
        error = payload.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                self._interval += 5
            self._message = "Waiting for you to approve sign-in on GitHub."
            self._next_poll = time.monotonic() + self._interval
            return
        token = payload.get("access_token")
        if error or not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
            self._clear()
            self._state = "error"
            self._message = AUTH_ERRORS.get(error, "GitHub did not return a valid authorization. Try signing in again.")
            return
        try:
            expires = positive_seconds(payload, "expires_in") if "expires_in" in payload else None
        except ValueError as error:
            self._clear()
            self._state, self._message = "error", str(error)
            return
        self._clear()
        self._token = token
        self._token_expires = time.monotonic() + expires if expires is not None else None
        self._state = "authorized"
        self._message = "Signed in to GitHub for this dashboard session. Checking Copilot access..."

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._clear()
            self._state = "signed_out"
            self._message = "GitHub sign-in disconnected. The session token was removed from memory."
            return self._snapshot()

    def credentials(self) -> tuple[int, str | None]:
        with self._lock:
            self._expire()
            return self._revision, self._token
