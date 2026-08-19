"""자연어 문장을 Skill 호출로 바꾸는 Workmate 어시스턴트 Router.

Drawer의 Skill 선택 UI(드롭다운 + 개별 입력 폼)를 자연어 대화로 바꿔
달라는 요청으로 추가했다(2026-08-17). 15번 문서는 애초에 자연어→파라미터
추출(Router)을 "필요하다는 근거가 아직 없다"며 범위 밖으로 미뤄뒀었는데
(18번 문서 "남은 범위"), 이제 사용자가 명시적으로 요청해 구현한다.

두 단계 LLM 호출로 동작한다:
  1) `route()` — 사용자 문장을 보고 어떤 Skill을 어떤 인자로 부를지(또는
     그냥 답하거나 되물을지) 정한다.
  2) `phrase_answer()` — 실행된 Skill의 구조화 결과(JSON)를 사용자 질문에
     대한 자연어 한국어 답으로 바꾼다.

파괴적 동작(Action Item 거절, 제안 무시, 할 일 삭제)은 Router가 스스로
실행하지 않는다 — 18번 문서 Human-in-the-loop 원칙을 자연어 경로에도
그대로 지킨다: 1차 호출은 "이렇게 할까요?" 확인만 반환하고(`needs_confirmation`),
사용자가 확인을 누르면 프론트가 같은 `skill_id`·`arguments`를
`confirmed_skill_id`·`confirmed_arguments`로 실어 다시 호출해야 실제로
실행된다 — Router가 다시 LLM에 묻지 않고 그대로 실행한다.

회의 ID·제안 ID처럼 사용자 문장에 없는 값을 LLM이 지어내지 않도록,
호출부(`assistant_ask_workflow`)가 최근 회의·대기 제안 목록을 `route()`의
Context로 함께 전달해 이름으로 매칭할 수 있게 한다. Drawer는 이 Context를
직접 만들어 보내지만, 그렇게 못 하는 호출자(오케스트레이터 어댑터 등, 20번
문서)를 위해 호출자가 `context`를 안 보내면 서버가 최근 10건까지 직접
채운다(`_with_default_context`, 2026-08-17) — 회의는 저장된 Repository를,
제안은 SSE로 내보내기 전 검토 대기 상태로 들고 있는 `proposal_hub`의
프로세스 메모리를 그대로 재사용한다(둘 다 새 저장소를 만들지 않는다).

`route()`는 오늘 날짜도 System Prompt에 함께 넣는다(2026-08-17, 실사용 중
발견 — "지난 일주일 주간보고"라고 물었는데 `week_of`를 비워 보내 서버
기본값("이번 주" = 오늘부터 7일)이 그대로 나가 버렸다: LLM이 "오늘이
언제인지" 전혀 모른 채 상대 시점("지난 주"·"이번 달" 등)을 판단하고
있었다). `week_of`는 `app/domain/weekly.py`의 `normalize_week_scope()`
계약대로 "그 날짜가 속한 ISO 주간"이 아니라 "그 날짜부터 7일 구간의
시작일"이다 — 이 차이를 카탈로그에도 명시해 LLM이 상대 시점을 오늘
날짜 기준으로 직접 계산하게 한다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib import error, request

from app.workflows.registry import WorkflowRequest, WorkflowResult


class AssistantRouterError(RuntimeError):
    """LLM 라우팅 또는 최종 답변 생성에 실패했다."""


# `docs/schemas/workmate-skill-schemas.schema.json`의 각 Input `$def`를 사람이
# 읽는 요약으로 옮겼다 — LLM 프롬프트용이라 원본 JSON Schema를 그대로 붙이지
# 않고 스스로 설명하는 문장으로 압축했다. 스키마의 필수 필드가 바뀌면 이
# 카탈로그도 함께 고쳐야 한다.
_SKILL_CATALOG = """\
- daily_briefing: 오늘 브리핑(오늘 일정·메일 신호·우선순위 Top 3). 인자 없음.
- weekly_report: 업무 요약(완료·진행·지연·미해결 이슈·다음 주 계획).
  인자: week_of(YYYY-MM-DD) — "그 날짜부터 7일간"을 조회한다("그 날짜가 속한 주"가 아니다).
  **week_of는 절대 비워두지 마라 — 항상 아래 규칙으로 직접 계산해서 채워라**(2026-08-17,
  사용자 요청 — "주간보고"는 관행적으로 지난 한 주를 되짚는 보고이지, 앞으로 일주일을
  미리 보는 게 아니다). 사용자 문장을 아래 경우 중 **정확히 하나에만** 맞춰 계산하라
  (두 규칙을 겹쳐 적용하지 마라 — 예를 들어 "지난 주"는 그 자체로 이미 오늘-7일이니
  거기서 다시 7일을 더 빼면 안 된다): "이번 주"라고 명시했으면 week_of=오늘. "지난
  주"라고 명시했거나 **아무 상대 표현도 날짜도 없으면**(기본값) week_of=오늘-7일(=
  "지난 일주일" — 기준일자가 불분명할 땐 오늘을 기준으로 한다). "지지난 주"·"2주
  전"이면 week_of=오늘-14일. 사용자가 구체적 날짜 D를 대고 "D 기준 지난 주"처럼
  말했으면 week_of=D-7일(D를 그대로 쓰지 않는다).
- rank_priorities: 우선순위 Top 3를 지금 시점 기준으로 다시 계산.
  인자 없음.
- search_meetings: 과거 회의록에서 근거를 찾아 답한다.
  인자: query(필수, 사용자 질문 원문 그대로), filters.date_from/date_to(선택, YYYY-MM-DD —
  "지난달"·"이번 주" 같은 상대 표현도 [오늘 날짜] 기준으로 직접 계산해 채워라).
- analyze_meeting: **아직 분석하지 않은** 회의의 Transcript를 처음으로 요약·
  Action Item 추출한다(시간·비용이 드는 작업이라 **항상 실행 전 확인이 필요**
  하다 — call_skill로 골라도 바로 실행되지 않고 확인 배너가 먼저 뜬다). 인자:
  meeting_id(필수, Context의 회의 목록에서 찾아 채운다). Context의 각 회의
  줄에 `[분석 완료]`/`[분석 대기]`/`[분석 여부 불명]` 표시가 있다 — **이미
  `[분석 완료]`인 회의라면, 사용자 문장에 "다시"·"재분석"·"또"처럼 재실행을
  명시하는 단어가 없는 한 이 Skill을 고르지 마라.** "오늘 한 회의 분석해줘",
  "이 회의 분석해줘"처럼 그냥 "분석해줘"라고만 말한 경우도 재실행 요청이
  **아니다** — 이미 결과가 있으므로 get_meeting_analysis로 보여줘라(예:
  대상이 `[분석 완료]` 회의 하나뿐이면 skill_id="get_meeting_analysis"로
  call_skill 하거나, 후보가 여럿이면 그중 분석 완료된 것부터 결과를 보여줄지
  reply로 물어라). `[분석 여부 불명]`이면 아는 척 단정하지 말고(예: "아직
  분석 안 한 회의를 분석하겠습니다"처럼 근거 없이 말하지 마라) 그냥 "이
  회의를 분석할까요?"처럼 확인만 물어라. **어떤 회의를 말하는지 Context에서
  하나로 확실히 특정되지 않으면**(회의 제목·시각이 비슷한 후보가 여럿이거나,
  "방금 진행한 회의"처럼 표현이 모호해 정확히 어떤 회의인지 알 수 없으면)
  **절대 아무거나 짐작해서 고르지 마라** — action="reply"로 후보 회의들을
  날짜·시각과 함께 목록으로 보여주고(예: "다음 중 어떤 회의인가요?\n1) 8월
  17일 16:06\n2) 8월 17일 16:51") 사용자가 정확히 특정해 다시 답할 때까지
  기다려라. 후보가 하나로 확실할 때만 call_skill로 진행하되, 그때도 확인
  배너를 통해 사용자가 실행을 최종 승인해야 한다. **call_skill일 때도 reply를
  꼭 채워라** — 이 reply가 확인 배너에 그대로 뜬다. "분석 대기인 회의를
  분석하겠습니다"처럼 어떤 회의인지 말 안 하고 뭉뚱그리면 사용자가 무엇을
  승인하는지 알 수 없다(2026-08-17, 실사용 중 발견) — 반드시 Context에서 고른
  그 회의의 제목·날짜·시각을 reply에 그대로 적어라(예: "'8월 17일 17:31'
  회의를 분석하겠습니다").
- get_meeting_analysis: 이미 분석된 회의(Context에 `[분석 완료]`로 표시)의
  저장된 요약·Action Item을 다시 보여준다 — "분석해줘"라고만 말했어도
  대상이 이미 `[분석 완료]`라면 재분석(analyze_meeting) 대신 **반드시 이
  Skill을 먼저 쓴다**(사용자가 "다시"·"재분석"·"또" 등으로 재실행을 명시한
  경우만 예외). 인자: meeting_id(필수, Context의 회의 목록에서 제목으로
  찾아 채운다).
- review_action_items: 사용자가 **명시적으로 승인·거절하라고 요청했을 때만** 쓴다.
  인자: meeting_id(필수), decisions(필수, [{action_item_id, decision: approve|reject}]
  배열) — action_item_id는 먼저 get_meeting_analysis로 확인해야 안다. Context에
  "방금 제안한 Action Item 후보(번호 순서)" 목록이 있으면, 그건 바로 앞
  assistant 응답이 "이 Action Item을 할 일로 추가할까요?"로 물었던 항목들을
  번호 순서 그대로 담은 것이다 — 사용자가 그중 일부만 골라 답하면(번호로
  "1번만"·"두 번째만"·"1, 2번"처럼 말하든, **내용으로**("가이드 공유하는 거만
  추가해줘"처럼 그 assistant 응답에 나열된 항목 제목을 가리키는 표현으로)
  말하든 상관없이, 바로 앞 assistant 응답의 번호 목록에서 어떤 항목을
  가리키는지 먼저 찾아낸 다음 **그 번호에 대응하는 pending_action_items의
  action_item_id를 그대로 꺼내 써라**(임의로 지어내지 마라). meeting_id도
  그 목록의 값을 그대로 쓴다. 그 목록이 Context에 없는데도 action_item_id를
  모르면 절대 지어내지 말고 "reply"로 먼저 어떤 회의인지, 무엇을
  승인/거절할지 물어라 — **이럴 때 manage_tasks(action=create)로 대신 새
  할 일을 만들지 마라**(회의 근거 링크가 통째로 끊긴다, 2026-08-17 실사용
  중 발견된 버그 — 번호 지칭은 고쳤지만 내용으로 지칭한 경우 이 함정에
  또 빠지는 게 재현됐다).
- review_proposal: 사용자가 **명시적으로 승인·무시(반려)하라고 요청했을 때만** 쓴다.
  인자: source_type(email|calendar), decision(approve|ignore), 그리고 email이면
  message_id, calendar면 calendar_id+event_id — Context의 제안 목록에서 제목으로
  찾아 채운다.
- read_email: 이메일의 **실제 본문 내용**이 필요할 때 쓴다 — "요약해줘"·"내용
  알려줘"·"뭐라고 적혀 있어?"처럼 Context의 제안 제목만으로는 답할 수 없는
  질문. Context의 제안 목록에는 짧은 title 하나만 있고 본문은 없다 —
  title을 그대로 되풀이해 "~내용은 ~와 관련된 내용입니다"처럼 답을 지어내지
  마라(2026-08-17, 실사용 중 발견 — 제목을 요약인 것처럼 돌려줘서 쓸모없는
  답이 됐다). 이 Skill이 매번 Gmail에서 실제 본문을 다시 가져온다(저장하지
  않음, 확인 불필요 — 읽기 전용). 인자는 둘 중 하나만 채운다: (a)
  message_id — Context의 제안 목록에서 source_type=email인 항목이 있으면
  그 message_id를 그대로 쓴다. (b) query — Context에 해당 이메일이 없으면
  (이미 처리됐거나 애초에 제안으로 뜬 적 없는 메일도) 지어내지 말고
  검색어를 query에 채운다. **query는 Gmail 검색창에 그대로 넣을 핵심
  키워드 2~5개만 골라야 한다 — 사용자 문장 전체를 그대로 복사해 넣지
  마라**(2026-08-17, 실사용 중 발견 — 질문 원문을 통째로 넣었더니 "찾아줘"·
  "언제"·"있는지" 같은 메일 본문에 없는 단어까지 검색어에 섞여 공백으로
  구분된 모든 단어를 AND로 요구하는 Gmail 검색이 실제로 존재하는 메일도
  못 찾았다). 실제 메일 제목·본문에 그대로 등장할 법한 고유명사·버전
  번호·프로젝트명·핵심 명사만 남겨라 — 예를 들어 "v2.4.0 업데이트 정기
  배포 및 DB 마이그레이션 작업은 언제 예정돼 있는지 메일에서 찾아줘"라면
  query="v2.4.0 배포 마이그레이션"처럼 줄여라. **query로 찾을 때는 최근
  30일 내 메일만 검색한다** — 결과를 답할 때 이 30일 제한을 반드시 함께
  언급해라(예: "최근 30일 내 메일에서 찾았습니다" 또는 찾지 못했으면
  "최근 30일 내에서는 찾지 못했습니다 — 더 오래된 메일일 수 있습니다"),
  그냥 못 찾았다고만 답하지 마라.
- manage_tasks: 할 일 조회 또는 신규 등록만 가능하다(수정·삭제는 지원하지
  않는다 — "할 일 관리" 화면을 이용하라고 안내하라). 인자: action(list|create),
  create일 때 title(필수)·due_at(선택, ISO 8601). "이메일로 받은 ~요청 내용 찾아줘"·
  "그 수정 요청 있었나?"처럼, 예전에 받은 요청을 찾는 질문은 **모른다고 답하기
  전에 반드시 먼저 action="call_skill"·skill_id="manage_tasks"·action="list"로
  실제 목록을 조회하라** — "목록에서 확인해 보세요"라고 사용자에게 미루지 말고
  네가 직접 조회해서 판단해라. 이미 승인된 이메일·Calendar 제안은 원본 요청
  내용이 title에 그대로 담긴 Task로 남는다(source_type=email/calendar). 목록을
  조회한 결과는 다음 턴에 그 데이터로 다시 판단하게 되니, 지금은 그냥 list를
  실행하면 된다 — 제목보다 자세한 이메일 본문 내용이 필요하면 read_email을
  대신 써라.

Context에 없는 회의·제안은 존재를 지어내지 마라. 필요한 정보(회의 제목,
어떤 Action Item인지 등)가 Context에도 사용자 문장에도 없으면 반드시
action="reply"로 무엇이 더 필요한지 되물어라.

**중요 1 — reply는 항상 그 자리에서 끝나는 최종 답이다**: 이후 사용자가 다시
묻지 않는 한 후속 턴은 없다 — "찾아드릴게요"·"확인해드릴게요"·"알려드릴게요"·
"검색해보겠습니다"처럼 **나중에 하겠다는 약속형 문장은 reply에 절대 쓰지 마라**.
지금 바로 결론을 내려서 답하라 — 방법은 둘 중 하나뿐이다: (a) Context나 사용자
문장에 이미 있는 사실을 그대로 인용해 답하거나(예: "'신규 입사자 환영회'는
2026년 8월 21일 오후 6시입니다" — 날짜·시각은 ISO 8601 원문(2026-08-21T18:00:00+09:00)을
그대로 옮기지 말고 "YYYY년 M월 D일 오전/오후 H시"처럼 자연스러운 한국어로 바꿔
써라), (b) 그 정보가 없으면 "~에 대한 정보가 없습니다"처럼
**지금 모른다고 정직하게 인정**하고 "제안함"·"할 일 관리"·"회의 관리" 같은
확인할 화면을 안내하라(예: "'신규 입사자 환영회' 일정 정보가 없습니다 —
제안함이나 캘린더에서 직접 확인해 주세요"). 그 중간(있는지 없는지 모호하게
얼버무리는 답)은 없다. **call_skill을 고를 때도 reply는 채워야 한다** — 확인이
필요한 Skill이면 이 reply가 그대로 확인 배너에 뜬다. "대상 회의를
처리하겠습니다"처럼 무엇을 실행하는지 뭉뚱그리지 말고, Context에서 실제로 고른
대상(회의 제목·날짜·시각, Action Item 내용 등)을 reply에 구체적으로 적어라 —
사용자가 확인 배너만 보고도 무엇을 승인하는지 알 수 있어야 한다(2026-08-17,
실사용 중 발견).

**중요 2**: review_action_items·review_proposal·manage_tasks(action=create)는 모두
데이터를 바꾸는 Skill이다. 사용자 문장이 "~해줘"·"~해줄래"처럼 **명시적인 실행
요청**일 때만 골라라. "언제야?"·"뭐였지?"·"찾아줘"·"알려줘"처럼 **정보를 묻는
질문**(그 답이 Context의 제안·회의 항목과 관련 있어 보여도)은 절대 승인/거절/
무시 Skill을 고르지 마라 — action="reply"를 골라 위 원칙대로 답하라.
"""

# `_chat_completion`의 LLM Provider 호출 재시도 횟수 — 2026-08-19 발견한 간헐적
# DNS 실패(`getaddrinfo failed`) 대응. 최초 시도 포함 총 시도 횟수다.
_CHAT_COMPLETION_MAX_ATTEMPTS = 3
_CHAT_COMPLETION_RETRY_DELAY_SECONDS = 0.5

# `manage_tasks`는 카탈로그 설명과 달리 Workflow 자체는 update/delete도 지원하지만
# (`app/workflows/assistant_skills.py`), 자연어 경로에서는 LLM이 본 적 없는
# task_id를 지어낼 위험이 있어 list/create만 허용한다 — 기존 Drawer 구조화
# 폼과 같은 범위(2026-08-16 결정을 그대로 유지).
_ALLOWED_MANAGE_TASKS_ACTIONS = {"list", "create"}

_ROUTABLE_SKILL_IDS = (
    "daily_briefing",
    "weekly_report",
    "rank_priorities",
    "search_meetings",
    "analyze_meeting",
    "get_meeting_analysis",
    "review_action_items",
    "review_proposal",
    "manage_tasks",
    "read_email",
)


def _requires_confirmation(skill_id: str, arguments: dict[str, Any]) -> bool:
    """확인 없이 바로 실행하면 안 되는 조합인지 — 18번 문서 Human-in-the-loop 범위다.

    파괴적 동작(거절·무시·삭제)뿐 아니라, `analyze_meeting`처럼 **되돌릴 수는
    없어도 파괴적이진 않은** costly한 동작도 포함한다(2026-08-17, 사용자
    요청) — STT+LLM 비용이 들고, 여러 회의 중 엉뚱한 걸 골라 실행하면 그
    비용이 헛되이 든다. `route()`가 Context에서 회의 하나로 확실히 특정하지
    못하면 애초에 여기까지 오지 않고 후보 목록으로 되묻는다(카탈로그 지침
    참고) — 여기 도달했다는 건 이미 하나로 특정됐다는 뜻이고, 그래도 사용자의
    최종 승인은 항상 받는다.
    """

    if skill_id == "analyze_meeting":
        return True
    if skill_id == "review_action_items":
        decisions = arguments.get("decisions")
        return isinstance(decisions, list) and any(isinstance(item, dict) and item.get("decision") == "reject" for item in decisions)
    if skill_id == "review_proposal":
        return arguments.get("decision") == "ignore"
    if skill_id == "manage_tasks":
        return arguments.get("action") == "delete"
    return False


@dataclass(frozen=True, slots=True)
class RoutedAction:
    """`route()`의 결과 — 직접 답하거나, 어떤 Skill을 어떤 인자로 부를지."""

    action: str  # "reply" | "call_skill"
    reply: str | None
    skill_id: str | None
    arguments: dict[str, Any] | None


def _chat_completion(
    messages: list[dict[str, str]],
    *,
    schema: dict[str, Any],
    schema_name: str,
    strict: bool,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
) -> dict[str, Any]:
    key = api_key or os.getenv("OPENAI_API_KEY")
    url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    configured_model = model or os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini")
    chosen_model = "gpt-4.1-mini" if configured_model == "openai/gpt-4.1-mini" else configured_model
    if not key:
        raise AssistantRouterError("OPENAI_API_KEY is required")
    json_schema: dict[str, Any] = {"name": schema_name, "schema": schema}
    if strict:
        json_schema["strict"] = True
    payload = {
        "model": chosen_model,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": json_schema},
        "messages": messages,
    }
    req = request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    # 2026-08-19 실사용 중 발견 — `_ipv4_only_getaddrinfo`(app/main.py)로 IPv4를
    # 강제해도 이 환경에서 LLM Provider 호출이 간헐적으로
    # `socket.gaierror: [Errno 11001] getaddrinfo failed`로 실패하는 게 재현됐다
    # (재시도 없이 바로 assistant_ask 전체가 500으로 죽었다). 원인이 이 프로세스
    # 밖(환경의 일시적 DNS 응답 실패)이라 완전히 없앨 수는 없어, 흔한 일시적
    # 오류에 대응하는 표준 방식대로 재시도를 추가한다 — HTTP 오류(4xx/5xx)는
    # 재시도해도 똑같이 실패할 뿐이라 그대로 즉시 실패시키고, 네트워크 계층
    # 오류(연결·DNS·Timeout·JSON 파싱)만 재시도 대상으로 좁힌다.
    last_exc: Exception | None = None
    body: dict[str, Any] | None = None
    for attempt in range(_CHAT_COMPLETION_MAX_ATTEMPTS):
        try:
            with request.urlopen(req, timeout=30) as response:
                body = json.loads(response.read().decode())
            break
        except error.HTTPError as exc:
            raise AssistantRouterError(f"LLM Provider returned HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt + 1 < _CHAT_COMPLETION_MAX_ATTEMPTS:
                time.sleep(_CHAT_COMPLETION_RETRY_DELAY_SECONDS)
    if body is None:
        raise AssistantRouterError("LLM Provider request failed") from last_exc
    try:
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AssistantRouterError("LLM response is not valid JSON") from exc


# `arguments`를 Skill마다 다른 모양의 자유 `object`로 두고 `strict`를 껐더니
# (최초 구현), 실사용 중 이 Provider가 그 느슨한 Schema를 사실상 무시하고
# Schema 정의 자체를 답으로 그대로 반환하는 사례가 나왔다 — `route()`가
# `{"type": "object", "properties": {...}}` 같은 Schema 원문을 완료 결과로
# 받아 "invalid action" 오류로 사용자에게 그대로 노출됐다(2026-08-17, 실사용
# 중 발견). `strict: true`로 바꿔보면 이 Provider는 반대로 매 요청을 HTTP
# 400으로 거부한다 — `additionalProperties: false` + 모든 속성이
# `required`에 있어야 하는 OpenAI Strict 모드 요건을, Skill마다 모양이 다른
# 자유 `object` `arguments`가 만족하지 못해서다.
#
# 그래서 "Skill마다 다른 모양"을 별도 Branch로 나누는 대신, 모든 Skill의
# 인자 후보 필드를 한 평평한(Flat) `object`에 다 펼쳐 놓고 각자 Nullable로
# 둔다(Strict 모드가 요구하는 "모든 속성이 required"는 만족하면서, 실제로는
# 고른 `skill_id`에 맞는 필드만 채우고 나머지는 `null`로 두게 한다 —
# System Prompt의 카탈로그가 그렇게 안내한다). `review_proposal`의 승인
# Task 필드(title·assignee_user_id 등)는 LLM에 맡기지 않는다 —
# `assistant_ask_workflow`가 Context의 제안 제목과 호출자 user_id로 직접
# 채운다(아래 참고).
_ARGUMENTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "week_of", "query", "date_from", "date_to", "meeting_id", "decisions",
        "source_type", "decision", "message_id", "calendar_id", "event_id",
        "action", "title", "due_at", "task_id",
    ],
    "properties": {
        "week_of": {"type": ["string", "null"], "description": "weekly_report. YYYY-MM-DD, 그 날짜부터 7일간."},
        "query": {"type": ["string", "null"], "description": "search_meetings(사용자 질문 원문) 또는 read_email(message_id를 모를 때 검색어, 최근 30일 내만 검색됨)."},
        "date_from": {"type": ["string", "null"], "description": "search_meetings 필터. YYYY-MM-DD."},
        "date_to": {"type": ["string", "null"], "description": "search_meetings 필터. YYYY-MM-DD."},
        "meeting_id": {"type": ["string", "null"], "description": "analyze_meeting·get_meeting_analysis·review_action_items."},
        "decisions": {
            "type": ["array", "null"],
            "description": "review_action_items. [{action_item_id, decision}] 배열.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action_item_id", "decision"],
                "properties": {
                    "action_item_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["approve", "reject"]},
                },
            },
        },
        "source_type": {"type": ["string", "null"], "enum": ["email", "calendar", None], "description": "review_proposal."},
        "decision": {"type": ["string", "null"], "enum": ["approve", "ignore", None], "description": "review_proposal."},
        "message_id": {"type": ["string", "null"], "description": "review_proposal(source_type=email일 때) 또는 read_email."},
        "calendar_id": {"type": ["string", "null"], "description": "review_proposal, source_type=calendar일 때."},
        "event_id": {"type": ["string", "null"], "description": "review_proposal, source_type=calendar일 때."},
        "action": {"type": ["string", "null"], "enum": ["list", "create", "update", "delete", None], "description": "manage_tasks."},
        "title": {"type": ["string", "null"], "description": "manage_tasks, action=create일 때 할 일 제목."},
        "due_at": {"type": ["string", "null"], "description": "manage_tasks, action=create일 때 마감(ISO 8601), 선택."},
        "task_id": {"type": ["string", "null"], "description": "manage_tasks, action=update/delete일 때(현재는 지원 안 함)."},
    },
}

_ROUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reply", "skill_id", "arguments"],
    "properties": {
        "action": {"type": "string", "enum": ["reply", "call_skill"]},
        "reply": {"type": ["string", "null"]},
        "skill_id": {"type": ["string", "null"], "enum": [*_ROUTABLE_SKILL_IDS, None]},
        "arguments": {"anyOf": [{"type": "null"}, _ARGUMENTS_SCHEMA]},
    },
}

_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply"],
    "properties": {"reply": {"type": "string", "minLength": 1}},
}


_DEFAULT_CONTEXT_LIMIT = 10
"""호출자가 `context`를 안 보내면(오케스트레이터 어댑터 등) 서버가 대신 채우는
최근 회의·제안 개수 — 대화 이력을 최근 10턴까지만 기억하는 `_MAX_HISTORY_TURNS`와
같은 기준을 그대로 맞췄다(2026-08-17, 20번 문서 4단계·Q5)."""


def _default_meetings_context(user_id: str, *, limit: int = _DEFAULT_CONTEXT_LIMIT) -> list[dict[str, Any]]:
    """사용자의 최근 회의를 `_format_context`가 읽는 모양으로 서버에서 직접 조회한다.

    `app.meeting_api.list_meetings`은 FastAPI Route 함수지만 `user_id`를
    직접 인자로 주면 `Depends`를 거치지 않고 그대로 호출할 수 있다 — REST
    화면이 쓰는 것과 같은 Repository·같은 정렬(최근 생성순)을 그대로
    재사용한다. 순환 Import를 피하려고 호출 시점에만 가져온다(다른 지연
    Import와 같은 이유 — `app.meeting_api`가 `app.internal_chat`을 거쳐
    결국 이 모듈을 다시 Import한다).
    """

    from app.meeting_api import list_meetings

    return [
        {"meeting_id": item.meeting_id, "title": item.title, "has_analysis": item.has_analysis}
        for item in list_meetings(user_id=user_id)[:limit]
    ]


def _default_proposals_context(user_id: str, *, limit: int = _DEFAULT_CONTEXT_LIMIT) -> list[dict[str, Any]]:
    """사용자의 대기 중인 제안을 최근 순으로 최대 `limit`건 서버에서 직접 채운다.

    제안은 DB에 저장하지 않지만, 검토(승인/무시)되기 전까지는 `proposal_hub`
    (`app.proposal_api`)의 프로세스 메모리에 그대로 남아 있다 — Drawer가
    SSE로 받아 프론트 상태에 쌓아두는 것과 같은 원본 소스다. 이 함수는 그
    메모리를 REST/SSE 없이 바로 읽어, Drawer가 못 보내는(=Context가 없는)
    호출자에게도 같은 정보를 준다.
    """

    from app.proposal_api import proposal_hub

    recent = proposal_hub.recent(user_id, limit=limit)
    items: list[dict[str, Any]] = []
    for proposal in recent:
        item: dict[str, Any] = {"source_type": proposal.source_type, "title": proposal.title}
        if proposal.due_at is not None:
            item["due_at"] = proposal.due_at.isoformat()
        if proposal.source_type == "email":
            item["message_id"] = proposal.source_id
        else:
            metadata = proposal.metadata or {}
            item["calendar_id"] = metadata.get("calendar_id")
            item["event_id"] = metadata.get("event_id")
        items.append(item)
    return items


def _with_default_context(context: dict[str, Any] | None, user_id: str) -> dict[str, Any] | None:
    """호출자가 `meetings`/`proposals`를 안 보낸 자리만 서버 기본값으로 채운다.

    Drawer는 이미 화면 상태로 만든 Context를 보내므로 그 값을 그대로
    존중한다(동작 변화 없음) — 오케스트레이터 어댑터처럼 애초에 `context`를
    보내지 않는 호출자만 이 기본값의 실질적인 수혜자다. `pending_action_items`는
    "바로 앞 turn이 무엇을 제안했는지"라 서버가 다시 만들어낼 수 없어 그대로
    둔다.

    두 조회 모두 실패해도 요청 자체를 실패시키지 않는다 — Context는 지칭
    질문의 정확도를 높이는 보조 정보일 뿐이라, 회의 DB가 잠깐 말을 안
    듣는다고 "오늘 브리핑 보여줘" 같은 무관한 질문까지 함께 죽으면 안 된다
    (2026-08-17, 실제로 `WORKMATE_MEETING_DB_PATH`가 잘못된 경로일 때
    `_with_default_context(None, ...)` 자체가 `FileNotFoundError`로 죽는 것을
    재현해 확인했다). 실패하면 그 자리만 빈 목록으로 남기고 조용히 넘어간다.
    """

    result = dict(context) if context else {}
    if not result.get("meetings"):
        try:
            result["meetings"] = _default_meetings_context(user_id)
        except Exception:
            result.setdefault("meetings", [])
    if not result.get("proposals"):
        try:
            result["proposals"] = _default_proposals_context(user_id)
        except Exception:
            result.setdefault("proposals", [])
    return result or None


def _format_context(context: dict[str, Any] | None) -> str:
    if not context:
        return "(회의·제안 Context 없음)"
    lines: list[str] = []
    meetings = context.get("meetings") or []
    if meetings:
        lines.append("최근 회의:")
        for item in meetings:
            analyzed = item.get("has_analysis")
            status = "분석 완료(저장된 요약 있음)" if analyzed else "분석 대기(아직 분석 안 함)" if analyzed is False else "분석 여부 불명"
            lines.append(f"  - meeting_id={item.get('meeting_id')}: {item.get('title')} [{status}]")
    proposals = context.get("proposals") or []
    if proposals:
        lines.append("대기 중인 제안:")
        for item in proposals:
            ref = f"message_id={item.get('message_id')}" if item.get("source_type") == "email" else f"calendar_id={item.get('calendar_id')} event_id={item.get('event_id')}"
            due_at = item.get("due_at")
            when = f", 일정 시각={due_at}" if due_at else ""
            lines.append(f"  - {ref}: {item.get('title')}{when}")
    pending_action_items = context.get("pending_action_items") or []
    if pending_action_items:
        # 바로 앞 assistant 응답이 "이 Action Item을 할 일로 추가할까요?"
        # 확인 배너로 나열한 action_item_id를 그 순서 그대로 담고 있다 —
        # 사용자가 배너 버튼 대신 "1번만 할일에 추가해"처럼 자유 문장으로
        # 답할 때, 이 순서(1-based)로 action_item_id를 정확히 옮기라고
        # review_action_items 카탈로그 항목이 참조한다(2026-08-17, 실사용
        # 중 발견 — 이 정보가 없어 manage_tasks로 잘못 빠지며 회의 근거
        # 링크가 끊기는 버그의 원인이었다).
        lines.append("방금 제안한 Action Item 후보(번호 순서):")
        for index, item in enumerate(pending_action_items, start=1):
            lines.append(f"  {index}) meeting_id={item.get('meeting_id')} action_item_id={item.get('action_item_id')}")
    return "\n".join(lines) if lines else "(회의·제안 Context 없음)"


_MATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matched_index"],
    "properties": {"matched_index": {"type": ["integer", "null"]}},
}


def _resolve_context_reference(
    text: str,
    context: dict[str, Any] | None,
    *,
    history: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """사용자 문장이 Context(회의·제안) 중 어떤 항목을 가리키는지 전용 LLM 호출로 판단한다.

    처음엔 문자열 유사도(최장 공통 부분 문자열)로 후보를 미리 찾았는데, 조사·어미가
    다르거나 아예 다른 동의어("신입사원 환영회" vs Context의 "신규 입사자 환영회")로
    표현되면 여전히 놓쳤다(2026-08-17, 실사용 피드백 — "조사 하나 바뀌었다고 못 찾은 게
    확실하다"). 문자열 겹침 자체가 신뢰할 근거가 아니었다. Routing·인자 추출과 같은
    프롬프트에서 판단하게 하는 대신, "이 문장이 이 목록 중 무엇을 가리키는가"만 묻는
    별도의 작고 집중된 LLM 호출로 분리했더니 훨씬 안정적으로 찾았다(재현 시나리오 20회
    중 실패 1회 → 그 이하로 개선, `tests/test_assistant_router.py` 참고).

    `history`도 함께 준다 — "신입사원 환영회 언제?" 다음에 "신입사원"만 다시 보내는
    식의 짧은 후속 메시지는, 이전 턴 없이는 이 매칭 호출도 확신하지 못하고 매번
    "일치 항목 없음"으로 답했다(2026-08-17, 대화 이력 없이 매 요청을 독립적으로
    처리하던 문제의 연장선).
    """

    if not context:
        return None
    candidates: list[dict[str, Any]] = [{"kind": "meeting", "item": item} for item in context.get("meetings") or []]
    candidates.extend({"kind": "proposal", "item": item} for item in context.get("proposals") or [])
    if not candidates:
        return None
    listing = "\n".join(f"{index}: {candidate['item'].get('title')}" for index, candidate in enumerate(candidates))
    system = (
        "아래는 번호가 붙은 회의·제안 제목 목록이다. 대화 이력이 있으면 마지막 사용자 "
        "메시지가 짧거나 모호해도 이전 turn의 맥락을 이어서 해석하라. 사용자의 (이력을 "
        "반영한) 최종 의도가 목록 중 하나를 가리키면 그 번호를 matched_index에 적어라 — "
        '조사·어미가 다르거나 동의어로 표현돼도(예: "신입사원 환영회"와 "신규 입사자 '
        '환영회"는 같은 대상) 같은 대상을 가리키면 찾아라. 가리키는 항목이 없거나 '
        "확신할 수 없으면 matched_index를 null로 둬라.\n\n"
        f"[목록]\n{listing}"
    )
    result = _chat_completion(
        [{"role": "system", "content": system}, *_history_messages(history), {"role": "user", "content": text}],
        schema=_MATCH_SCHEMA,
        schema_name="assistant_context_match",
        strict=True,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    index = result.get("matched_index")
    if not isinstance(index, int) or not (0 <= index < len(candidates)):
        return None
    return candidates[index]


def _format_match_hint(match: dict[str, Any] | None) -> str:
    if match is None:
        return ""
    item = match["item"]
    if match["kind"] == "meeting":
        detail = f"meeting_id={item.get('meeting_id')}, 제목={item.get('title')}"
    else:
        ref = f"message_id={item.get('message_id')}" if item.get("source_type") == "email" else f"calendar_id={item.get('calendar_id')} event_id={item.get('event_id')}"
        due_at = item.get("due_at")
        when = f", 일정 시각={due_at}" if due_at else ""
        detail = f"{ref}, 제목={item.get('title')}{when}"
    return (
        f"\n\n[Context 항목 확인됨] 사용자 문장이 가리키는 항목을 확인했다 — {detail}. "
        "이 정보를 반드시 사용해서 답하라(정보가 없다고 하지 마라)."
    )


def _today(timezone_name: str) -> str:
    """`timezone_name` 현지 기준 오늘 날짜(YYYY-MM-DD)를 반환한다.

    이 값이 없으면 LLM이 "지난 주"·"이번 달"처럼 오늘을 기준으로 하는 상대
    시점을 전혀 계산할 수 없다(2026-08-17, 실사용 중 발견 — Docstring 참고).
    """

    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name)).date().isoformat()


_MAX_HISTORY_TURNS = 10


def _history_messages(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """대화 이력을 실제 Chat 메시지(user/assistant 교대)로 바꾼다.

    Drawer가 매 요청을 독립적으로(대화 이력 없이) 보내던 게 문제였다(2026-08-17,
    실사용 중 발견) — "신입사원 환영회 언제?" 다음에 짧게 "신입사원"만 다시
    보내면, 이전 턴 없이는 이 한 단어가 "언제인지 묻는 후속 질문"인지 "이
    제안을 어떻게 하라는 요청"인지 LLM이 알 도리가 없어 엉뚱하게 승인/무시를
    물어봤다. `_MAX_HISTORY_TURNS`로 최근 턴만 잘라 토큰 사용량을 제한한다.
    """

    messages: list[dict[str, str]] = []
    for turn in (history or [])[-_MAX_HISTORY_TURNS:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text_value = str(turn.get("text") or "").strip()
        if text_value:
            messages.append({"role": role, "content": text_value})
    return messages


def route(
    text: str,
    *,
    context: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    timezone_name: str = "Asia/Seoul",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> RoutedAction:
    """사용자 문장을 보고 어떤 Skill을 어떤 인자로 부를지(또는 직접 답할지) 정한다."""

    system = (
        "당신은 Workmate 업무 비서다. 아래 Skill 중 하나가 필요하면 "
        'action="call_skill"과 skill_id·arguments를 채우고, 잡담이거나 정보가 '
        '부족해 되물어야 하면 action="reply"와 reply만 채워라. reply는 두 경우 '
        "모두 채운다 — call_skill일 땐 무엇을 하려는지 한 문장으로 요약해라(예: "
        "\"'신규 입사자 환영회' 제안을 무시할게요\") — 실행 전 사용자에게 그대로 "
        "보여줄 확인 문구로 쓰인다.\n\n"
        "**대화 이력 사용 원칙**: 대화 이력은 마지막 사용자 메시지가 짧거나 모호할 때"
        "(예: 단어 하나, \"그거\", 번호 하나) **무엇을 가리키는지 판단하는 용도로만**"
        " 써라 — 예를 들어 직전에 회의 후보 목록을 보여줬는데 사용자가 \"1\"이라고만 "
        "답하면 그 목록의 1번을 가리키는 것으로 이어서 해석하라. 하지만 새 질문이 "
        "이전 turn과 **주제 자체가 다르면**(예: 방금 회의를 분석했는데 이번엔 전혀 "
        "다른 메일 내용을 묻는 경우) 이전 turn에서 나온 요약·내용을 이번 답에 절대 "
        "가져다 쓰지 마라 — 그건 다른 질문에 대한 답이었을 뿐, 이번 질문의 사실 정보가 "
        "아니다. 이번 질문에 필요한 사실은 오직 아래 [Context]에 있는 것만 근거로 "
        "삼아라 — 이전 대화에서 봤다고 해서 다시 답에 써도 되는 게 아니다. **주제가 "
        "같아도 마찬가지다**: 사용자가 회의 분석처럼 어떤 Skill이 실제로 수행해야 할 "
        "동작을 요청하면, 이전 turn에 비슷하거나 심지어 같은 회의에 대한 분석 결과가 "
        "이미 나와 있었더라도 그 내용을 그대로 재사용해 action=\"reply\"로 답하지 "
        "마라 — 그건 이번 요청을 실제로 처리한 게 아니라 예전 답을 베낀 것일 뿐이다. "
        "이런 요청은 반드시 call_skill로 그 Skill을 실제로 호출해야 한다(이미 분석된 "
        "회의라면 analyze_meeting이 아니라 get_meeting_analysis를 호출해 저장된 결과를 "
        "실제로 가져오는 것도 여기 포함된다 — 기억으로 재구성해 답하는 것과는 다르다). "
        "항상 한국어로 답하라.\n\n"
        f"[오늘 날짜] {_today(timezone_name)} ({timezone_name}) — '지난 주'·'이번 달' 같은 "
        "상대 시점은 이 날짜를 기준으로 직접 계산해서 채워라.\n\n"
        f"[사용 가능한 Skill]\n{_SKILL_CATALOG}\n[Context]\n{_format_context(context)}"
        f"{_format_match_hint(_resolve_context_reference(text, context, history=history, base_url=base_url, api_key=api_key, model=model))}"
    )
    result = _chat_completion(
        [{"role": "system", "content": system}, *_history_messages(history), {"role": "user", "content": text}],
        schema=_ROUTE_SCHEMA,
        schema_name="assistant_route",
        strict=True,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    action = result.get("action")
    if action not in {"reply", "call_skill"}:
        raise AssistantRouterError(f"router response has an invalid action: {result!r}")
    skill_id = result.get("skill_id")
    if action == "call_skill" and skill_id not in _ROUTABLE_SKILL_IDS:
        raise AssistantRouterError(f"router selected an unknown skill_id: {skill_id!r}")
    raw_arguments = result.get("arguments")
    # 평평한 Schema라 고르지 않은 필드까지 `null`로 채워져 돌아온다 —
    # 실제 값이 있는 필드만 남긴다(서브 Skill Payload를 깨끗하게 유지).
    arguments = {key: value for key, value in raw_arguments.items() if value is not None} if isinstance(raw_arguments, dict) else None
    return RoutedAction(
        action=action,
        reply=result.get("reply") if isinstance(result.get("reply"), str) else None,
        skill_id=skill_id if action == "call_skill" else None,
        arguments=arguments if action == "call_skill" else None,
    )


def phrase_answer(
    text: str,
    skill_id: str,
    result_data: Any,
    *,
    markdown: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> str:
    """Skill 실행 결과(JSON)를 사용자 질문에 대한 자연어 한국어 답으로 바꾼다.

    `markdown`은 참고용 원문이다 — weekly_report의 "주요 회의 내용"처럼, 공개
    A2A 계약(구조화 `data` Schema)에는 없지만 Skill이 자체 Markdown 본문에만
    실어 둔 사실이 있다(`app/workflows/weekly_report.py`의
    `_render_weekly_report_markdown()` 참고 — 공개 계약을 이 저장소 혼자
    넓히지 않으려고 의도적으로 `data`에는 안 넣었다). 그래서 챗봇이 구조화
    `data`만 보고 답하면 이 내용을 통째로 답하지 못했다(2026-08-17, 실사용
    중 발견). `data`에 없어도 이 Markdown에 있는 사실은 답에 써도 된다 —
    둘 다에 없는 내용만 지어내지 마라.
    """

    system = (
        "아래 JSON은 사용자 요청을 처리해 얻은 결과다. 이 JSON과, 함께 주어지면 참고용 "
        "Markdown 원문에 있는 사실만 근거로 정중하고 간결한 한국어로 답하라. 즉 Markdown에만 "
        "있고 JSON에는 없는 사실(예: 회의 요약처럼 구조화 필드가 없는 내용)도 답에 써도 "
        "된다 — 둘 다에 없는 내용만 지어내지 마라. 날짜·시각 필드는 ISO 8601 "
        '원문(예: "2026-08-21T18:00:00+09:00")을 그대로 옮기지 말고 "2026년 8월 21일 오후 6시"처럼 '
        "자연스러운 한국어로 바꿔서 답하라."
    )
    user = f"사용자 질문: {text}\n\nSkill: {skill_id}\n결과 JSON:\n{json.dumps(result_data, ensure_ascii=False, default=str)}"
    if markdown:
        user += f"\n\n참고용 Markdown 원문:\n{markdown}"
    result = _chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        schema=_ANSWER_SCHEMA,
        schema_name="assistant_answer",
        strict=True,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    reply = result.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise AssistantRouterError("router answer response is empty")
    return reply.strip()


def _default_proposal_task(user_id: str, context: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """`review_proposal` 승인에 필요한 `task` 필드를 LLM 대신 채운다.

    `review_proposal_workflow`는 승인 시 `task.assignee_user_id`가 호출자
    본인과 다르면 403을 낸다 — 이 값은 LLM이 고를 필요 없이 항상
    `request_.user_id`로 결정적이다. 제목은 Context로 이미 아는 제안 원본
    제목을 그대로 쓴다 — 구조화 Drawer의 `defaultTaskInput()`
    (`lib/proposal-review.ts`)과 같은 동작이다. `strict: true` JSON Schema로는
    이런 필드를 LLM에 안전하게 맡기기 어려워(제목을 지어낼 위험) 여기서 채운다.
    """

    title = "제안된 업무"
    if context and isinstance(context.get("proposals"), list):
        for item in context["proposals"]:
            if arguments.get("source_type") == "email" and item.get("message_id") == arguments.get("message_id"):
                title = str(item.get("title") or title)
                break
            if arguments.get("source_type") == "calendar" and item.get("calendar_id") == arguments.get("calendar_id") and item.get("event_id") == arguments.get("event_id"):
                title = str(item.get("title") or title)
                break
    return {"title": title, "assignee_user_id": user_id, "due_at": None, "priority_hint": None}


async def assistant_ask_workflow(request_: WorkflowRequest) -> WorkflowResult:
    """자연어 채팅 한 턴을 처리하는 Skill Workflow — Drawer의 Skill 드롭다운을 대체한다.

    `text`만 있으면 새 질문으로 보고 `route()`부터 시작한다. `confirmed_skill_id`가
    있으면(사용자가 확인 배너에서 "확인"을 눌러 재전송한 경우) `route()`를 다시
    묻지 않고 그 Skill·인자를 그대로 실행한다 — 파괴적 동작(Action Item 거절·
    제안 무시·할 일 삭제)을 두 번째 LLM 판단에 다시 맡기지 않기 위해서다.
    """

    payload = request_.payload
    # `text`가 우선이다(기존 계약) — 없으면 `message`/`input`으로 폴백한다.
    # `m51_legacy_adapter.py::_workmate_request()`가 레거시 Orchestrator의
    # `message` Payload를 `text`로 변환해 보내던 것과 같은 호환 로직인데,
    # 그 어댑터를 거치지 않고 오케스트레이터가 workmate-agent에 직접
    # `skill_id=assistant_ask`로 붙는 경로(2026-08-18)에서는 그 변환이 없어
    # 여기서 직접 받아준다 — 오케스트레이터 쪽(`a2a_client.py`)의 `data` Part
    # 구성을 바꿀 필요가 없어진다.
    text = str(payload.get("text") or payload.get("message") or payload.get("input") or "").strip()
    if not text:
        raise ValueError("text is required")
    context = payload.get("context") if isinstance(payload.get("context"), dict) else None
    history = payload.get("history") if isinstance(payload.get("history"), list) else None
    timezone_name = str(payload.get("timezone") or "Asia/Seoul")
    confirmed_skill_id = payload.get("confirmed_skill_id")
    confirmed_arguments = payload.get("confirmed_arguments")

    if confirmed_skill_id:
        if confirmed_skill_id not in _ROUTABLE_SKILL_IDS:
            raise ValueError(f"unknown confirmed_skill_id: {confirmed_skill_id}")
        skill_id = str(confirmed_skill_id)
        arguments = confirmed_arguments if isinstance(confirmed_arguments, dict) else {}
    else:
        # `confirmed_skill_id` 재전송 때는 route()를 다시 안 부르므로(위 Docstring)
        # 여기서만 기본 Context를 채운다 — 매 확인 재전송마다 회의·제안을 다시
        # 조회하는 낭비를 피한다.
        context = _with_default_context(context, request_.user_id)
        routed = route(text, context=context, history=history, timezone_name=timezone_name)
        if routed.action == "reply" or routed.skill_id is None:
            reply = routed.reply or "죄송해요, 다시 한 번 말씀해 주시겠어요?"
            return WorkflowResult(
                artifact_name="assistant_ask",
                artifact_description="Natural-language reply with no skill call.",
                text=json.dumps({"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}}, ensure_ascii=False),
                data={"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}},
                markdown=reply,
                mock=False,
            )
        skill_id = routed.skill_id
        arguments = routed.arguments or {}
        # 2026-08-19 실사용 중 발견 — Context에 `[분석 대기]`(has_analysis=False)로
        # 명시된 회의인데도 LLM이 get_meeting_analysis를 골라, 저장된 분석이 없는데
        # 있는 것처럼 답을 지어낸 사례가 오케스트레이터 경유 호출에서 재현됐다
        # (같은 입력을 반복해도 매번 같은 결과가 보장되지 않는 LLM 라우팅의 한계).
        # get_meeting_analysis는 읽기 전용이라 `_requires_confirmation()`을 안 거쳐
        # 바로 실행되므로(analyze_meeting과 달리 확인 단계가 없다), 프롬프트 지시만
        # 믿지 않고 여기서 Context의 실제 has_analysis 값으로 직접 재검증한다 —
        # 근거 없는 호출을 실행 전에 막는 마지막 방어선이다.
        if skill_id == "get_meeting_analysis":
            target_meeting_id = arguments.get("meeting_id")
            meetings = (context or {}).get("meetings") or []
            target_meeting = next((item for item in meetings if item.get("meeting_id") == target_meeting_id), None)
            if target_meeting is None:
                reply = "어떤 회의의 분석 결과를 찾으시는지 특정할 수 없습니다 — 회의 제목이나 날짜를 알려주세요."
                return WorkflowResult(
                    artifact_name="assistant_ask",
                    artifact_description="Declined an ungrounded get_meeting_analysis call (unknown meeting_id).",
                    text=json.dumps({"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}}, ensure_ascii=False),
                    data={"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}},
                    markdown=reply,
                    mock=False,
                )
            if target_meeting.get("has_analysis") is not True:
                # 아직 분석 안 한 회의를 잘못 골랐다 — 조용히 진행하지 않고
                # analyze_meeting으로 바꿔 실행 전 확인을 받게 한다(그 Skill은
                # 이미 `_requires_confirmation()` 대상이라 아래에서 자동으로 확인
                # 배너가 붙는다).
                skill_id = "analyze_meeting"
                arguments = {"meeting_id": target_meeting_id}
                routed = RoutedAction(
                    action="call_skill",
                    reply=f"'{target_meeting.get('title')}' 회의는 아직 분석하지 않았습니다 — 지금 분석할까요?",
                    skill_id=skill_id,
                    arguments=arguments,
                )
        if skill_id == "review_proposal" and arguments.get("decision") == "approve":
            arguments = {**arguments, "task": _default_proposal_task(request_.user_id, context, arguments)}
        if skill_id == "manage_tasks" and arguments.get("action") not in _ALLOWED_MANAGE_TASKS_ACTIONS:
            reply = "할 일 수정·삭제는 '할 일 관리' 화면에서 해주세요 — 여기서는 조회·등록만 도와드릴 수 있어요."
            return WorkflowResult(
                artifact_name="assistant_ask",
                artifact_description="Declined an out-of-scope manage_tasks action.",
                text=json.dumps({"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}}, ensure_ascii=False),
                data={"type": "assistant_reply", "data": {"reply": reply, "executed_skill_id": None, "pending_action": None}},
                markdown=reply,
                mock=False,
            )
        if _requires_confirmation(skill_id, arguments):
            confirm_reply = routed.reply or f"'{skill_id}'를 요청하신 내용대로 실행할까요?"
            pending_action = {"skill_id": skill_id, "arguments": arguments}
            return WorkflowResult(
                artifact_name="assistant_ask",
                artifact_description="Action proposed; awaiting confirmation before it runs.",
                text=json.dumps({"type": "assistant_reply", "data": {"reply": confirm_reply, "executed_skill_id": None, "pending_action": pending_action}}, ensure_ascii=False),
                data={"type": "assistant_reply", "data": {"reply": confirm_reply, "executed_skill_id": None, "pending_action": pending_action}},
                markdown=confirm_reply,
                mock=False,
            )

    # 순환 Import를 피하려고 호출 시점에만 가져온다 — `app.a2a.runtime`도 이
    # 모듈의 `assistant_ask_workflow`를 등록하려고 이 모듈을 Import한다.
    from app.a2a.runtime import workflow_registry

    sub_request = WorkflowRequest(
        skill_id=skill_id,
        task_id=request_.task_id,
        thread_id=request_.thread_id,
        message_id=request_.message_id,
        user_id=request_.user_id,
        payload={**arguments, "user_id": request_.user_id},
    )
    sub_result = await workflow_registry().execute(sub_request)
    result_data = sub_result.data.get("data") if sub_result.data else sub_result.text
    reply = phrase_answer(text, skill_id, result_data, markdown=sub_result.markdown)

    # 회의 분석(또는 저장된 분석 재조회) 뒤 아직 검토 안 한 Action Item이 있으면
    # 할 일로 추가할지 먼저 물어본다(2026-08-17, 사용자 요청 — Human-in-the-loop).
    # 자동으로 Task를 만들지 않는다 — `_requires_confirmation()`의 확인
    # 메커니즘과 같은 pending_action을 그대로 재사용한다(Drawer가 이미 확인
    # 배너를 그릴 줄 안다). "확인"을 누르면 대기 항목을 전부 승인한다 — 일부만
    # 고르고 싶으면 채팅으로 "2번만 승인해줘"처럼 다시 요청하면 된다.
    pending_action = _offer_to_add_action_items_as_tasks(skill_id, result_data)
    if pending_action is not None:
        reply = f"{reply}\n\n이 Action Item을 할 일로 추가할까요?"

    envelope = {"reply": reply, "executed_skill_id": skill_id, "pending_action": pending_action}
    return WorkflowResult(
        artifact_name="assistant_ask",
        artifact_description="Natural-language reply backed by a skill result.",
        text=json.dumps({"type": "assistant_reply", "data": envelope}, ensure_ascii=False),
        data={"type": "assistant_reply", "data": envelope},
        markdown=reply,
        warnings=sub_result.warnings,
        mock=False,
    )


def _offer_to_add_action_items_as_tasks(skill_id: str, result_data: Any) -> dict[str, Any] | None:
    """`analyze_meeting`·`get_meeting_analysis` 결과에 아직 검토 안 한(pending)
    Action Item이 있으면, 전부 승인(=할 일로 등록)하는 `review_action_items`
    확인 요청을 만든다. 없으면 `None`을 돌려줘 확인 없이 답만 보여준다."""

    if skill_id not in ("analyze_meeting", "get_meeting_analysis") or not isinstance(result_data, dict):
        return None
    meeting_id = result_data.get("meeting_id")
    action_items = result_data.get("action_items")
    if not meeting_id or not isinstance(action_items, list):
        return None
    pending_ids = [item.get("action_item_id") for item in action_items if isinstance(item, dict) and item.get("approval_status") == "pending" and item.get("action_item_id")]
    if not pending_ids:
        return None
    return {
        "skill_id": "review_action_items",
        "arguments": {"meeting_id": meeting_id, "decisions": [{"action_item_id": action_item_id, "decision": "approve"} for action_item_id in pending_ids]},
    }


__all__ = ["AssistantRouterError", "RoutedAction", "assistant_ask_workflow", "phrase_answer", "route"]
