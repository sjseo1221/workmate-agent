"""회의 Transcript를 LLM으로 요약하고 Action Item을 검증하는 경계."""

from __future__ import annotations

import json
import os
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ActionItem(BaseModel):
    """회의에서 추출된 실행 항목과 원문 근거."""
    model_config = ConfigDict(extra="forbid")
    action_item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    assignee_id: str | None = None
    due_at: str | None = None
    evidence_text: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class MeetingAnalysis(BaseModel):
    """요약과 근거를 포함한 회의 분석 결과."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=60)
    """회의 내용을 요약한 짧은 제목 — 녹음 시점엔 아직 내용이 없어 "회의 M월 D일
    HH:MM"처럼 날짜·시각만으로 자동 생성된 제목을 갖는다(`Meeting.tsx`의
    `defaultMeetingTitle()`). 그 자리표시자를 대체할 실제 제목이 필요해
    분석 결과에 함께 요청한다(2026-08-17, 사용자 요청 — 회의 목록에 자동
    생성 제목을 보여달라는 요청). 사용자가 직접 지은 제목은 덮어쓰지 않는다
    (`app/workflows/meetings.py`가 자리표시자 패턴일 때만 적용한다)."""
    summary: str = Field(min_length=1)
    action_items: list[ActionItem]


class MeetingAnalysisProviderError(RuntimeError):
    """LLM Provider 호출 또는 응답 검증 실패."""


def analyze_transcript(transcript: str, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> MeetingAnalysis:
    """실제 OpenAI 호환 LLM에 구조화 분석을 요청하고 Transcript 근거를 검증한다."""
    if not transcript.strip():
        raise ValueError("transcript must not be empty")
    key = api_key or os.getenv("OPENAI_API_KEY")
    url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    configured_model = model or os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini")
    chosen_model = "gpt-4.1-mini" if configured_model == "openai/gpt-4.1-mini" else configured_model
    if not key:
        raise MeetingAnalysisProviderError("OPENAI_API_KEY is required")
    schema = {"type": "object", "additionalProperties": False, "required": ["title", "summary", "action_items"], "properties": {"title": {"type": "string"}, "summary": {"type": "string"}, "action_items": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["action_item_id", "title", "assignee_id", "due_at", "evidence_text", "start_ms", "end_ms"], "properties": {"action_item_id": {"type": "string"}, "title": {"type": "string"}, "assignee_id": {"type": ["string", "null"]}, "due_at": {"type": ["string", "null"]}, "evidence_text": {"type": "string"}, "start_ms": {"type": "integer", "minimum": 0}, "end_ms": {"type": "integer", "minimum": 0}}}}}}
    payload = {"model": chosen_model, "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"name": "meeting_analysis", "strict": True, "schema": schema}}, "messages": [{"role": "system", "content": "Return only the requested JSON schema. `title` is a short (under 30 Korean characters) descriptive meeting title in Korean, summarizing what the meeting was about — not a generic phrase like '회의' alone. Every evidence_text MUST be an exact contiguous substring copied from the transcript; never invent or translate evidence. Use empty action_items when no explicit action exists."}, {"role": "user", "content": transcript}]}
    req = request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=30) as response:
            body = json.loads(response.read().decode())
    except error.HTTPError as exc:
        raise MeetingAnalysisProviderError(f"LLM Provider returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MeetingAnalysisProviderError("LLM Provider request failed") from exc
    try:
        content = body["choices"][0]["message"]["content"]
        result = MeetingAnalysis.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise MeetingAnalysisProviderError("LLM response does not match MeetingAnalysis") from exc
    for item in result.action_items:
        if item.evidence_text not in transcript or item.end_ms < item.start_ms:
            raise MeetingAnalysisProviderError("action item evidence is invalid")
    return result
