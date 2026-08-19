"""메일에서 LLM으로 할 일(Task) 후보를 추출하고 업무 관련성을 판단하는 경계.

메일 한 통을 통째로 제안 하나로 만들지 않는다. 실제 OpenAI 호환 LLM에
구조화 추출을 요청해 "할 일로 등록할 가치가 있는 항목"만 0개 이상 뽑는다.
`app/meeting_analysis.py`(회의 Action Item 추출)와 같은 방식(Schema 강제
JSON 응답, 모델명 정규화)을 따른다.

`classify_work_related_emails`는 제안함(`extract_action_items`)과 별개로,
오늘 브리핑의 "중요 업무 신호"에 올릴 메일 후보가 광고·뉴스레터·소셜 알림처럼
업무와 무관한지 제목·Snippet만으로 일괄 판단한다(2026-08-17, 사용자 요청).
"""

from __future__ import annotations

import json
import os
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EmailActionItem(BaseModel):
    """메일에서 추출된 할 일 후보 하나."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)


class EmailActionItemsResult(BaseModel):
    """한 메일에서 나온 할 일 후보 전체(0개 이상)."""

    model_config = ConfigDict(extra="forbid")
    action_items: list[EmailActionItem]


class EmailActionItemProviderError(RuntimeError):
    """LLM Provider 호출 또는 응답 검증 실패."""


_SYSTEM_PROMPT = (
    "너는 업무 메일을 검토해 실제로 할 일(Task)로 등록할 가치가 있는 항목만 "
    "추려내는 보조자다. 다음은 절대 할 일로 만들지 마라: 단순 정보 공유·공지·"
    "광고·뉴스레터, 전달(FYI)만 목적인 메일, 배달 실패·시스템 알림, 이미 "
    "완료된 일을 보고하는 내용. 발신자가 명시적으로 요청·확인·승인·검토를 "
    "요구하거나 마감이 있는 항목만 할 일로 인정한다. 근거가 없으면 절대 "
    "지어내지 말고 action_items를 빈 배열로 반환하라. 제목은 메일 언어를 "
    "그대로 따르고, 무엇을 해야 하는지 한 문장으로 명확히 적어라."
)


def extract_action_items(
    subject: str,
    body: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> list[EmailActionItem]:
    """메일 제목·본문에서 할 일 후보를 추출한다. 없으면 빈 목록을 반환한다."""

    if not subject.strip() and not body.strip():
        return []
    key = api_key or os.getenv("OPENAI_API_KEY")
    url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    configured_model = model or os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini")
    chosen_model = "gpt-4.1-mini" if configured_model == "openai/gpt-4.1-mini" else configured_model
    if not key:
        raise EmailActionItemProviderError("OPENAI_API_KEY is required")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["action_items"],
        "properties": {
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "reason"],
                    "properties": {"title": {"type": "string"}, "reason": {"type": "string"}},
                },
            }
        },
    }
    payload = {
        "model": chosen_model,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {"name": "email_action_items", "strict": True, "schema": schema}},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"제목: {subject}\n\n본문:\n{body[:8000]}"},
        ],
    }
    req = request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            body_response = json.loads(response.read().decode())
    except error.HTTPError as exc:
        raise EmailActionItemProviderError(f"LLM Provider returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EmailActionItemProviderError("LLM Provider request failed") from exc
    try:
        content = body_response["choices"][0]["message"]["content"]
        result = EmailActionItemsResult.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise EmailActionItemProviderError("LLM response does not match EmailActionItemsResult") from exc
    return result.action_items


class EmailRelevanceJudgment(BaseModel):
    """메일 하나가 업무 관련인지에 대한 판단."""

    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1)
    is_work_related: bool


class EmailRelevanceJudgmentsResult(BaseModel):
    """일괄 판단 요청에 대한 메일별 판단 전체."""

    model_config = ConfigDict(extra="forbid")
    judgments: list[EmailRelevanceJudgment]


class EmailRelevanceProviderError(RuntimeError):
    """LLM Provider 호출 또는 응답 검증 실패."""


_RELEVANCE_SYSTEM_PROMPT = (
    "너는 여러 메일의 제목·미리보기만 보고 각각이 업무와 관련 있는지 "
    "판단하는 보조자다. 회사·프로젝트 업무 요청·보고·회의·검토·승인처럼 "
    "실제 업무와 관련된 메일만 업무 관련(true)으로 판단하라. 광고·마케팅·"
    "뉴스레터·소셜 알림·구독 안내·개인 쇼핑/배송 알림처럼 업무와 무관한 "
    "메일은 false로 판단하라. 내용이 짧거나 애매해 업무 관련 여부를 확신할 "
    "수 없으면 업무 신호를 놓치지 않도록 true로 판단하라. 입력으로 받은 "
    "모든 message_id에 대해 반드시 하나씩 판단을 반환하라."
)


def classify_work_related_emails(
    messages: list[tuple[str, str]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, bool]:
    """메일 후보들이 업무 관련인지 제목·Snippet만으로 한 번에 판단한다.

    `extract_action_items`처럼 메일마다 본문을 다시 읽지 않는다 — 오늘
    브리핑은 이미 `list_messages`+`get_message`로 얻은 제목·Snippet만
    있고, 신호 몇 건을 개별 LLM 호출로 판단하면 왕복이 그만큼 늘어 느려진다.
    한 번의 호출로 전부 판단해 광고·뉴스레터·소셜 알림처럼 업무와 무관한
    메일을 "중요 업무 신호"에서 제외할 수 있게 한다(2026-08-17, 사용자
    요청 — 제안함은 `extract_action_items`가 이미 실제 할 일이 아니면
    걸러내지만, 오늘 브리핑은 필터링 없이 Gmail이 준 메일을 그대로
    보여주고 있었다).

    반환값에 없는 `message_id`는 호출자가 "업무 관련(True)"으로 취급해야
    한다 — 판단을 놓친 메일까지 자동으로 숨기지 않기 위해서다.
    """

    ids_and_text = [(message_id, text) for message_id, text in messages if message_id]
    if not ids_and_text:
        return {}
    key = api_key or os.getenv("OPENAI_API_KEY")
    url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    configured_model = model or os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini")
    chosen_model = "gpt-4.1-mini" if configured_model == "openai/gpt-4.1-mini" else configured_model
    if not key:
        raise EmailRelevanceProviderError("OPENAI_API_KEY is required")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["judgments"],
        "properties": {
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["message_id", "is_work_related"],
                    "properties": {
                        "message_id": {"type": "string"},
                        "is_work_related": {"type": "boolean"},
                    },
                },
            }
        },
    }
    listing = "\n\n".join(f"- message_id: {message_id}\n  내용: {text[:500] or '(내용 없음)'}" for message_id, text in ids_and_text)
    payload = {
        "model": chosen_model,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "email_relevance_judgments", "strict": True, "schema": schema},
        },
        "messages": [
            {"role": "system", "content": _RELEVANCE_SYSTEM_PROMPT},
            {"role": "user", "content": listing},
        ],
    }
    req = request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            body_response = json.loads(response.read().decode())
    except error.HTTPError as exc:
        raise EmailRelevanceProviderError(f"LLM Provider returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EmailRelevanceProviderError("LLM Provider request failed") from exc
    try:
        content = body_response["choices"][0]["message"]["content"]
        result = EmailRelevanceJudgmentsResult.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise EmailRelevanceProviderError("LLM response does not match EmailRelevanceJudgmentsResult") from exc
    return {judgment.message_id: judgment.is_work_related for judgment in result.judgments}
