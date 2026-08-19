"""Gmail·Calendar 증분 동기화 Workflow 경계."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.domain.sync_state import SyncStateRecord
from app.providers.google import CalendarEvent, GmailAdapter, GmailMessage, GoogleCalendarAdapter, SyncCursorExpiredError
from app.repositories.sync_state import SyncStateRepository


@dataclass(frozen=True, slots=True)
class SyncBatch:
    """외부 Provider에서 가져온 정규화 결과와 부분 실패 경고."""

    items: tuple[Any, ...]
    next_cursor: str | None
    warnings: tuple[str, ...] = ()


class ExternalSyncWorkflow:
    """Provider Adapter와 SyncStateRepository를 연결하는 증분 동기화 Workflow."""

    def __init__(self, repository: SyncStateRepository) -> None:
        self.repository = repository

    def sync_gmail(self, user_id: str, adapter: GmailAdapter) -> SyncBatch:
        """Gmail History를 처리하고 성공한 History ID를 저장한다."""

        state = self.repository.get(user_id, "gmail")
        warnings: list[str] = []
        profile = adapter.profile()
        history_id = str(profile.get("historyId", "")) or None
        try:
            if state and state.sync_cursor:
                body = adapter.history(state.sync_cursor)
                items = tuple(adapter.list_messages(max_results=100)) if body.get("history") else ()
            else:
                items = tuple(adapter.list_messages(max_results=100))
        except SyncCursorExpiredError:
            warnings.append("gmail_sync_cursor_expired_reset")
            items = tuple(adapter.list_messages(max_results=100))
        if history_id:
            self.repository.upsert(
                SyncStateRecord(
                    source_sync_state_id=state.source_sync_state_id if state else str(uuid4()),
                    sync_user_id=user_id,
                    source_type="gmail",
                    sync_cursor=history_id,
                    last_synced_at=datetime.now(timezone.utc),
                    status="succeeded",
                )
            )
        return SyncBatch(items=items, next_cursor=history_id, warnings=tuple(warnings))

    def sync_calendar(self, user_id: str, adapter: GoogleCalendarAdapter) -> SyncBatch:
        """Calendar syncToken을 처리하고 성공한 nextSyncToken을 저장한다."""

        state = self.repository.get(user_id, "google_calendar")
        warnings: list[str] = []
        try:
            events, next_token = adapter.list_events(sync_token=state.sync_cursor if state else None)
        except SyncCursorExpiredError:
            warnings.append("calendar_sync_cursor_expired_reset")
            events, next_token = adapter.list_events()
        if next_token:
            self.repository.upsert(
                SyncStateRecord(
                    source_sync_state_id=state.source_sync_state_id if state else str(uuid4()),
                    sync_user_id=user_id,
                    source_type="google_calendar",
                    sync_cursor=next_token,
                    last_synced_at=datetime.now(timezone.utc),
                    status="succeeded",
                )
            )
        return SyncBatch(items=tuple(events), next_cursor=next_token, warnings=tuple(warnings))

    @staticmethod
    def calendar_source_ids(events: tuple[CalendarEvent, ...]) -> tuple[str, ...]:
        """Calendar 제안의 Task 중복 검사 키를 결정적으로 만든다."""

        return tuple(event.source_id for event in events)

    async def publish_task_proposals(
        self,
        user_id: str,
        batch: SyncBatch,
        *,
        titles: dict[str, str],
    ) -> tuple[str, ...]:
        """동기화 결과 중 후보로 판정된 항목을 사용자 SSE Hub로 발행한다.

        `titles`는 상위 Workflow(예: LLM 후보 판정)가 승인한 원본 ID와
        제안 제목의 매핑이다. 매핑에 없는 메일·일정은 제안하지 않으며,
        제안 자체는 DB에 저장하지 않는다.
        """

        from app.proposal_api import publish_calendar_proposal, publish_email_proposal

        proposal_ids: list[str] = []
        for item in batch.items:
            if isinstance(item, GmailMessage) and item.message_id in titles:
                await publish_email_proposal(user_id, item, title=titles[item.message_id])
                proposal_ids.append(item.message_id)
            elif isinstance(item, CalendarEvent) and item.source_id in titles:
                await publish_calendar_proposal(user_id, item, title=titles[item.source_id])
                proposal_ids.append(item.source_id)
        return tuple(proposal_ids)
