"""회의 Action Item 검토·승인과 Task 원장 연결 API."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.internal_chat import _authenticated_user_or_assignee
from app.meeting_api import meeting_repository
from app.domain.task import TaskRecord
from app.task_api import task_repository

router = APIRouter(prefix="/api/v1/meetings", tags=["action-items"])


class ActionItemRequest(BaseModel):
    """Action Item 후보 입력(단건 승인 전용 — 배치 검토는 `ActionsReviewRequest` 참고)."""
    title: str = Field(min_length=1, max_length=500)
    evidence_text: str = Field(min_length=1)


def _source_id(meeting_id: str, action_item_id: str) -> str:
    """Task가 근거로 삼는 원본 ID를 만든다.

    `action_item_id`만으로는 회의 간 유일성이 보장되지 않는다(LLM이
    `temperature=0`으로 스스로 매기는 값이라 비슷한 회의록에 같은 ID가 나올
    수 있음 — 2026-08-15 실사용 중 발견한 PK 충돌 버그와 같은 원인).
    `meeting_id`를 합쳐 Task 쪽에서도 유일성을 보장하고, Task 상세의
    `meeting_evidence`(#11)가 이 값에서 다시 `meeting_id`를 복원할 수 있게
    한다(2026-08-16, 17번 갭 문서 #11). Calendar 제안의 `{calendar_id}:{event_id}`와
    같은 패턴이다.
    """

    return f"{meeting_id}:{action_item_id}"


def _parse_due_at(value: object) -> datetime | None:
    """`action_items.due_at`(TEXT)를 `TaskRecord.due_at`(datetime)로 변환한다.

    `TaskRepository`는 `due_at.astimezone(...)`을 그대로 호출하므로(예:
    `app/repositories/tasks.py`) 문자열을 그대로 넘기면 저장 시점에 크래시한다.
    LLM이 만든 값이라 형식이 어긋날 수 있어, 파싱 실패는 조용히 None으로
    떨어뜨린다(due_at 미기재와 같은 취급, 2026-08-16, 17번 갭 문서 #11)."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _action_item_response(row: dict[str, object]) -> dict[str, object]:
    """DB 행을 `ActionItem` 조회값 형태로 변환한다.

    `build_analyze_meeting_workflow`가 분석 직후 돌려주는 `action_items` 딕셔너리와
    같은 키(`assignee_user_id` 등, 승인 Schema `docs/schemas/workmate-skill-schemas.schema.json`
    §ActionItem 기준)를 써야 한다 — 프론트가 두 응답을 같은 `ActionItem` 타입으로
    다룬다(2026-08-16, 17번 갭 문서 #10)."""

    return {
        "action_item_id": row["action_item_id"],
        "title": row["title"],
        "assignee_user_id": row.get("assignee_user_id"),
        "due_at": row.get("due_at"),
        "description": row.get("description"),
        "evidence_span": {
            "meeting_chunk_ids": [row["meeting_chunk_id"]] if row.get("meeting_chunk_id") else [],
            "start_ms": row.get("start_ms"),
            "end_ms": row.get("end_ms"),
        },
        "evidence_text": row["evidence_text"],
        "confidence": 1.0,
        "approval_status": row["approval_status"],
    }


@router.get("/{meeting_id}/analysis")
def get_meeting_analysis(meeting_id: str, user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, object]:
    """저장된 분석 결과(요약·Action Item)를 재조회한다 — `analyze_meeting`을 다시
    실행하지 않는다(2026-08-16, 17번 갭 문서 #10). `analyze_meeting`이 실제
    저장하는 값이 근거이며, 아직 한 번도 분석하지 않은 회의는 404를 반환한다."""

    # `include_deleted=True` — "회의록에서 보기"로 삭제된 회의의 분석 결과도
    # 계속 볼 수 있어야 한다(2026-08-17, 사용자 요청). 목록(`GET /api/v1/meetings`)
    # 에는 여전히 안 보인다 — 직접 링크로 찾아왔을 때만 이 예외가 적용된다.
    meeting = meeting_repository().get(meeting_id, user_id, include_deleted=True)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    actions = meeting_repository().list_actions(meeting_id, user_id)
    if meeting.summary is None and not actions:
        raise HTTPException(status_code=404, detail="this meeting has not been analyzed yet")
    transcripts = [row for row in meeting_repository().list_transcripts(meeting_id, user_id) if row.get("is_final")]
    transcript_ref = str(transcripts[0]["transcript_id"]) if transcripts else None
    source_refs = list(dict.fromkeys(
        [str(row["transcript_id"]) for row in transcripts] + [str(action["action_item_id"]) for action in actions]
    ))
    return {
        "meeting_id": meeting_id,
        "summary": meeting.summary,
        "action_items": [_action_item_response(action) for action in actions],
        "transcript_ref": transcript_ref,
        "source_refs": source_refs,
    }


@router.post("/{meeting_id}/actions/{action_item_id}/approve")
def approve_action_item(meeting_id: str, action_item_id: str, payload: ActionItemRequest, user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, object]:
    """Action Item을 승인하고 동일 원본의 Task ID를 재사용한다."""
    if meeting_repository().get(meeting_id, user_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    action = meeting_repository().upsert_action(action_item_id, meeting_id, user_id, payload.title, payload.evidence_text)
    task_id = str(action["task_id"] or uuid4())
    if action["task_id"] is None:
        task_repository().create(TaskRecord(task_id=task_id, assignee_user_id=user_id, title=payload.title, source_type="action_item", source_id=_source_id(meeting_id, action_item_id)))
    approved = meeting_repository().approve_action(action_item_id, meeting_id, user_id, task_id)
    assert approved is not None
    return {"action_item_id": action_item_id, "approval_status": approved["approval_status"], "task_id": approved["task_id"], "idempotent": action["task_id"] is not None}


class ActionDecision(BaseModel):
    """`decisions[]` 배열의 항목 하나 — 02 §5.3."""
    model_config = ConfigDict(extra="forbid")
    action_item_id: str = Field(min_length=1)
    decision: Literal["approve", "edit", "reject"]
    changes: dict[str, object] | None = None
    """`edit`일 때만 허용한다 — `evidence_text`는 여기 넣어도 무시된다(서버가 원문에서
    추출한 값이라 읽기 전용, 02 §5.3)."""


class ActionsReviewRequest(BaseModel):
    """배치 Action Item 검토 요청 — 15번 문서 조회값 정의 #3."""
    model_config = ConfigDict(extra="forbid")
    decisions: list[ActionDecision] = Field(min_length=1, max_length=50)


@router.post("/{meeting_id}/actions:review")
def review_actions(meeting_id: str, payload: ActionsReviewRequest, user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, object]:
    """여러 Action Item을 한 번에 승인·수정·거절한다(2026-08-16, 17번 갭 문서 #8·#9).

    `changes`는 `edit`에서만 허용한다(02 §5.3) — `approve`·`reject`와 함께 오면
    422로 거절한다. `approve`는 클라이언트가 다시 보낸 `title`/`evidence_text`가
    아니라 이미 저장된 값을 그대로 쓴다(구식 단건 엔드포인트와의 차이 — 분석
    시점에 이미 서버가 저장해 둔 값이 근거이므로 클라이언트가 다시 보낼
    이유가 없다).
    """

    if meeting_repository().get(meeting_id, user_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    results: list[dict[str, object]] = []
    for item in payload.decisions:
        if item.decision != "edit" and item.changes:
            raise HTTPException(status_code=422, detail=f"changes is only allowed when decision is edit (action_item_id={item.action_item_id})")

        if item.decision == "reject":
            action = meeting_repository().reject_action(item.action_item_id, meeting_id, user_id)
            if action is None:
                raise HTTPException(status_code=404, detail=f"action item not found: {item.action_item_id}")
            results.append({"action_item_id": item.action_item_id, "decision": "reject", "approval_status": action["approval_status"], "task_id": action["task_id"]})
            continue

        if item.decision == "edit":
            changes = item.changes or {}
            unknown = set(changes) - {"title", "description", "due_at", "assignee_user_id"}
            if unknown:
                raise HTTPException(status_code=422, detail=f"unsupported change fields: {sorted(unknown)}")
            action = meeting_repository().edit_action(
                item.action_item_id, meeting_id, user_id,
                title=changes.get("title"), description=changes.get("description"),
                due_at=changes.get("due_at"), assignee_user_id=changes.get("assignee_user_id"),
            )
            if action is None:
                raise HTTPException(status_code=404, detail=f"action item not found: {item.action_item_id}")
            results.append({"action_item_id": item.action_item_id, "decision": "edit", "approval_status": action["approval_status"], "task_id": action["task_id"]})
            continue

        # approve — 저장된 title/evidence_text를 그대로 쓴다.
        existing = next((row for row in meeting_repository().list_actions(meeting_id, user_id) if row["action_item_id"] == item.action_item_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"action item not found: {item.action_item_id}")
        task_id = str(existing["task_id"] or uuid4())
        if existing["task_id"] is None:
            task_repository().create(TaskRecord(
                task_id=task_id, assignee_user_id=user_id, title=str(existing["title"]),
                due_at=_parse_due_at(existing.get("due_at")),
                priority_hint=None, source_type="action_item", source_id=_source_id(meeting_id, item.action_item_id),
            ))
        approved = meeting_repository().approve_action(item.action_item_id, meeting_id, user_id, task_id)
        assert approved is not None
        results.append({"action_item_id": item.action_item_id, "decision": "approve", "approval_status": approved["approval_status"], "task_id": approved["task_id"]})

    return {"results": results}
