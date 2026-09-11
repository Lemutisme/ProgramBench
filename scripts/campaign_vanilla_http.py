"""Bounded, redirect-free HTTP adapter for the public OpenCode Session API."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


ORIGINAL_TASK = (
    "Implement the documented program in this cleanroom. Produce source, a relocatable compile.sh, and "
    "./executable. Use the provided reference only for black-box experiments. "
    "The reference entrypoints are /candidate/reference and /workspace/executable; "
    "the candidate you build is /candidate/executable. These are different programs. "
    "The reference symlink may be omitted by file-listing or Git tools; omission is not absence. "
    "Internet access is forbidden during inference and is disabled. Do not obtain upstream source, "
    "alternate reference binaries, or additional dependencies from outside this cleanroom. "
    "Use only the supplied documentation, permitted black-box reference behavior, preinstalled tools/libraries, "
    "and code you implement here. Do not read or decompile the reference binary."
    " Also deliver an executable, self-contained validate.sh of regression cases derived from permitted "
    "reference observations. Cover applicable commands, flags, errors, boundaries and state transitions. "
    "Use batched differential probes and preserve discoveries as regression tests. A passing build alone "
    "is not a reason to stop behavioral implementation. All work and local validation finish in this Session; "
    "the host evaluates the final artifact later without feeding hidden tests back into your search."
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class VanillaError(RuntimeError):
    """Raised when a vanilla trajectory cannot be safely run or adopted."""


class VanillaHttpError(VanillaError):
    """Bounded, sanitized failure from the public OpenCode HTTP API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        transient: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.transient = transient
        super().__init__(message[:1000])


@dataclass(frozen=True)
class VanillaPromptRequest:
    session_id: str
    message_id: str
    text: str
    delivery: Literal["steer", "queue"]
    model: str
    variant: str
    directory: str
    agent: str | None
    files: tuple[Mapping[str, object], ...]
    permission: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if (
            not self.session_id.startswith("ses_")
            or not self.message_id.startswith("msg_")
            or self.text != ORIGINAL_TASK
            or self.delivery != "steer"
            or self.model not in {"openai/gpt-5.6-luna", "openai/gpt-5.6-sol"}
            or self.variant != ("xhigh" if self.model == "openai/gpt-5.6-sol" else "max")
            or self.directory != "/candidate"
            or self.agent is not None
            or self.files
            or self.permission != (("*", "*", "allow"),)
        ):
            raise VanillaError("vanilla prompt request is invalid")
        json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "text": self.text,
            "delivery": self.delivery,
            "model": self.model,
            "variant": self.variant,
            "directory": self.directory,
            "agent": self.agent,
            "files": [dict(file) for file in self.files],
            "permission": [
                {"permission": permission, "pattern": pattern, "action": action}
                for permission, pattern, action in self.permission
            ],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def api_payload(self, *, resume: bool) -> dict[str, object]:
        return {"id": self.message_id, "prompt": {"text": self.text}, "delivery": self.delivery, "resume": resume}


class OpenCodeApi(Protocol):
    def get_session(self, session_id: str) -> Mapping[str, object] | None: ...

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> Mapping[str, object]: ...

    def allow_permissions(self, session_id: str, *, directory: str) -> None: ...

    def get_message(self, session_id: str, message_id: str) -> Mapping[str, object] | None: ...

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None: ...

    def messages(self, session_id: str) -> Mapping[str, object]: ...

    def status(self, session_id: str) -> str: ...

    def interrupt(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse: ...


class UrllibHttpClient:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, new_url):
            return None

    def __init__(self) -> None:
        self._opener = build_opener(self.NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise VanillaHttpError("OpenCode API response exceeds size limit", transient=True)
                return HttpResponse(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    payload,
                )
        except HTTPError as error:
            payload = error.read(max_bytes + 1)
            raise VanillaHttpError(
                f"OpenCode API request failed with HTTP {error.code}"
                + (" and an oversized response" if len(payload) > max_bytes else ""),
                status_code=error.code,
                retry_after=error.headers.get("retry-after"),
                transient=error.code >= 500,
            ) from None
        except (OSError, URLError) as error:
            raise VanillaHttpError(f"OpenCode API network failure: {error}", transient=True) from None


class HttpOpenCodeApi:
    """Bounded client locked to one literal loopback server origin."""

    def __init__(
        self,
        base_url: str,
        *,
        expected_port: int,
        http_client: HttpClient | None = None,
        request_timeout: float = 10.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None
            or type(expected_port) is not int
            or not 0 < expected_port < 65536
            or parsed.port != expected_port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise VanillaError("OpenCode API must use a loopback HTTP origin")
        if not 0 < request_timeout <= 30 or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES:
            raise VanillaError("OpenCode API bounds are invalid")
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client or UrllibHttpClient()
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes

    def get_session(self, session_id: str) -> Mapping[str, object] | None:
        try:
            return self._envelope(self._request("GET", f"/api/session/{_id(session_id, 'ses_')}"))
        except VanillaHttpError as error:
            if error.status_code == 404:
                return None
            raise

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> Mapping[str, object]:
        provider_id, model_id = model.split("/", 1)
        return self._envelope(
            self._request(
                "POST",
                "/api/session",
                {
                    "id": _id(session_id, "ses_"),
                    "model": {"id": model_id, "providerID": provider_id, "variant": variant},
                    "location": {"directory": directory},
                    **({"agent": agent} if agent is not None else {}),
                },
            )
        )

    def allow_permissions(self, session_id: str, *, directory: str) -> None:
        self._request(
            "PATCH",
            f"/session/{_id(session_id, 'ses_')}?{urlencode({'directory': directory})}",
            {"permission": [{"permission": "*", "pattern": "*", "action": "allow"}]},
        )

    def get_message(self, session_id: str, message_id: str) -> Mapping[str, object] | None:
        try:
            return self._envelope(
                self._request("GET", f"/api/session/{_id(session_id, 'ses_')}/message/{_id(message_id, 'msg_')}")
            )
        except VanillaHttpError as error:
            if error.status_code == 404:
                return None
            raise

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        admitted = self._envelope(
            self._request(
                "POST",
                f"/api/session/{_id(request.session_id, 'ses_')}/prompt",
                request.api_payload(resume=resume),
            )
        )
        _validate_admitted(admitted, request)

    def messages(self, session_id: str) -> Mapping[str, object]:
        messages: list[object] = []
        cursor = None
        while True:
            query = {"limit": 200, **({"order": "asc"} if cursor is None else {"cursor": cursor})}
            raw = self._json(self._request("GET", f"/api/session/{_id(session_id, 'ses_')}/message?{urlencode(query)}"))
            if (
                not isinstance(raw, Mapping)
                or set(raw) != {"data", "cursor"}
                or not isinstance(raw["data"], list)
                or not isinstance(raw["cursor"], Mapping)
            ):
                raise VanillaError("OpenCode projected messages response is invalid")
            messages.extend(raw["data"])
            if len(messages) > 10_000:
                raise VanillaError("OpenCode projected message count exceeds campaign limit")
            next_cursor = raw["cursor"].get("next")
            if not raw["data"] or next_cursor is None:
                return {"data": messages}
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise VanillaError("OpenCode projected message cursor is invalid")
            cursor = next_cursor

    def status(self, session_id: str) -> str:
        raw = self._envelope(self._request("GET", "/api/session/active"))
        if any(not isinstance(key, str) or not isinstance(value, Mapping) for key, value in raw.items()):
            raise VanillaError("OpenCode Session status response is invalid")
        status_value = raw.get(session_id)
        if status_value is not None and status_value.get("type") != "running":
            raise VanillaError("OpenCode Session status entry is invalid")
        return "busy" if status_value is not None else "idle"

    def interrupt(self, session_id: str) -> None:
        self._request("POST", f"/api/session/{_id(session_id, 'ses_')}/interrupt", expected=(204,))

    def feedback(self, session_id: str, message_id: str, text: str) -> None:
        self._request("POST", f"/api/session/{_id(session_id, 'ses_')}/prompt",
                      {"id": _id(message_id, "msg_"), "prompt": {"text": text}, "delivery": "steer", "resume": True})

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> HttpResponse:
        body = None if payload is None else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if body is not None and len(body) > 1024 * 1024:
            raise VanillaError("OpenCode API request exceeds size limit")
        response = self.http_client.request(
            method,
            self.base_url + path,
            body=body,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {}),
                     **({"Authorization": "Basic " + base64.b64encode(
                         (os.environ.get("OPENCODE_SERVER_USERNAME", "opencode") + ":" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()).decode()}
                        if os.environ.get("OPENCODE_SERVER_PASSWORD") else {})},
            timeout=self.request_timeout,
            max_bytes=self.max_response_bytes,
        )
        if response.status not in expected:
            raise VanillaHttpError(
                f"OpenCode API request failed with HTTP {response.status}",
                status_code=response.status,
                retry_after=response.headers.get("retry-after"),
                transient=response.status >= 500,
            )
        return response

    def _envelope(self, response: HttpResponse) -> Mapping[str, object]:
        raw = self._json(response)
        if not isinstance(raw, Mapping) or set(raw) != {"data"} or not isinstance(raw["data"], Mapping):
            raise VanillaError("OpenCode API response envelope is invalid")
        return raw["data"]

    def _json(self, response: HttpResponse) -> object:
        try:
            return json.loads(
                response.body,
                object_pairs_hook=_unique_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeError, ValueError) as error:
            raise VanillaError("OpenCode API returned invalid JSON") from error


def _id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 256:
        raise VanillaError("OpenCode ID is invalid")
    return quote(value, safe="_-.")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_admitted(admitted: Mapping[str, object], request: VanillaPromptRequest) -> None:
    required = {"admittedSeq", "id", "sessionID", "prompt", "delivery", "timeCreated"}
    if frozenset(admitted) not in {frozenset(required), frozenset({*required, "promotedSeq"})}:
        raise VanillaError("OpenCode prompt admission response has invalid fields")
    admitted_seq = admitted["admittedSeq"]
    promoted_seq = admitted.get("promotedSeq")
    time_created = admitted["timeCreated"]
    if (
        type(admitted_seq) is not int
        or admitted_seq < 0
        or promoted_seq is not None
        and (type(promoted_seq) is not int or promoted_seq < 0)
        or isinstance(time_created, bool)
        or not isinstance(time_created, (int, float))
        or not math.isfinite(time_created)
        or time_created < 0
        or admitted["id"] != request.message_id
        or admitted["sessionID"] != request.session_id
        or admitted["prompt"] != request.api_payload(resume=True)["prompt"]
        or admitted["delivery"] != request.delivery
    ):
        raise VanillaError("OpenCode prompt admission response conflicts with persisted request")
