"""Gmail·Google Calendar HTTP+JSON Adapter.

`AuthorizedSession`은 OAuth 자격 증명을 외부에서 주입받는다. 이 모듈은
Client Secret·Refresh Token을 읽거나 저장하지 않으며, 원문을 영속화하지 않는다.
`get_message_with_body`가 읽는 본문도 LLM Action Item 추출 호출 동안만
메모리에 머물고, 결과(Action Item 제목·근거)만 제안으로 남는다.
"""

from __future__ import annotations

import base64
import html as html_module
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


class GoogleProviderError(RuntimeError):
    """Google API 오류를 상태 코드와 재시도 가능 여부와 함께 전달한다."""

    def __init__(self, message: str, *, status_code: int, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class SyncCursorExpiredError(GoogleProviderError):
    """Gmail History 또는 Calendar syncToken이 만료된 경우다."""

    def __init__(self, message: str = "Google sync cursor expired") -> None:
        super().__init__(message, status_code=410)


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """Gmail 목록 응답에서 업무 제안에 필요한 정규화 필드.

    `subject`는 `messages.list`에는 없고 `messages.get`(metadataHeaders)에만
    있다. `list_messages`가 만드는 항목은 `subject=None`이며, 개별 메시지를
    `get_message`로 조회했을 때만 채워진다. `received_at`도 마찬가지다 —
    Gmail의 `internalDate`(수신 시각, epoch 밀리초)에서 파싱하며,
    `list_messages`는 채우지 않는다(2026-08-17, 제안함 카드에 수신 날짜를
    보여주고 정렬하라는 요청으로 추가).
    """

    message_id: str
    thread_id: str | None
    snippet: str | None
    label_ids: tuple[str, ...]
    subject: str | None = None
    received_at: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Calendar Event에서 업무 제안에 필요한 정규화 필드."""

    calendar_id: str
    event_id: str
    summary: str
    status: str | None
    start: str | None
    end: str | None
    html_link: str | None

    @property
    def source_id(self) -> str:
        """Task 원장 중복 검사용 Calendar 원본 식별자."""

        return f"{self.calendar_id}:{self.event_id}"


def _rfc3339(value: str | None) -> str | None:
    """Provider 날짜 문자열을 검증 가능한 ISO 문자열로 유지한다."""

    if value is None:
        return None
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _decode_body_data(data: str) -> str:
    """Gmail의 URL-safe Base64 본문을 텍스트로 디코딩한다."""

    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _find_body_by_mime_type(part: dict[str, Any], mime_type: str) -> str | None:
    """MIME Part 트리를 재귀 탐색해 지정한 MIME Type의 본문을 찾는다."""

    if part.get("mimeType") == mime_type:
        data = (part.get("body") or {}).get("data")
        if data:
            return _decode_body_data(data)
    for child in part.get("parts") or []:
        found = _find_body_by_mime_type(child, mime_type)
        if found is not None:
            return found
    return None


def _parse_internal_date(body: dict[str, Any]) -> str | None:
    """Gmail 메시지 응답의 `internalDate`(수신 시각, epoch 밀리초 문자열)를
    ISO 8601(UTC)로 변환한다. 값이 없거나 파싱할 수 없으면 `None`을 반환한다."""

    raw = body.get("internalDate")
    if not raw:
        return None
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def _strip_html_tags(html_text: str) -> str:
    """`text/plain` Part가 없을 때만 쓰는 최소 HTML → 평문 변환.

    완전한 HTML Parser가 아니다 — LLM 입력용으로 태그·Script·Style을
    제거하고 HTML Entity만 정확히 복원하면 충분하다.
    """

    without_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    unescaped = html_module.unescape(without_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


class _GoogleRestAdapter:
    """AuthorizedSession을 사용해 Google REST 응답을 공통 처리한다."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(method, url, timeout=30, **kwargs)
        if response.status_code == 410:
            raise SyncCursorExpiredError()
        if response.status_code in {401, 403}:
            raise GoogleProviderError("Google authorization required", status_code=response.status_code)
        if response.status_code == 429 or response.status_code >= 500:
            raise GoogleProviderError("Google provider temporarily unavailable", status_code=response.status_code, retryable=True)
        if response.status_code >= 400:
            # Google이 왜 거절했는지(예: 400 잘못된 검색 쿼리 문법) 원문 오류
            # 메시지 없이는 알 수 없었다 — "Google provider request failed"만
            # 남아 재현·진단이 막혔다(2026-08-17, 실사용 중 발견). 상태 코드와
            # Google이 돌려준 오류 메시지를 그대로 붙인다.
            try:
                detail = response.json().get("error", {}).get("message")
            except ValueError:
                detail = None
            suffix = f": {detail}" if detail else f" (HTTP {response.status_code})"
            raise GoogleProviderError(f"Google provider request failed{suffix}", status_code=response.status_code)
        return response.json()


class GmailAdapter(_GoogleRestAdapter):
    """Gmail Profile·Message·History·Watch 호출과 응답 정규화를 담당한다."""

    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"

    def profile(self) -> dict[str, Any]:
        """현재 사용자 Profile을 조회한다."""

        return self._request("GET", f"{self.base_url}/profile")

    def list_messages(self, *, query: str | None = None, page_token: str | None = None, max_results: int = 100) -> list[GmailMessage]:
        """메일 ID 목록을 조회하고 업무 제안용 최소 필드로 정규화한다."""

        params: dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        body = self._request("GET", f"{self.base_url}/messages", params=params)
        return [GmailMessage(str(item["id"]), item.get("threadId"), item.get("snippet"), tuple(item.get("labelIds", []))) for item in body.get("messages", [])]

    def get_message(self, message_id: str) -> GmailMessage:
        """단일 메시지의 최소 필드(Snippet·제목 포함)를 조회한다.

        `users.messages.list`는 `id`·`threadId`만 반환하고 Snippet·제목을
        주지 않는다. History Watch로 알게 된 개별 메시지 ID를 보강할 때 쓴다.
        Gmail API는 `metadataHeaders`로 명시한 헤더만 `format=metadata`
        응답에 채워주므로 `Subject`를 지정해야 제목을 받을 수 있다.
        """

        body = self._request(
            "GET",
            f"{self.base_url}/messages/{message_id}",
            params={"format": "metadata", "metadataHeaders": "Subject"},
        )
        headers = (body.get("payload") or {}).get("headers", [])
        subject = next((str(header["value"]) for header in headers if header.get("name") == "Subject"), None)
        return GmailMessage(str(body["id"]), body.get("threadId"), body.get("snippet"), tuple(body.get("labelIds", [])), subject, _parse_internal_date(body))

    def get_message_with_body(self, message_id: str) -> tuple[GmailMessage, str]:
        """메시지 메타데이터와 LLM Action Item 추출용 평문 본문을 함께 조회한다.

        `text/plain` 파트를 우선 쓰고 없으면 `text/html`에서 태그를 제거해
        대체한다. 둘 다 없으면 Snippet으로 대체한다. 본문은 이 호출의
        반환값으로만 쓰이며 어디에도 저장하지 않는다.
        """

        body = self._request("GET", f"{self.base_url}/messages/{message_id}", params={"format": "full"})
        payload = body.get("payload") or {}
        headers = payload.get("headers", [])
        subject = next((str(header["value"]) for header in headers if header.get("name") == "Subject"), None)
        message = GmailMessage(str(body["id"]), body.get("threadId"), body.get("snippet"), tuple(body.get("labelIds", [])), subject, _parse_internal_date(body))

        plain_text = _find_body_by_mime_type(payload, "text/plain")
        if plain_text is None:
            html_text = _find_body_by_mime_type(payload, "text/html")
            plain_text = _strip_html_tags(html_text) if html_text is not None else None
        return message, (plain_text or message.snippet or "")

    def history(self, start_history_id: str, *, page_token: str | None = None) -> dict[str, Any]:
        """지정 History ID 이후 변경을 조회한다."""

        params: dict[str, Any] = {"startHistoryId": start_history_id, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", f"{self.base_url}/history", params=params)

    def watch(self, topic_name: str) -> dict[str, Any]:
        """Gmail Pub/Sub Watch를 등록한다."""

        return self._request("POST", f"{self.base_url}/watch", json={"topicName": topic_name, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"})


class GoogleCalendarAdapter(_GoogleRestAdapter):
    """Google Calendar Event·Watch 호출과 응답 정규화를 담당한다."""

    base_url = "https://www.googleapis.com/calendar/v3/calendars"

    def __init__(self, session: Any, *, calendar_id: str = "primary") -> None:
        super().__init__(session)
        self.calendar_id = calendar_id
        self.calendar_url = f"{self.base_url}/{quote(calendar_id, safe='')}"

    def list_events(self, *, sync_token: str | None = None, time_min: str | None = None, time_max: str | None = None) -> tuple[list[CalendarEvent], str | None]:
        """Calendar Event를 조회하고 다음 증분 Token과 함께 반환한다."""

        params: dict[str, Any] = {"maxResults": 2500, "singleEvents": "true", "showDeleted": "true"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            if time_min:
                params["timeMin"] = _rfc3339(time_min)
            if time_max:
                params["timeMax"] = _rfc3339(time_max)
        body = self._request("GET", f"{self.calendar_url}/events", params=params)
        events = [
            CalendarEvent(self.calendar_id, str(item["id"]), str(item.get("summary", "")), item.get("status"),
                          (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date"),
                          (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date"), item.get("htmlLink"))
            for item in body.get("items", [])
            if item.get("id")
        ]
        return events, body.get("nextSyncToken")

    def watch(self, *, channel_id: str, address: str, token: str | None = None) -> dict[str, Any]:
        """Calendar Webhook Watch를 등록한다."""

        payload: dict[str, Any] = {"id": channel_id, "type": "web_hook", "address": address}
        if token:
            payload["token"] = token
        return self._request("POST", f"{self.calendar_url}/events/watch", json=payload)
