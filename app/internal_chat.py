"""M0.3-01 내부 Skill 입력 검증 API.

이 모듈은 검증 챗봇의 입력 경계만 제공한다. 실제 Workflow 호출, 장기
Task 조회, Session/OIDC 인증은 후속 M0.3 백로그에서 이 경계에 연결한다.
"""

from __future__ import annotations

import os
import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from a2a.server.context import ServerCallContext
from a2a.auth.user import User
from a2a.types import Artifact, Message, Part, Role, Task, TaskState, TaskStatus
from google.protobuf import json_format
from jsonschema import Draft202012Validator, FormatChecker
import jwt
from jwt import PyJWKClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.a2a.runtime import approved_skill_definitions, task_store, workflow_registry
from app.workflows.registry import WorkflowRequest


INTERNAL_CHAT_ENABLED_ENV = "WORKMATE_INTERNAL_CHAT_ENABLED"
OIDC_ISSUER_ENV = "WORKMATE_OIDC_ISSUER"
OIDC_AUDIENCE_ENV = "WORKMATE_OIDC_AUDIENCE"
OIDC_JWKS_URL_ENV = "WORKMATE_OIDC_JWKS_URL"
SCHEMA_ROOT_ENV = "WORKMATE_SCHEMA_ROOT"

# 19번 문서 결정 5 — 오케스트레이터의 기존 "담당자" 선택
# (`AI-agent_game_platform/frontend/src/assignee.ts`)을 재사용한 우회 인증.
# Gmail/Calendar처럼 진짜 신원이 필요한 라우터(`proposal_api.py`,
# `dev_gmail_sync_api.py`, `dev_calendar_sync_api.py`, `google_oauth_web.py`)는
# 여기 관여하지 않고 `_authenticated_user`를 그대로 쓴다.
ASSIGNEE_HEADER = "x-workmate-assignee"
ASSIGNEE_TO_USER_ID: dict[str, str] = {
    # 지금까지 실사용해온 실제 Google sub 그대로 — Task·Meeting 데이터 연속성 유지.
    "서선정": "10464531542706509691",
    "배동우": "dev-assignee-video",
    "이승현": "dev-assignee-develop",
    "변해훈": "dev-assignee-gameqna",
}


def _enabled() -> bool:
    """내부 검증 API 활성화 여부를 환경변수에서 읽는다."""

    return os.getenv(INTERNAL_CHAT_ENABLED_ENV, "").lower() == "true"


def _schema_validator() -> Draft202012Validator:
    """승인된 Workmate Skill Schema를 로드한다."""

    configured = os.getenv(SCHEMA_ROOT_ENV)
    root = Path(configured) if configured else Path(__file__).resolve().parents[2] / "docs" / "schemas"
    schema_path = root / "workmate-skill-schemas.schema.json"
    if not schema_path.is_file():
        raise HTTPException(status_code=503, detail="approved skill schema is unavailable")
    return Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )


@lru_cache(maxsize=8)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    """`jwks_url`별로 하나의 Client를 재사용한다.

    예전엔 요청마다 `PyJWKClient(jwks_url)`을 새로 만들었다 — PyJWT의 내장 Key 캐시는
    Client 인스턴스에 딸려 있어서, 매번 새 인스턴스를 만들면 그 캐시가 전혀 쓰이지
    못하고 인증되는 모든 요청마다 Google에 실제 네트워크 호출이 나갔다(2026-08-16,
    14번 갭 문서). Client를 재사용하면 PyJWT가 알아서 Key를 캐시해 대부분의 요청은
    네트워크를 타지 않는다.
    """

    return PyJWKClient(jwks_url)


def _authenticated_user(request: Request) -> str:
    """OIDC JWT의 `sub`를 내부 API의 사용자 범위로 반환한다."""

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="OIDC bearer token is required")
    issuer = os.getenv(OIDC_ISSUER_ENV, "")
    audience = os.getenv(OIDC_AUDIENCE_ENV, "")
    jwks_url = os.getenv(OIDC_JWKS_URL_ENV, "")
    if not issuer or not audience or not jwks_url:
        raise HTTPException(status_code=503, detail="OIDC validation is not configured")
    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid OIDC bearer token") from exc
    except (URLError, OSError, TimeoutError) as exc:
        # JWKS Key를 가져오려는 실제 네트워크 호출이 실패하면(DNS·연결 재설정 등)
        # `jwt.PyJWTError`가 아닌 원본 네트워크 예외가 그대로 올라온다. 여기서 잡지
        # 않으면 이 미들웨어 체인(`verify_a2a_headers`가 CORSMiddleware 바깥에
        # 있음)에서는 CORS 헤더 없는 500이 되어 브라우저에서 `Failed to fetch`만
        # 보인다 — 2026-08-16 실사용 중 재현(오늘 브리핑이 동시에 보낸 두 요청이
        # 같은 순간 이 오류를 맞아 화면이 "브리핑을 아직 실행하지 않았습니다"에
        # 멈춰 있었다). 재시도하면 대개 해결되는 일시적 문제이므로 503으로 변환한다.
        raise HTTPException(status_code=503, detail="OIDC key verification is temporarily unavailable") from exc
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="OIDC subject is missing")
    return user_id


def _authenticated_user_or_assignee(request: Request) -> str:
    """Gmail/Calendar와 무관한 라우터(Task·Meeting·스킬챗)의 인증 경계다.

    `Authorization` 헤더가 있으면 `_authenticated_user`로 그대로(항상) 검증한다
    — Google GIS 자동 재발급 등으로 실제 OIDC 토큰이 오면 그 결과를 그대로
    쓰고, 토큰이 있는데 무효하면 조용히 담당자 헤더로 넘어가지 않고 401을
    낸다(그렇지 않으면 아무 토큰이나 넣고 담당자 이름을 사칭할 수 있다).
    `Authorization`이 아예 없을 때만 `X-Workmate-Assignee` 헤더를 오케스트레이터의
    "담당자" 선택으로 신뢰해 고정 `user_id`로 매핑한다(19번 문서 결정 5).
    """

    if request.headers.get("authorization"):
        return _authenticated_user(request)
    raw_assignee = request.headers.get(ASSIGNEE_HEADER, "").strip()
    if not raw_assignee:
        raise HTTPException(status_code=401, detail="OIDC bearer token or assignee header is required")
    # HTTP 헤더 값은 ASCII 범위 밖 문자를 그대로 담지 못한다(httpx·브라우저 fetch
    # 둘 다 한글을 그대로 보내면 인코딩 예외를 낸다) — 오케스트레이터 프론트는
    # `encodeURIComponent(name)`으로 percent-encode해 보내야 한다.
    try:
        assignee = unquote(raw_assignee, errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="assignee header is not valid percent-encoded UTF-8") from exc
    user_id = ASSIGNEE_TO_USER_ID.get(assignee)
    if user_id is None:
        raise HTTPException(status_code=400, detail="unknown assignee")
    return user_id


class _InternalUser(User):
    """내부 API에서 영속 Task Store의 소유자 범위를 표현하는 사용자다."""

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    @property
    def is_authenticated(self) -> bool:
        """인증된 내부 요청인지 반환한다."""

        return True

    @property
    def user_name(self) -> str:
        """Task Store가 사용할 사용자 식별자를 반환한다."""

        return self._user_id


class SkillChatMessageRequest(BaseModel):
    """선택한 Skill에 전달할 자연어 또는 JSON 입력이다."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=100)
    input: str | dict[str, Any]

    @model_validator(mode="after")
    def validate_input(self) -> "SkillChatMessageRequest":
        """빈 입력과 JSON 내부의 다른 Skill 선택을 거부한다."""

        if isinstance(self.input, str) and not self.input.strip():
            raise ValueError("input text must not be blank")
        if isinstance(self.input, dict):
            if not self.input:
                raise ValueError("input JSON must not be empty")
            embedded_skill = self.input.get("skill_id")
            if embedded_skill is not None and embedded_skill != self.skill_id:
                raise ValueError("input.skill_id must match skill_id")
        return self


class SkillChatMessageResponse(BaseModel):
    """입력 검증 결과를 반환한다.

    실제 업무 Artifact의 상세 Schema와 장기 Task 상태는 후속 백로그에서
    보강하며, 이번 응답은 공유 Workflow의 실행 결과를 담는다.
    """

    skill_id: str
    input_type: str
    input: str | dict[str, Any]
    validation: str = "valid"
    task_id: str
    state: str
    artifact: dict[str, Any]
    warnings: list[dict[str, Any]] = Field(default_factory=list)


_TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


_ASYNC_SKILLS = {"analyze_meeting", "assistant_ask"}
"""장시간 실행되는 Skill만 Task Polling 경로를 탄다(2026-08-16, 17번 갭 문서 #7).
`analyze_meeting`은 STT+LLM 분석을 순서대로 처리해 회의가 길면 오래 걸리지만,
나머지 4개 Skill(`daily_briefing`·`weekly_report`·`rank_priorities`·
`search_meetings`)은 실측상 짧게 끝나 굳이 Polling으로 바꿀 이유가 없다 —
15번 문서도 이 Skill만 "장기 Task"로 분류한다(02 §2.1).

`assistant_ask`(2026-08-17, 자연어 Router)도 여기 넣었다 — LLM 호출을 최소
두 번(Routing·답변 생성) 하고, 고른 Skill이 `analyze_meeting`이면 그
STT+LLM까지 이어서 실행해 소요 시간을 예측할 수 없다. 매번 어떤 Skill로
풀릴지 호출 전에는 모르므로 동기/비동기를 나눠 처리하지 않고 항상
Polling 경로를 태운다 — 대신 결과 Artifact의 `text`에 사람이 읽을 수 있는
답변 Envelope(JSON)을 그대로 담아, Task Store가 `data`·`markdown`을
저장하지 않는 문제를 피한다(`_persist_task` 참고)."""


async def _persist_task(
    task_id: str,
    request: SkillChatMessageRequest,
    result: Any,
    user_id: str,
) -> Task:
    """완료된 Workflow 결과를 A2A Task Store에 Snapshot으로 저장한다."""

    task = Task(
        id=task_id,
        context_id=task_id,
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    artifact = Artifact(
        artifact_id=f"artifact-{task_id}",
        name=result.artifact_name,
        description=result.artifact_description,
        # `warnings`도 여기 함께 싣는다 — Task Store Snapshot(Polling으로 조회하는
        # 값)엔 원래 Artifact만 남고 동기 응답의 `warnings` 필드는 저장되지
        # 않았다. 동기 Skill은 어차피 같은 요청의 응답 바디로 바로 받으니
        # 상관없었지만, `_ASYNC_SKILLS`(2026-08-16, 17번 갭 문서 #7)는 결과를
        # Polling으로만 받으므로 여기 없으면 부분 실패 경고가 조용히 사라진다.
        metadata={"mock": result.mock, "business_result": not result.mock, "warnings": result.warnings},
        parts=[Part(text=result.text, media_type="text/plain")],
    )
    task.artifacts.append(artifact)
    context = ServerCallContext(user=_InternalUser(user_id))
    await task_store().save(task, context)
    await task_store().record_message(
        message_id=f"internal-{task_id}",
        task_id=task_id,
        payload={"skill_id": request.skill_id, "input": request.input},
        response=task,
    )
    await task_store().save_artifact(task_id, artifact)
    return task


async def _save_task_status(
    task_id: str,
    state: TaskState,
    user_id: str,
    *,
    message_text: str | None = None,
) -> Task:
    """Artifact 없이 Task 상태 전이만 저장한다(`SUBMITTED`→`WORKING`, 실패 메시지 부착용).

    `_persist_task`와 달리 `record_message`를 호출하지 않는다 — 그건 같은
    `message_id`를 두 번 쓰면 `ValueError`를 내는 멱등성 Key라 최종 결과 저장
    시점에 딱 한 번만 기록해야 한다(2026-08-16, 17번 갭 문서 #7)."""

    status = TaskStatus(state=state)
    if message_text:
        status.message.CopyFrom(
            Message(
                message_id=f"internal-{task_id}-status",
                role=Role.ROLE_AGENT,
                parts=[Part(text=message_text)],
            )
        )
    task = Task(id=task_id, context_id=task_id, status=status)
    await task_store().save(task, ServerCallContext(user=_InternalUser(user_id)))
    return task


async def _run_skill_in_background(
    task_id: str,
    request: SkillChatMessageRequest,
    payload: dict[str, Any],
    user_id: str,
) -> None:
    """`_ASYNC_SKILLS`의 Skill을 HTTP 응답과 분리해 실행하고 최종 상태를 Task에 반영한다.

    동기 경로는 `ValueError`/`RuntimeError`를 422/503으로 바로 돌려주지만,
    여긴 이미 요청이 끝난 뒤라 그럴 수 없다 — 실패 원인을 `FAILED` Task의
    `status.message`에 담아 Polling(`GET .../tasks/{task_id}`)으로 드러낸다
    (2026-08-16, 17번 갭 문서 #7)."""

    await _save_task_status(task_id, TaskState.TASK_STATE_WORKING, user_id)
    try:
        result = await workflow_registry().execute(
            WorkflowRequest(
                skill_id=request.skill_id,
                task_id=task_id,
                thread_id=task_id,
                message_id=f"internal-{task_id}",
                user_id=str(payload.get("user_id", "internal-chat")),
                payload=payload,
            )
        )
    except (ValueError, RuntimeError) as exc:
        await _save_task_status(task_id, TaskState.TASK_STATE_FAILED, user_id, message_text=str(exc))
        return
    except Exception as exc:  # pragma: no cover - Task를 WORKING에 영원히 묶어두지 않기 위한 방어
        await _save_task_status(task_id, TaskState.TASK_STATE_FAILED, user_id, message_text=f"unexpected error: {exc}")
        return
    await _persist_task(task_id, request, result, user_id)


router = APIRouter(prefix="/api/v1/internal/skill-chat", tags=["internal-skill-chat"])


@router.get("/skills")
def list_skills(user_id: str = Depends(_authenticated_user_or_assignee)) -> list[dict[str, str]]:
    """Agent Card와 동일한 승인 Skill 11개를 반환한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    return list(approved_skill_definitions())


@router.post("/messages", response_model=SkillChatMessageResponse)
async def validate_message(
    request: SkillChatMessageRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(_authenticated_user_or_assignee),
) -> SkillChatMessageResponse:
    """입력을 검증하고 A2A와 공유하는 Workflow Registry를 호출한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    known_skills = {skill["id"] for skill in approved_skill_definitions()}
    if request.skill_id not in known_skills:
        raise HTTPException(status_code=422, detail="unknown skill_id")
    input_type = "natural_language" if isinstance(request.input, str) else "json"
    payload = {"text": request.input} if isinstance(request.input, str) else request.input
    if isinstance(payload, dict):
        payload_user_id = payload.get("user_id")
        if payload_user_id is not None and payload_user_id != user_id:
            raise HTTPException(status_code=403, detail="user_id does not match OIDC subject")
        payload = {**payload, "user_id": user_id}
        if input_type == "json":
            errors = sorted(_schema_validator().iter_errors(payload), key=lambda error: error.path)
            if errors:
                raise HTTPException(status_code=422, detail="input does not match approved skill schema")
    task_id = str(uuid4())
    if request.skill_id in _ASYNC_SKILLS:
        # 요청을 여기서 끝내고 STT+LLM 분석은 백그라운드로 넘긴다 — 클라이언트는
        # `state="submitted"`를 받고 `GET .../tasks/{task_id}`로 완료를
        # 기다린다(2026-08-16, 17번 갭 문서 #7). `record_message`(멱등성 Key)는
        # 아직 안 쓴다 — 최종 결과가 나올 때 `_persist_task`가 한 번만 기록한다.
        await _save_task_status(task_id, TaskState.TASK_STATE_SUBMITTED, user_id)
        background_tasks.add_task(_run_skill_in_background, task_id, request, payload, user_id)
        return SkillChatMessageResponse(
            skill_id=request.skill_id,
            input_type=input_type,
            input=request.input,
            task_id=task_id,
            state="submitted",
            artifact={"name": "", "description": "", "text": "", "data": None, "markdown": None, "mock": False, "business_result": False},
            warnings=[],
        )
    try:
        result = await workflow_registry().execute(
            WorkflowRequest(
                skill_id=request.skill_id,
                task_id=task_id,
                thread_id=task_id,
                message_id=f"internal-{task_id}",
                user_id=str(payload.get("user_id", "internal-chat")),
                payload=payload,
            )
        )
    except ValueError as exc:
        # Workflow 계층은 잘못된 입력·상태(예: "final transcript is
        # required")를 관례적으로 `ValueError`로 표시한다(app/workflows/*.py
        # 15곳). 잡지 않으면 이 미들웨어 체인(`verify_a2a_headers`가
        # CORSMiddleware 바깥에 있음)에서는 예외가 Starlette의 최상위
        # ServerErrorMiddleware까지 그대로 올라가 CORS 헤더 없는 500을
        # 반환한다 — 브라우저는 이를 읽지 못해 실제 오류 메시지 대신
        # `TypeError: Failed to fetch`만 보게 된다(2026-08-15 실사용
        # 중 발견, `analyze_meeting`에서 재현). 여기서 잡아 422로 변환해
        # 정상적인 CORS 응답 경로를 타게 한다.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Workflow 계층은 "인프라/설정 미비"(DATABASE_URL 미설정, psycopg
        # 미설치, R2 설정 누락 등)를 관례적으로 `RuntimeError`로 표시한다
        # (app/workflows/meetings.py `_search_dependencies`,
        # app/storage/r2.py, app/repositories/*.py 다수). ValueError와
        # 같은 이유로 여기서 잡지 않으면 CORS 헤더 없는 500이 되어
        # `Failed to fetch`만 보인다(2026-08-15 실사용 중 발견,
        # `search_meetings`에서 `DATABASE_URL` 미설정으로 재현). 이 요청
        # 자체는 유효하므로 422가 아니라, 다른 "설정 안 됨" 응답들과
        # 맞춰 503으로 변환한다(app/meeting_api.py R2, app/main.py
        # service token 참고).
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _persist_task(task_id, request, result, user_id)
    return SkillChatMessageResponse(
        skill_id=request.skill_id,
        input_type=input_type,
        input=request.input,
        task_id=task_id,
        state=result.state,
        artifact={
            "name": result.artifact_name,
            "description": result.artifact_description,
            "text": result.text,
            "data": result.data,
            "markdown": result.markdown,
            "mock": result.mock,
            "business_result": not result.mock,
        },
        warnings=result.warnings,
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """영속 Task Snapshot을 조회해 내부 검증 API의 Polling 결과로 반환한다."""

    task = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return json_format.MessageToDict(task, preserving_proto_field_name=False)


@router.post("/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """진행 중 Task의 취소를 기록하고 Terminal Task 취소는 거부한다."""

    task = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status.state in _TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="task is already terminal")
    await task_store().mark_cancel_requested(task_id)
    cancelled = await task_store().get(task_id, ServerCallContext(user=_InternalUser(user_id)))
    return json_format.MessageToDict(cancelled, preserving_proto_field_name=False)


__all__ = [
    "INTERNAL_CHAT_ENABLED_ENV",
    "SkillChatMessageRequest",
    "SkillChatMessageResponse",
    "router",
]
