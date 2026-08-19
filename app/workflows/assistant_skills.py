"""Workmate 어시스턴트(Drawer)가 쓰는 Skill Workflow.

Drawer용으로 추가했지만 `app/a2a/runtime.py`의 `_SKILLS`(Orchestrator Agent
Card)에도 등록돼 있어(2026-08-17, 15번 문서 결정), 공개 A2A로도 호출할 수
있다 — Drawer(내부 챗봇 경로)와 오케스트레이터(공개 A2A, 20번 문서)가 같은
Workflow를 공유한다.

여기 있는 함수들은 새 업무 규칙을 만들지 않는다 — 기존 REST 엔드포인트 함수
(`app/action_item_api.py`, `app/proposal_api.py`, `app/task_api.py`)를 그대로
호출하는 얇은 어댑터일 뿐이다. 화면(REST)과 Drawer·오케스트레이터(Skill)가
같은 로직·같은 Human-in-the-loop 규칙을 공유한다.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.workflows.registry import WorkflowRequest, WorkflowResult

# `app/dev_gmail_sync_api.py`와 같은 기본 경로·환경 변수를 쓴다 — 별도
# 저장소를 새로 만들지 않고 이미 `google-oauth-test`가 만들어 둔 Token을
# 그대로 재사용한다. `WORKMATE_DEV_GMAIL_SYNC_ENABLED` 뒤에 두지 않는다 —
# 그 플래그는 "제안함으로 새 메일을 발행하는 개발용 동기화"를 막는 스위치일
# 뿐, 이미 Context에 나온 메일 하나를 다시 읽어 답하는 것과는 무관하다.
_GMAIL_CLIENT_SECRET_FILE_ENV = "GOOGLE_CLIENT_SECRET_FILE"
_GMAIL_TOKEN_FILE_ENV = "GOOGLE_TOKEN_FILE"
_GMAIL_OAUTH_TEST_DIR = Path(__file__).resolve().parents[3] / "google-oauth-test"
_GMAIL_READ_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_EMAIL_BODY_CHAR_LIMIT = 4000


def _require(payload: dict[str, Any], field: str) -> Any:
    """자연어 모드로 필드가 비었을 때 어떤 Skill·필드가 문제인지 바로 알 수 있는
    오류를 낸다 — `internal_chat.py`가 `ValueError`를 422로 변환한다."""

    value = payload.get(field)
    if value in (None, ""):
        raise ValueError(f"{field} is required")
    return value


_EMAIL_SEARCH_WINDOW_DAYS = 30
_EMAIL_SEARCH_WINDOW_NOTE = f"최근 {_EMAIL_SEARCH_WINDOW_DAYS}일 내 메일만 검색했습니다."


def _sanitize_gmail_query_text(text: str) -> str:
    """자유 문장을 Gmail 검색 연산자로 잘못 해석되지 않게 다듬는다.

    Router가 사용자 문장에서 뽑은 검색어를 그대로 `q` 파라미터에 넣는데,
    `:`(예: "제목:")·`"`(짝이 안 맞는 따옴표)가 우연히 섞이면 Gmail이
    `subject:`류 연산자나 구문 검색으로 잘못 해석해 400을 낸다(2026-08-17,
    실사용 중 발견 — "Google provider 요청 실패"만 남고 원인을 알 수
    없었다. `app/providers/google.py`의 오류 메시지 보강과 별개로, 애초에
    잘못된 쿼리를 안 보내는 게 더 안전하다). 두 문자를 공백으로 바꿔 순수
    자유 텍스트 검색으로만 동작하게 한다.
    """

    return text.replace(":", " ").replace('"', " ").strip()


async def read_email_workflow(request: WorkflowRequest) -> WorkflowResult:
    """Gmail에서 메일 본문을 실시간으로 다시 가져와 요약·질의응답의 근거로 쓴다.

    메일이 처음 동기화될 때(`app/dev_gmail_sync_api.py`) 원문 본문은 할 일
    제목을 뽑는 LLM 호출에 한 번만 쓰이고 그 자리에서 버려진다 — 제안
    (Proposal)에는 짧은 제목 하나만 남는다. 그래서 "메일 내용 요약해줘"처럼
    나중에 다시 물으면 시스템에 근거로 삼을 내용이 전혀 남아있지 않아,
    Router가 제목을 그대로 되풀이하는 것 말고는 할 수 있는 게 없었다
    (2026-08-17, 실사용 중 발견). 이 Skill은 질문마다 Gmail API를 다시
    호출해 실제 본문을 가져온다 — 아무것도 저장하지 않고 그 자리에서만
    쓴다(제안 자체가 DB에 저장되지 않는다는 기존 원칙과 같다).

    인자는 둘 중 하나다:
    - `message_id`: Context의 제안(Proposal) 목록에서 이미 message_id를 아는
      경우 — 그 메일을 그대로 다시 읽는다.
    - `query`: 제안함에 없는(이미 처리됐거나 애초에 제안으로 뜬 적 없는) 메일도
      찾아 읽을 수 있게, 제목·본문 키워드로 메일함 전체를 검색한다(2026-08-17,
      사용자 요청). **검색 범위는 항상 최근 30일로 제한한다** — 메일함 전체를
      무제한 검색하면 응답이 느려지고 예상 밖의 오래된 메일을 잘못 골라올
      위험이 있다. 이 제한은 결과의 `search_window_note`에 항상 실어, 사용자가
      "그 전 메일이라 못 찾았을 수 있다"는 걸 답에서 알 수 있게 한다.
    """

    from app.providers.google import GmailAdapter, GoogleProviderError
    from app.providers.google_auth import GoogleCredentialError, build_authorized_session

    message_id = request.payload.get("message_id")
    if message_id:
        # Context.proposals의 message_id는 실제로 email 제안의 source_id다
        # (`app/dev_gmail_sync_api.py`의 `_publish_action_items_for_message`가
        # `f"{message_id}:{index}"`로 만든다 — 한 메일에서 Action Item을 여러
        # 개 뽑으면 각각 별도 제안으로 승인·무시하기 위해서다, 17번 갭 문서
        # #11). `review_proposal`은 이 전체 값이 그대로 필요하지만, Gmail
        # `messages.get`은 순수 Message ID만 받는다 — `:index` 그대로 넘기면
        # "Invalid id value" 400을 낸다(2026-08-17, 실사용 중 발견). 콜론
        # 앞부분만 잘라 쓴다.
        message_id = str(message_id).split(":", 1)[0]
    query = request.payload.get("query")
    if not message_id and not query:
        raise ValueError("message_id or query is required")

    def _credential_paths() -> tuple[Path, Path]:
        secret = Path(os.getenv(_GMAIL_CLIENT_SECRET_FILE_ENV, str(_GMAIL_OAUTH_TEST_DIR / "client_secret.json")))
        token = Path(os.getenv(_GMAIL_TOKEN_FILE_ENV, str(_GMAIL_OAUTH_TEST_DIR / "token.json")))
        return secret, token

    def _unavailable(reason: str, *, searched_by_query: bool) -> WorkflowResult:
        data = {
            "type": "email_content",
            "data": {
                "message_id": message_id,
                "available": False,
                "reason": reason,
                "search_window_note": _EMAIL_SEARCH_WINDOW_NOTE if searched_by_query else None,
            },
        }
        text = f"{reason} {_EMAIL_SEARCH_WINDOW_NOTE}" if searched_by_query else reason
        return WorkflowResult(
            artifact_name="read_email",
            artifact_description="Fetch an email's live content for a grounded answer.",
            text=text,
            data=data,
            markdown=text,
            mock=False,
        )

    searched_by_query = message_id is None
    _secret_path, token_path = _credential_paths()
    if not token_path.exists():
        return _unavailable("Gmail 인증이 안 되어 있어 메일 내용을 다시 가져올 수 없습니다.", searched_by_query=searched_by_query)

    try:
        session = build_authorized_session(token_path, _GMAIL_READ_SCOPES)
        adapter = GmailAdapter(session)
        if searched_by_query:
            gmail_query = f"{_sanitize_gmail_query_text(str(query))} newer_than:{_EMAIL_SEARCH_WINDOW_DAYS}d"
            candidates = await asyncio.to_thread(adapter.list_messages, query=gmail_query, max_results=5)
            if not candidates:
                return _unavailable(f"'{query}'와(과) 관련된 메일을 찾지 못했습니다.", searched_by_query=True)
            message_id = candidates[0].message_id
        message, body = await asyncio.to_thread(adapter.get_message_with_body, str(message_id))
    except (GoogleCredentialError, GoogleProviderError) as exc:
        return _unavailable(f"메일 내용을 가져오지 못했습니다: {exc}", searched_by_query=searched_by_query)

    excerpt = body[:_EMAIL_BODY_CHAR_LIMIT]
    subject = message.subject or "(제목 없음)"
    search_note = _EMAIL_SEARCH_WINDOW_NOTE if searched_by_query else None
    data = {
        "type": "email_content",
        "data": {
            "message_id": message.message_id,
            "subject": subject,
            "body": excerpt,
            "available": True,
            "search_window_note": search_note,
        },
    }
    footer = f"\n\n({search_note})" if search_note else ""
    return WorkflowResult(
        artifact_name="read_email",
        artifact_description="Fetch an email's live content for a grounded answer.",
        text=f"{excerpt}{footer}",
        data=data,
        markdown=f"## {subject}\n\n{excerpt}{footer}",
        mock=False,
    )


async def get_meeting_analysis_workflow(request: WorkflowRequest) -> WorkflowResult:
    """저장된 회의 분석(요약·Action Item)을 재조회한다 — `GET /api/v1/meetings/{id}/analysis`와 같은 로직.

    `analyze_meeting`을 다시 실행하지 않아 LLM을 다시 호출하지 않는다(2026-08-16,
    17번 갭 문서 #10과 같은 이유).
    """

    from app.action_item_api import get_meeting_analysis as _get_meeting_analysis

    meeting_id = str(_require(request.payload, "meeting_id"))
    data = _get_meeting_analysis(meeting_id, request.user_id)
    summary = str(data.get("summary") or "")
    action_items = list(data.get("action_items") or [])
    lines = "\n".join(f"- {item.get('title')} ({item.get('approval_status')})" for item in action_items)
    text = f"{summary}\n\nAction Item {len(action_items)}건" if summary else f"Action Item {len(action_items)}건"
    return WorkflowResult(
        artifact_name="get_meeting_analysis",
        artifact_description="Re-fetch a meeting's stored analysis without re-running analyze_meeting.",
        text=text,
        data={"type": "meeting_analysis", "data": data},
        markdown=f"## 회의 분석 결과\n\n{summary}\n\n### Action Item {len(action_items)}건\n\n{lines or '- 없음'}",
        mock=False,
    )


async def review_action_items_workflow(request: WorkflowRequest) -> WorkflowResult:
    """회의 Action Item을 배치로 승인·수정·거절한다 — `POST /api/v1/meetings/{id}/actions:review`와 같은 로직."""

    from app.action_item_api import ActionDecision, ActionsReviewRequest
    from app.action_item_api import review_actions as _review_actions

    meeting_id = str(_require(request.payload, "meeting_id"))
    raw_decisions = request.payload.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise ValueError("decisions must be a non-empty array")
    try:
        payload = ActionsReviewRequest(decisions=[ActionDecision(**item) for item in raw_decisions])
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError를 통일된 422 경로로 보낸다.
        raise ValueError(f"invalid decisions: {exc}") from exc
    result = _review_actions(meeting_id, payload, request.user_id)
    results = list(result.get("results") or [])
    lines = "\n".join(f"- {item['action_item_id']}: {item['decision']} → {item['approval_status']}" for item in results)
    return WorkflowResult(
        artifact_name="review_action_items",
        artifact_description="Batch approve, edit, or reject meeting action item candidates.",
        text=f"{len(results)}건 처리됨\n{lines}",
        data={"type": "action_items_review", "data": result},
        markdown=f"## Action Item 검토 결과\n\n{lines or '- 처리된 항목 없음'}",
        mock=False,
    )


async def review_proposal_workflow(request: WorkflowRequest) -> WorkflowResult:
    """이메일·Calendar 제안 하나를 승인·무시한다 — `POST /api/v1/{email,calendar}-task-proposals:review`와 같은 로직.

    두 REST 엔드포인트가 공유하는 내부 함수 `_review()`를 그대로 호출해, 화면
    (`Proposals.tsx`)과 똑같은 유사 Task 검사·멱등성 규칙을 그대로 따른다.
    """

    from app.proposal_api import ProposalTaskInput
    from app.proposal_api import _review as _review_proposal

    source_type = _require(request.payload, "source_type")
    if source_type not in ("email", "calendar"):
        raise ValueError("source_type must be email or calendar")
    decision = _require(request.payload, "decision")
    if decision not in ("approve", "ignore"):
        raise ValueError("decision must be approve or ignore")
    if source_type == "email":
        source_id = str(_require(request.payload, "message_id"))
    else:
        calendar_id = str(_require(request.payload, "calendar_id"))
        event_id = str(_require(request.payload, "event_id"))
        source_id = f"{calendar_id}:{event_id}"
    raw_task = request.payload.get("task")
    task_input: ProposalTaskInput | None = None
    if raw_task is not None:
        try:
            task_input = ProposalTaskInput(**raw_task)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid task: {exc}") from exc
    # 화면의 Idempotency-Key와 같은 역할 — 같은 Task(멱등성 단위) 안에서 재시도해도
    # 중복 생성되지 않게 Task ID에서 고정 값을 만든다.
    idempotency_key = f"assistant-{request.task_id}"
    result = _review_proposal(
        user_id=request.user_id,
        source_type=source_type,
        source_id=source_id,
        decision=decision,
        task_input=task_input,
        allow_similar_duplicate=bool(request.payload.get("allow_similar_duplicate", False)),
        idempotency_key=idempotency_key,
    )
    if result["decision"] == "ignore":
        summary = "무시했습니다."
    else:
        task = result.get("task") or {}
        summary = f"Task {'생성' if result.get('created') else '재사용'}: {task.get('task_id', '')}"
    return WorkflowResult(
        artifact_name="review_proposal",
        artifact_description="Approve or ignore a single email/calendar task proposal.",
        text=summary,
        data={"type": "proposal_review", "data": result},
        markdown=f"## 제안 검토 결과\n\n{summary}",
        mock=False,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("date fields must be ISO 8601 strings")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date: {value}") from exc


async def manage_tasks_workflow(request: WorkflowRequest) -> WorkflowResult:
    """Task를 조회·생성·수정·삭제한다 — `app/task_api.py`의 REST 함수를 그대로 호출한다.

    `action` 필드로 네 동작을 하나의 Skill에 묶었다 — `list`/`create`/`update`/`delete`.
    담당자는 항상 요청 사용자 본인 고정이라(15번 문서 📋 할 일 관리) 담당자를 입력받지 않는다.
    """

    from app.task_api import TaskCreateRequest, TaskUpdateRequest
    from app.task_api import create_task as _create_task
    from app.task_api import delete_task as _delete_task
    from app.task_api import list_tasks as _list_tasks
    from app.task_api import update_task as _update_task

    action = _require(request.payload, "action")

    if action == "list":
        status_filter = request.payload.get("status")
        due_before = _parse_datetime(request.payload.get("due_before"))
        include_deleted = bool(request.payload.get("include_deleted", False))
        tasks = _list_tasks(status_filter=status_filter, due_before=due_before, include_deleted=include_deleted, user_id=request.user_id)
        items = [task.model_dump(mode="json") for task in tasks]
        lines = "\n".join(f"- {item['title']} ({item['status']})" for item in items[:20])
        return WorkflowResult(
            artifact_name="manage_tasks",
            artifact_description="List the caller's tasks.",
            text=f"할 일 {len(items)}건",
            data={"type": "task_list", "data": {"tasks": items}},
            markdown=f"## 할 일 {len(items)}건\n\n{lines or '- 없음'}",
            mock=False,
        )

    if action == "create":
        try:
            payload = TaskCreateRequest(
                title=str(_require(request.payload, "title")),
                due_at=_parse_datetime(request.payload.get("due_at")),
                priority_hint=request.payload.get("priority_hint"),
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid task: {exc}") from exc
        task = _create_task(payload, request.user_id)
        item = task.model_dump(mode="json")
        return WorkflowResult(
            artifact_name="manage_tasks",
            artifact_description="Create a manual task.",
            text=f"할 일 등록: {item['title']}",
            data={"type": "task", "data": item},
            markdown=f"## 할 일 등록됨\n\n- {item['title']}",
            mock=False,
        )

    if action == "update":
        task_id = str(_require(request.payload, "task_id"))
        try:
            payload = TaskUpdateRequest(
                title=request.payload.get("title"),
                status=request.payload.get("status"),
                priority_hint=request.payload.get("priority_hint"),
                due_at=_parse_datetime(request.payload.get("due_at")),
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid task: {exc}") from exc
        task = _update_task(task_id, payload, request.user_id)
        item = task.model_dump(mode="json")
        return WorkflowResult(
            artifact_name="manage_tasks",
            artifact_description="Update a task.",
            text=f"할 일 수정됨: {item['title']}",
            data={"type": "task", "data": item},
            markdown=f"## 할 일 수정됨\n\n- {item['title']}",
            mock=False,
        )

    if action == "delete":
        task_id = str(_require(request.payload, "task_id"))
        _delete_task(task_id, request.user_id)
        return WorkflowResult(
            artifact_name="manage_tasks",
            artifact_description="Soft-delete a task.",
            text=f"할 일 삭제됨: {task_id}",
            data={"type": "task_deleted", "data": {"task_id": task_id}},
            markdown=f"## 할 일 삭제됨\n\n- `{task_id}`",
            mock=False,
        )

    raise ValueError(f"unsupported action: {action}")


__all__ = [
    "get_meeting_analysis_workflow",
    "manage_tasks_workflow",
    "read_email_workflow",
    "review_action_items_workflow",
    "review_proposal_workflow",
]
