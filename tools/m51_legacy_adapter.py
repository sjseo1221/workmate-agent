"""레거시 Orchestrator와 정식 Workmate A2A 계약 사이의 로컬 어댑터.

Workmate 자체에는 `/a2a/v1` Route를 추가하지 않는다. 이 프로세스만 레거시
경로와 응답을 변환하며, upstream은 정식 `/a2a` Agent를 가리킨다.

담당 Orchestrator의 채팅 경로는 Skill을 고르지 못하고 항상 `daily_briefing`을
보낸다. 해당 저장소를 수정할 수 없으므로, 사용자 문장을 그대로
`assistant_ask`(자연어 Router, `app/workflows/assistant_router.py`)로
넘긴다 — Skill 판별은 이 어댑터가 아니라 `assistant_ask` 내부 LLM이 한다
(2026-08-17, 20번 문서 3단계). `assistant_ask`는 이제 다른 10개 Skill과
마찬가지로 Agent Card에 있어(15번 문서 결정) 인증도 새로 만들 필요가 없다 —
오케스트레이터가 보낸 Service Bearer Token을 그대로 전달하면 된다.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, Response


UPSTREAM = os.getenv("WORKMATE_UPSTREAM_URL", "http://127.0.0.1:8001/a2a").rstrip("/")
PUBLIC_BASE = os.getenv("M51_ADAPTER_PUBLIC_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
# Orchestrator는 인증된 사용자를 전달하지 않으므로 데모 범위의 고정 사용자를 사용한다.
DEFAULT_USER_ID = os.getenv(
    "M51_ADAPTER_DEFAULT_USER_ID", "00000000-0000-0000-0000-000000000001"
)
# 오케스트레이터의 "담당자" 드롭다운(`AI-agent_game_platform/frontend/src/assignee.ts`)
# 이름 4개를 Workmate user_id로 미리 매핑해 둔다. "서선정"만 실제 Google 계정이
# 연동된 Workmate 사용자다(2026-08-18, `.runtime/tasks.sqlite3`의 `users` 테이블에
# 그 user_id 한 행만 존재함을 확인) — 나머지 3명(배동우·이승현·변해훈)은 원래
# Video/Dev/Game Q&A 담당자라 Workmate 계정 자체가 없고, 여기 적힌 값은 "서선정"의
# 실제 user_id와 자릿수·형식(숫자 21자리)만 맞춰 새로 발급한 placeholder일 뿐이라
# daily_briefing을 실행해도 실제 데이터 없이 빈 결과만 나온다.
#
# 지금은 오케스트레이터가 담당자 식별값(이름이든 user_id든)을 요청에 아예 안
# 실어 보내므로(20번 문서 "오케스트레이터 담당자에게 전달할 요청 목록" R5로
# 등록) 이 매핑을 조회할 키가 없다 — `_resolve_user_id()`는 그 요청이 반영돼
# `metadata.owner`로 이름이 오면 즉시 매핑을 쓰고, 못 찾으면 지금처럼
# `DEFAULT_USER_ID`로 폴백한다.
ASSIGNEE_USER_IDS = {
    "서선정": "10464531542706509691",
    "배동우": "267494469329567778120",
    "이승현": "568401699951365934381",
    "변해훈": "898605867716224776814",
}


def _resolve_user_id(payload: dict[str, object]) -> str:
    """요청의 `metadata.owner`(담당자 이름)를 Workmate user_id로 바꾼다.

    오케스트레이터가 아직 이 필드를 안 보내므로 대부분의 요청은 못 찾고
    `DEFAULT_USER_ID`로 폴백한다 — R5(위 `ASSIGNEE_USER_IDS` 주석 참고)가
    반영된 뒤를 대비해 미리 만들어 둔 조회 경로다.
    """

    message = payload.get("message")
    metadata = message.get("metadata") if isinstance(message, dict) else None
    owner = metadata.get("owner") if isinstance(metadata, dict) else None
    if isinstance(owner, str) and owner in ASSIGNEE_USER_IDS:
        return ASSIGNEE_USER_IDS[owner]
    return DEFAULT_USER_ID
# 오케스트레이터 채팅 경로가 보내는 모든 요청을 이 Skill 하나로 고정한다 —
# Skill 판별은 이 Skill 내부 LLM Router(`route()`)가 대신한다.
ASSISTANT_ASK_SKILL_ID = "assistant_ask"
app = FastAPI(title="Workmate legacy A2A adapter")


def _request_json(url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    """Upstream JSON을 호출하고 상태·본문·Content-Type을 반환한다."""

    request = Request(url, data=body, headers=headers or {}, method="POST" if body else "GET")
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get_content_type()
    except URLError as exc:
        raise RuntimeError("Workmate upstream is unavailable") from exc


def _legacy_card(card: dict[str, object]) -> dict[str, object]:
    """Agent Card의 HTTP+JSON endpoint만 어댑터 주소로 바꾼다."""

    transformed = dict(card)
    interfaces = []
    for interface in card.get("supportedInterfaces", []):
        item = dict(interface)
        if item.get("protocolBinding") == "HTTP+JSON" and item.get("protocolVersion") == "1.0":
            item["url"] = f"{PUBLIC_BASE}/a2a/v1/message:send"
        interfaces.append(item)
    transformed["supportedInterfaces"] = interfaces
    # 담당 Orchestrator의 구형 AgentCard 모델이 요구하는 최상위 URL을 보완한다.
    transformed["url"] = f"{PUBLIC_BASE}/a2a/v1/message:send"
    # 담당 Orchestrator가 토큰·상태 조회 키로 사용하는 registry 이름을 맞춘다.
    transformed["name"] = "workmate-agent"
    return transformed


_PENDING_ACTION_NOTICE = (
    "\n\n(이 요청은 실행 확인이 필요합니다 — Workmate 화면에서 직접 진행해 주세요.)"
)
"""오케스트레이터 채팅 UI에는 `assistant_ask`의 확인 배너(확인/취소 버튼)가
없다(15번 문서 G5) — 파괴적이거나 비용이 드는 동작(Action Item 거절, 제안
무시, 할 일 삭제, 회의 분석 등)은 `pending_action`이 있는 채로 그냥 텍스트만
돌아오면 사용자가 확인 없이 넘어갔다고 착각할 수 있다. 자동으로 실행하는
대신 이 안내를 답변에 덧붙여, Workmate 화면(Drawer 등)에서 확인하도록
유도한다(2026-08-17, 20번 문서 5단계)."""


def _legacy_response(body: dict[str, object]) -> dict[str, object]:
    """`assistant_ask` Artifact에서 실행된 Skill을 레거시 smoke-test용으로 노출하고,
    확인이 필요한 답이면 원본 답변 Part에 안내를 직접 덧붙인다.

    실제 답(자연어)은 Markdown Part에, 실행된 Skill·확인 필요 여부
    (`pending_action`)는 같은 Artifact의 JSON Data Part에 있다
    (`app/a2a/runtime.py::_artifact_parts`, `assistant_ask_workflow`가 만드는
    `{"type": "assistant_reply", "data": {...}}` 봉투). 오케스트레이터 채팅
    UI는 한 Artifact 안의 **모든** Part의 `.text`를 이어붙여 보여주므로
    (`a2a_client.py::_read_text_parts`), 새 Part에 답변을 통째로 복사해
    추가하면 같은 문장이 두 번 보인다 — 그래서 안내는 원본 답변 Part의
    `.text`에 직접 덧붙이고, 새로 추가하는 호환용 Part에는 `.text`를 두지
    않는다.

    `message:send`(`{"task": {...}}`로 감싼 `SendMessageResponse`)와
    `GET .../tasks/{id}`(Task 객체가 그대로 최상위에 오는 `GetTask` 응답) 둘 다
    받을 수 있다 — `returnImmediately=true`를 쓰면 실제 완료된 답은 거의 항상
    후자로 온다(2026-08-17, 20번 문서 G7 대응).
    """

    transformed = json.loads(json.dumps(body))
    task = transformed.get("task")
    if not isinstance(task, dict):
        # `message:send` 래핑이 아니면, 이 본문 자체가 이미 `GetTask`가 돌려준
        # bare Task일 수 있다 — `status`/`artifacts` 존재로 구분한다.
        task = transformed if isinstance(transformed.get("status"), dict) else None
    if not isinstance(task, dict):
        return transformed
    for artifact in task.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        parts = artifact.setdefault("parts", [])
        if not isinstance(parts, list):
            continue
        text_part = next((part for part in parts if isinstance(part, dict) and part.get("text")), None)
        envelope = next((part.get("data") for part in parts if isinstance(part, dict) and isinstance(part.get("data"), dict)), None)
        reply_data = envelope.get("data") if isinstance(envelope, dict) else None
        executed_skill_id = reply_data.get("executed_skill_id") if isinstance(reply_data, dict) else None
        pending_action = reply_data.get("pending_action") if isinstance(reply_data, dict) else None
        if pending_action and text_part is not None:
            text_part["text"] = f"{text_part['text']}{_PENDING_ACTION_NOTICE}"
        parts.append({
            "data": {"invoked_skill": executed_skill_id or ASSISTANT_ASK_SKILL_ID},
            "mediaType": "application/json",
        })
    return transformed


def _workmate_request(body: bytes) -> bytes:
    """오케스트레이터 요청의 text Part를 `assistant_ask` Skill 요청 data로 변환한다.

    Skill 판별용 사용자 문장 하나만 있으면 되므로, 기존에 오던 구조화 필드
    (예: `as_of`)는 더 이상 의미가 없다 — `assistant_ask`는 `text`만 보고
    스스로 어떤 Skill을 어떤 인자로 부를지 판단한다(20번 문서 "설계").
    """

    payload = json.loads(body)
    message = payload.setdefault("message", {})
    if message.get("messageId") == "main-agent":
        message["messageId"] = f"legacy-{uuid.uuid4()}"
    parts = message.setdefault("parts", [])
    text = ""
    if parts and isinstance(parts[0], dict):
        part = parts[0]
        existing = part.get("data")
        if isinstance(existing, dict):
            text = str(existing.get("message") or existing.get("input") or existing.get("text") or "")
        elif isinstance(part.get("text"), str):
            text = part.pop("text")
        part["data"] = {
            "skill_id": ASSISTANT_ASK_SKILL_ID,
            "text": text,
            "user_id": _resolve_user_id(payload),
        }
    # `assistant_ask`가 고르는 Skill(예: `analyze_meeting`)은 몇 초에서 몇 분까지
    # 걸릴 수 있다. 공개 A2A `message:send`는 기본이 Blocking이라(SDK
    # `DefaultRequestHandler.on_message_send` — Task가 끝날 때까지 응답을
    # 그대로 들고 있는다, 실측 확인함) 오래 걸리는 요청은 이 한 호출 안에서
    # 계속 기다리게 된다. `configuration.returnImmediately=true`를 실어 보내면
    # SDK가 `TASK_STATE_SUBMITTED` 상태로 즉시 돌려주고, 이미 있는
    # `GET /a2a/v1/tasks/{id}` 프록시(이 어댑터의 `task()`)로 완료를 Polling할
    # 수 있다 — 오케스트레이터의 `A2AClient.send_message()`가 Task 응답을 받으면
    # 자동으로 `poll_task()`로 넘어가므로(`a2a_client.py`) 어댑터 쪽에는 새
    # Route가 필요 없다(2026-08-17, 20번 문서 G7 — 실측으로 확인·해결).
    payload.setdefault("configuration", {})["returnImmediately"] = True
    return json.dumps(payload, ensure_ascii=False).encode()


@app.get("/.well-known/agent-card.json")
async def agent_card() -> JSONResponse:
    """레거시 Orchestrator가 접근할 수 있는 변환 Agent Card를 반환한다."""

    status, body, _ = await asyncio.to_thread(
        _request_json, f"{UPSTREAM.removesuffix('/a2a')}/.well-known/agent-card.json"
    )
    if status >= 400:
        return JSONResponse(json.loads(body), status_code=status)
    return JSONResponse(_legacy_card(json.loads(body)))


def _requirement_error(message: str) -> JSONResponse:
    """필수 입력이 없을 때 Orchestrator가 표시할 수 있는 400을 만든다."""

    return JSONResponse(
        {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}},
        status_code=400,
    )


async def _proxy(request: FastAPIRequest, suffix: str) -> Response:
    """레거시 Route를 정식 Workmate Route로 전달한다."""

    body = await request.body()
    if suffix == "message:send":
        try:
            body = _workmate_request(body)
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
            pass
        # 빈 문장으로 보내면 upstream이 `text is required` ValueError를 그대로
        # 내부 오류로 노출하므로 미리 거른다.
        if not _converted_data(body).get("text"):
            return _requirement_error("사용자 문장(text)이 없습니다.")
    headers = {
        "Authorization": request.headers.get("authorization", ""),
        "A2A-Version": "1.0",
        "Content-Type": "application/a2a+json",
        "Accept": request.headers.get("accept", "application/a2a+json"),
    }
    status, response_body, content_type = await asyncio.to_thread(
        _request_json, f"{UPSTREAM}/{suffix}", body=body, headers=headers
    )
    if content_type == "application/json":
        try:
            # `message:send`(`returnImmediately`라 보통 SUBMITTED만 옴)와 Task
            # Polling(`tasks/{id}`, 실제 완료된 Artifact가 오는 쪽) 둘 다 같은
            # 변환을 적용한다 — `_legacy_response`가 두 응답 모양을 알아서
            # 구분한다(위 Docstring).
            payload = _legacy_response(json.loads(response_body))
            return JSONResponse(payload, status_code=status)
        except json.JSONDecodeError:
            pass
    return Response(response_body, status_code=status, media_type=content_type)


def _converted_data(body: bytes) -> dict[str, object]:
    """변환된 요청 본문에서 첫 data Part를 읽는다."""

    try:
        parts = json.loads(body).get("message", {}).get("parts", [])
        data = parts[0].get("data", {}) if parts else {}
        return data if isinstance(data, dict) else {}
    except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
        return {}


@app.post("/a2a/v1/message:send")
async def message_send(request: FastAPIRequest) -> Response:
    """레거시 Message 전송을 정식 HTTP+JSON send로 변환한다."""

    return await _proxy(request, "message:send")


@app.get("/a2a/v1/tasks/{task_id}")
async def task(task_id: str, request: FastAPIRequest) -> Response:
    """Orchestrator의 장기 Task 조회를 upstream으로 전달한다."""

    return await _proxy(request, f"tasks/{task_id}")
