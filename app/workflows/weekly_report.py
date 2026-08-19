"""M2.3 주간 보고서 생성과 계획 근거 검증 Workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
import os
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.domain.weekly import WeeklyScope, normalize_week_scope
from app.repositories.meetings import SQLiteMeetingRepository
from app.repositories.tasks import TaskRepository
from app.workflows.daily_briefing import _repositories
from app.workflows.registry import WorkflowRequest, WorkflowResult
from app.workflows.weekly_classification import (
    build_weekly_report_data,
    extract_next_week_plans,
    link_approved_tasks,
)


@dataclass(frozen=True, slots=True)
class PlanGroundingResult:
    """검증된 계획과 Artifact에 넣지 않은 후보를 함께 보관한다."""

    accepted: tuple[dict[str, object], ...]
    rejected: tuple[dict[str, object], ...]


_KINDS = {"planned", "carry_over", "suggestion"}
LLM_BASE_URL_ENV = "LLM_BASE_URL"
LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_MODEL_ENV = "LLM_MODEL"
_DEFAULT_LLM_MODEL = "openai/gpt-4.1-mini"
_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": 3000}},
}


class LLMProviderError(RuntimeError):
    """주간 보고서 요약 Provider 호출이 실패한 경우다."""


LLMClient = Callable[[dict[str, object]], Awaitable[str | Mapping[str, object]] | str | Mapping[str, object]]


async def _call_llm_with_retry(
    client: LLMClient,
    payload: dict[str, object],
    *,
    sleep: Callable[[float], None] | None = None,
) -> str | Mapping[str, object]:
    """LLM 호출 실패를 한 번 재시도하고 마지막 오류를 보존한다.

    timeout·429·5xx와 Schema 오류는 Provider Adapter에서 `LLMProviderError`로
    정규화된다. 첫 실패 뒤 짧게 대기하고 한 번만 재호출하며, 두 번째 실패는
    Workflow가 원장 기반 부분 결과와 warning으로 변환하도록 다시 발생시킨다.
    """

    wait = sleep or time.sleep
    for attempt in range(2):
        try:
            generated = client(payload)
            if inspect.isawaitable(generated):
                generated = await generated
            return generated
        except LLMProviderError:
            if attempt == 1:
                raise
            wait(0.1)
    raise AssertionError("unreachable")


def validate_next_week_plans(
    plans: Iterable[Mapping[str, object]],
    *,
    allowed_source_refs: Iterable[str],
) -> PlanGroundingResult:
    """LLM 계획 후보를 허용된 원본 ID와 Schema 필드로 제한한다.

    `planned`와 `carry_over`는 입력에 존재하는 source ID가 하나 이상
    있어야 확정 결과로 통과한다. 근거가 없거나 허용되지 않은 ID를 포함한
    후보는 `rejected`에만 남기며, 공개 Artifact에는 넣지 않는다.
    `suggestion`도 source_refs가 없으면 Schema 계약상 반환하지 않는다.
    """

    allowed = {ref for ref in allowed_source_refs if ref}
    accepted: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for index, raw in enumerate(plans):
        title = raw.get("title")
        kind = raw.get("kind")
        raw_refs = raw.get("source_refs")
        refs = tuple(dict.fromkeys(ref for ref in raw_refs if isinstance(ref, str) and ref in allowed)) if isinstance(raw_refs, (list, tuple, set)) else ()
        reason: str | None = None
        if not isinstance(title, str) or not title.strip():
            reason = "title_missing"
        elif len(title.strip()) > 300:
            reason = "title_too_long"
        elif kind not in _KINDS:
            reason = "unsupported_kind"
        elif not refs:
            reason = "evidence_required"
        if reason:
            rejected.append({"index": index, "reason": reason})
            continue
        accepted.append(
            {
                "title": title.strip(),
                "kind": kind,
                "source_refs": list(refs),
            }
        )
    accepted.sort(key=lambda item: (str(item["kind"]), str(item["title"]), tuple(item["source_refs"])))
    return PlanGroundingResult(accepted=tuple(accepted), rejected=tuple(rejected))


def _decode_json_content(content: str) -> dict[str, object]:
    """Provider가 반환한 JSON 또는 코드 블록을 요약 객체로 변환한다."""

    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
        raise LLMProviderError("LLM response must contain a summary string")
    summary = str(parsed["summary"]).strip()
    if not summary or len(summary) > 3000:
        raise LLMProviderError("LLM summary length is invalid")
    return {"summary": summary}


def _call_openai_compatible(payload: dict[str, object]) -> Mapping[str, object]:
    """환경 변수로 지정한 OpenAI 호환 API에서 요약 JSON을 가져온다."""

    base_url = os.getenv(LLM_BASE_URL_ENV, "").strip()
    api_key = os.getenv(LLM_API_KEY_ENV, "").strip()
    if not base_url or not api_key:
        raise LLMProviderError(f"{LLM_BASE_URL_ENV} and {LLM_API_KEY_ENV} are required")
    body = {
        "model": os.getenv(LLM_MODEL_ENV, _DEFAULT_LLM_MODEL),
        "messages": [
            {
                "role": "system",
                "content": "주간 업무 데이터를 근거로 한국어 요약을 작성한다. 입력의 Task 상태와 source_refs는 변경하지 않는다.",
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ],
        "temperature": 0,
        "top_p": 1,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "weekly_report_summary", "strict": True, "schema": _SUMMARY_SCHEMA},
        },
    }
    request = Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise LLMProviderError(f"LLM provider returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LLMProviderError("LLM provider connection failed") from exc
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMProviderError("LLM provider response has no message content") from exc
    if not isinstance(content, str):
        raise LLMProviderError("LLM provider content must be text")
    return _decode_json_content(content)


def _weekly_meeting_highlights(
    repository: SQLiteMeetingRepository,
    *,
    scope: WeeklyScope,
) -> tuple[dict[str, object], ...]:
    """조회 주간에 열린, 이미 분석된 회의의 요약을 근거와 함께 모은다.

    "주요 회의 내용"은 `WeeklyReportResult` Schema에 구조화 필드가 없다 —
    Markdown 본문에만 있다(15번 문서 주간 업무보고 조회값 정의 #1 각주). 여기서
    `analyze_meeting`이 이미 만들어 저장해 둔 `Meeting.summary`(2026-08-16,
    17번 갭 문서 #10)를 그대로 재사용한다 — 새 LLM 호출을 하지 않는다. 아직
    분석하지 않은 회의(`summary`가 `None`)는 근거가 없어 제외한다.
    """

    highlights: list[dict[str, object]] = []
    for meeting in repository.list(scope.user_id):
        if not meeting.summary:
            continue
        when = meeting.started_at or meeting.created_at
        if when is None:
            continue
        when = when.astimezone(scope.period.start_at.tzinfo)
        if not (scope.period.start_at <= when < scope.period.end_at):
            continue
        highlights.append({"title": meeting.title, "summary": meeting.summary, "source_refs": [meeting.meeting_id]})
    highlights.sort(key=lambda item: (str(item["title"]), str(item["source_refs"])))
    return tuple(highlights)


async def build_weekly_report_workflow(
    request: WorkflowRequest,
    *,
    task_repository: TaskRepository | None = None,
    meeting_repository: SQLiteMeetingRepository | None = None,
    llm_client: LLMClient | None = None,
) -> WorkflowResult:
    """사용자 Task 원장과 검증된 계획으로 실제 `weekly_report`를 생성한다.

    LLM은 요약 문장만 만들며 분류 목록·계획 kind·source_refs는 결정론적
    Workflow가 보존한다. Provider 실패 시 원장 기반 부분 결과와 warning을
    반환하고 Mock 결과로 대체하지 않는다.
    """

    payload = dict(request.payload)
    timezone_name = str(payload.get("timezone", "Asia/Seoul"))
    week_of = payload.get("week_of") or datetime.now(timezone.utc).astimezone().date().isoformat()
    scope = normalize_week_scope(request.user_id, str(week_of), timezone_name, as_of=payload.get("as_of"))
    # 일일 브리핑과 같은 운영 저장소 선택 규칙(PostgreSQL 우선, SQLite 개발 경로)을 재사용한다.
    repository = task_repository or _repositories()[0]
    if meeting_repository is None:
        # 모듈 최상단에서 import하면 `app.a2a.runtime` → `weekly_report` →
        # `meeting_api` → `internal_chat` → `app.a2a.runtime` 순환 import가
        # 생긴다(meeting_api가 내부 인증 의존성으로 internal_chat을 참조).
        # 함수 실행 시점엔 모든 모듈이 이미 로드돼 있어 안전하다.
        from app.meeting_api import meeting_repository as _default_meeting_repository

        meeting_repository = _default_meeting_repository()
    meetings_repository = meeting_repository
    tasks = repository.list(request.user_id)
    items = link_approved_tasks(tasks, scope=scope)
    data = build_weekly_report_data(items, scope=scope)
    allowed_refs = {
        ref
        for task in tasks
        for ref in (task.task_id, task.source_id)
        if ref
    }
    plans = validate_next_week_plans(
        extract_next_week_plans(tasks, scope=scope),
        allowed_source_refs=allowed_refs,
    )
    data["next_week_plans"] = list(plans.accepted)
    data["source_refs"] = list(
        dict.fromkeys([*data["source_refs"], *(ref for plan in plans.accepted for ref in plan["source_refs"])])
    )
    warnings: list[dict[str, object]] = []
    if plans.rejected:
        warnings.append({"source": "weekly_report", "code": "PLAN_CANDIDATE_REJECTED", "message": f"{len(plans.rejected)} plan candidate(s) lacked valid evidence", "retryable": False, "last_success_at": None})

    summary_input = {key: value for key, value in data.items() if key != "summary"}
    try:
        client = llm_client or _call_openai_compatible
        generated = await _call_llm_with_retry(client, summary_input)
        summary_value = generated.get("summary") if isinstance(generated, Mapping) else None
        if not isinstance(summary_value, str) or not summary_value.strip():
            raise LLMProviderError("LLM summary is missing")
        data["summary"] = summary_value.strip()
    except LLMProviderError as exc:
        warnings.append({"source": "llm", "code": "LLM_PROVIDER_UNAVAILABLE", "message": str(exc), "retryable": True, "last_success_at": None})

    typed_result = {"type": "weekly_report", "data": data}
    meeting_highlights = _weekly_meeting_highlights(meetings_repository, scope=scope)
    markdown = _render_weekly_report_markdown(data, meeting_highlights=meeting_highlights)
    return WorkflowResult(
        artifact_name="weekly_report",
        artifact_description="Source-backed weekly report from the user-owned Task ledger.",
        text=json.dumps(typed_result, ensure_ascii=False, sort_keys=True),
        data=typed_result,
        markdown=markdown,
        warnings=warnings,
        mock=False,
    )


def _render_weekly_report_markdown(
    data: Mapping[str, object],
    *,
    meeting_highlights: Iterable[Mapping[str, object]] = (),
) -> str:
    """검증된 주간 보고 데이터를 복사용 Markdown으로 변환한다.

    구조화 JSON을 원장으로 유지하고, 화면과 사용자가 바로 읽을 수 있는 표현만
    결정론적으로 생성한다. LLM이 분류나 근거 ID를 새로 만들지 않는다.
    `meeting_highlights`("주요 회의 내용")는 `data`에 없다 — `WeeklyReportResult`
    Schema에 이 내용을 담을 구조화 필드가 없어 Markdown 본문에만 싣는다
    (2026-08-16, 17번 갭 문서 #17, 15번 문서 주간 업무보고 조회값 정의 #1 각주).
    """

    lines = [
        "# 주간 보고서",
        "",
        f"기간: {data['period']['start']} ~ {data['period']['end']}",
        "",
        str(data.get("summary", "")),
    ]
    sections = (
        ("완료", "completed"),
        ("진행", "in_progress"),
        ("지연", "delayed"),
        ("미해결 이슈", "unresolved_issues"),
    )
    for title, key in sections:
        _render_work_items(lines, title, data.get(key, []))

    # 주요 회의 내용 — WorkItem과 형태가 달라(제목·요약을 둘 다 보여줘야 함)
    # 위 공용 렌더러를 그대로 쓰지 않는다.
    lines.extend(("", "## 주요 회의 내용", ""))
    highlights = list(meeting_highlights)
    if not highlights:
        lines.append("- 없음")
    else:
        for highlight in highlights:
            title_h = highlight.get("title")
            summary_h = highlight.get("summary")
            refs = highlight.get("source_refs", [])
            suffix = f" (`{', '.join(str(ref) for ref in refs)}`)" if refs else ""
            lines.append(f"- **{title_h}**: {summary_h}{suffix}")

    _render_work_items(lines, "다음 주 계획", data.get("next_week_plans", []))
    return "\n".join(lines).strip() + "\n"


def _render_work_items(lines: list[str], title: str, values: object) -> None:
    """`WorkItem`/`PlanItem` 배열 하나를 `## {title}` Markdown 절로 덧붙인다."""

    lines.extend(("", f"## {title}", ""))
    if not isinstance(values, list) or not values:
        lines.append("- 없음")
        return
    for value in values:
        if isinstance(value, Mapping):
            label = value.get("title") or value.get("text") or value.get("summary")
            refs = value.get("source_refs", [])
            suffix = f" (`{', '.join(str(ref) for ref in refs)}`)" if refs else ""
            lines.append(f"- {label or value}{suffix}")
        else:
            lines.append(f"- {value}")


__all__ = [
    "LLMProviderError",
    "PlanGroundingResult",
    "build_weekly_report_workflow",
    "validate_next_week_plans",
]
