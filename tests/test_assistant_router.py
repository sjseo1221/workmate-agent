"""자연어 Router(`app/workflows/assistant_router.py`) 계약 테스트.

`route()`·`phrase_answer()`는 `app/email_action_items.py` 테스트와 같은 방식으로
`request.urlopen`을 Fake Response로 바꿔 실제 네트워크 호출 없이 검증한다.
`assistant_ask_workflow()`는 `route`/`phrase_answer`/`workflow_registry`를 직접
Monkeypatch해 Routing 판단과 Skill 실행 경계를 분리해서 검증한다.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.workflows import assistant_router
from app.workflows.assistant_router import (
    AssistantRouterError,
    RoutedAction,
    _default_proposal_task,
    _offer_to_add_action_items_as_tasks,
    _resolve_context_reference,
    assistant_ask_workflow,
    phrase_answer,
    route,
)
from app.workflows.registry import WorkflowRequest, WorkflowResult


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._body).encode()


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


def _request(**payload) -> WorkflowRequest:
    return WorkflowRequest(
        skill_id="assistant_ask",
        task_id="task-1",
        thread_id="task-1",
        message_id="msg-1",
        user_id="user-a",
        payload=payload,
    )


class RouteTests(unittest.TestCase):
    def test_call_skill_response_is_parsed(self) -> None:
        response = FakeResponse(_completion({"action": "call_skill", "reply": None, "skill_id": "daily_briefing", "arguments": {}}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            routed = route("오늘 브리핑 보여줘", api_key="test")
        self.assertEqual(routed.action, "call_skill")
        self.assertEqual(routed.skill_id, "daily_briefing")
        self.assertEqual(routed.arguments, {})

    def test_reply_response_carries_no_skill(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "안녕하세요!", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            routed = route("안녕", api_key="test")
        self.assertEqual(routed.action, "reply")
        self.assertEqual(routed.reply, "안녕하세요!")
        self.assertIsNone(routed.skill_id)

    def test_unknown_skill_id_raises(self) -> None:
        response = FakeResponse(_completion({"action": "call_skill", "reply": None, "skill_id": "delete_everything", "arguments": {}}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            with self.assertRaises(AssistantRouterError):
                route("전부 지워줘", api_key="test")

    def test_invalid_action_raises(self) -> None:
        response = FakeResponse(_completion({"action": "maybe", "reply": None, "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            with self.assertRaises(AssistantRouterError):
                route("음", api_key="test")

    def test_prompt_forbids_reusing_previous_turn_content_for_an_unrelated_new_question(self) -> None:
        """실사용 중 발견한 버그(2026-08-17) — 회의를 분석한 직후 전혀 다른 주제
        ("시즌 패스 보상 지급 로직 변경 관련 메일 내용 요약해줘")를 물었는데, 방금
        분석한 회의 요약을 그 메일 내용인 것처럼 그대로 재사용해 답했다. 대화
        이력은 짧은 후속 문장이 무엇을 가리키는지 판단하는 데만 쓰고, 주제가
        다른 새 질문에 이전 turn의 내용을 사실처럼 가져다 쓰면 안 된다는 지침이
        System Prompt에 있는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("주제 자체가 다르면", system_message)
        self.assertIn("절대 가져다 쓰지 마라", system_message)

    def test_prompt_forbids_reusing_previous_analysis_for_a_same_topic_action_request(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "오늘 한 회의 분석해줘"라고
        새로 요청했는데, call_skill로 실제 회의를 분석/조회하지 않고 이전 turn에 이미
        나온(다른) 회의 분석 결과를 그대로 action="reply"로 재활용해 답했다. 주제가
        같아도(둘 다 "회의 분석") 실행형 요청은 반드시 call_skill로 실제 Skill을
        호출해야 한다는 지침이 System Prompt에 있는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("주제가 같아도 마찬가지다", system_message)
        self.assertIn("예전 답을 베낀 것일 뿐이다", system_message)

    def test_history_turns_are_sent_as_real_chat_messages(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "싱잉사원 환영회 언제?" 다음에
        짧게 "신입사원"만 다시 보내면, 대화 이력 없이는 새 대화로 오해받아 엉뚱하게 답했다.
        `history`가 실제 user/assistant Chat 메시지로 함께 전송되는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        history = [
            {"role": "user", "text": "싱잉사원 환영회 언제?"},
            {"role": "assistant", "text": "'싱잉사원 환영회' 일정 정보가 없습니다."},
        ]
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("신입사원", history=history, api_key="test")
        sent_messages = json.loads(mocked.call_args.args[0].data)["messages"]
        self.assertEqual(sent_messages[1], {"role": "user", "content": "싱잉사원 환영회 언제?"})
        self.assertEqual(sent_messages[2], {"role": "assistant", "content": "'싱잉사원 환영회' 일정 정보가 없습니다."})
        self.assertEqual(sent_messages[-1], {"role": "user", "content": "신입사원"})

    def test_history_is_capped_to_the_most_recent_turns(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        history = [{"role": "user", "text": f"메시지 {i}"} for i in range(15)]
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("마지막 질문", history=history, api_key="test")
        sent_messages = json.loads(mocked.call_args.args[0].data)["messages"]
        # system(1) + 최근 10턴 + 이번 질문(1) = 12개.
        self.assertEqual(len(sent_messages), 12)
        self.assertEqual(sent_messages[1]["content"], "메시지 5")

    def test_blank_history_turns_are_skipped(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        history = [{"role": "user", "text": "   "}, {"role": "assistant", "text": "실제 답변"}]
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("질문", history=history, api_key="test")
        sent_messages = json.loads(mocked.call_args.args[0].data)["messages"]
        contents = [message["content"] for message in sent_messages]
        self.assertNotIn("   ", contents)
        self.assertIn("실제 답변", contents)

    def test_no_history_means_only_system_and_the_current_message(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("질문", api_key="test")
        sent_messages = json.loads(mocked.call_args.args[0].data)["messages"]
        self.assertEqual(len(sent_messages), 2)

    def test_context_meetings_and_proposals_are_included_in_the_prompt(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {
            "meetings": [{"meeting_id": "m-1", "title": "스프린트 리뷰"}],
            "proposals": [{"proposal_id": "p-1", "title": "PayPal 확인 메일", "source_type": "email", "message_id": "msg-9"}],
        }
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("스프린트 리뷰 분석해줘", context=context, api_key="test")
        sent_body = json.loads(mocked.call_args.args[0].data)
        system_message = sent_body["messages"][0]["content"]
        self.assertIn("스프린트 리뷰", system_message)
        self.assertIn("msg-9", system_message)

    def test_meeting_analysis_status_is_shown_in_the_prompt(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Context에 분석 여부가
        없어 LLM이 "아직 분석 안 한 회의를 분석하겠습니다"처럼 근거 없이 단정했다.
        회의별 분석 완료·대기·불명 상태가 System Prompt에 실제로 들어가는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {
            "meetings": [
                {"meeting_id": "m-1", "title": "이미 분석된 회의", "has_analysis": True},
                {"meeting_id": "m-2", "title": "아직 분석 안 한 회의", "has_analysis": False},
                {"meeting_id": "m-3", "title": "분석 여부 모르는 회의"},
            ],
        }
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", context=context, api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("이미 분석된 회의 [분석 완료(저장된 요약 있음)]", system_message)
        self.assertIn("아직 분석 안 한 회의 [분석 대기(아직 분석 안 함)]", system_message)
        self.assertIn("분석 여부 모르는 회의 [분석 여부 불명]", system_message)

    def test_prompt_prefers_get_meeting_analysis_over_reanalyzing(self) -> None:
        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("반드시 이\n  Skill을 먼저 쓴다", system_message)
        self.assertIn("이 Skill을 고르지 마라", system_message)

    def test_prompt_requires_confirmation_replies_to_name_the_specific_target(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "오늘 한 회의 분석해줘"에
        대한 확인 배너가 "분석 대기인 회의를 분석하겠습니다"처럼 어떤 회의인지 전혀
        말하지 않았다. call_skill의 reply도 Context에서 고른 구체적인 대상(제목·
        날짜·시각 등)을 적어야 한다는 지침이 System Prompt에 있는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("call_skill을 고를 때도 reply는 채워야 한다", system_message)
        self.assertIn("'8월 17일 17:31'\n  회의를 분석하겠습니다", system_message)

    def test_pending_action_items_are_shown_in_the_prompt_in_order(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "이 Action Item을 할
        일로 추가할까요?" 확인 배너에 "1번만 할일에 추가해"처럼 자유 문장으로
        답하면, action_item_id를 몰라 review_action_items 대신 manage_tasks(수동
        생성)로 잘못 빠지며 회의 근거 링크(meeting_evidence)가 끊겼다. Context의
        pending_action_items가 번호 순서 그대로 프롬프트에 들어가는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {
            "pending_action_items": [
                {"meeting_id": "m-1", "action_item_id": "ai-1"},
                {"meeting_id": "m-1", "action_item_id": "ai-2"},
            ],
        }
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("1번만 할일에 추가해", context=context, api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("1) meeting_id=m-1 action_item_id=ai-1", system_message)
        self.assertIn("2) meeting_id=m-1 action_item_id=ai-2", system_message)
        self.assertIn("manage_tasks(action=create)로 대신 새\n  할 일을 만들지 마라", system_message)

    def test_review_action_items_catalog_covers_content_based_selection_too(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — 번호("1번만") 지칭은
        고쳤지만, "가이드 공유하는 거만 추가해줘"처럼 내용으로 지칭하면 여전히
        같은 함정(manage_tasks로 잘못 빠져 회의 근거 링크가 끊김)에 빠졌다.
        카탈로그가 내용 기반 지칭도 pending_action_items로 옮기라고 명시하는지
        확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("내용으로", system_message)
        self.assertIn("가이드 공유하는 거만", system_message)

    def test_read_email_catalog_entry_is_present_and_routable(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Context의 제안 목록에는
        이메일 제목뿐이라 "메일 내용 요약해줘"에 제목을 그대로 되풀이하는 답만
        나왔다. 실제 본문을 다시 가져오는 read_email이 카탈로그에 있고
        skill_id로 고를 수 있는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        payload = json.loads(mocked.call_args.args[0].data)
        system_message = payload["messages"][0]["content"]
        self.assertIn("read_email", system_message)
        self.assertIn("title을 그대로 되풀이해", system_message)
        skill_id_enum = payload["response_format"]["json_schema"]["schema"]["properties"]["skill_id"]["enum"]
        self.assertIn("read_email", skill_id_enum)

    def test_read_email_catalog_requires_mentioning_the_30_day_search_window(self) -> None:
        """사용자 요청(2026-08-17) — 제안함에 없는 메일도 제목으로 메일함 전체를
        검색해 읽어오되, 검색 범위(최근 30일)를 답변에도 항상 명시해야 한다.
        카탈로그가 이를 명시하는지, query 인자 설명에도 30일 제한이 있는지
        확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        payload = json.loads(mocked.call_args.args[0].data)
        system_message = payload["messages"][0]["content"]
        self.assertIn("최근\n  30일 내 메일만 검색한다", system_message)
        self.assertIn("이 30일 제한을 반드시 함께\n  언급해라", system_message)
        query_description = payload["response_format"]["json_schema"]["schema"]["properties"]["arguments"]["anyOf"][1]["properties"]["query"]["description"]
        self.assertIn("최근 30일", query_description)

    def test_read_email_catalog_requires_keywords_not_the_full_sentence(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — 질문 원문을 통째로
        query에 넣었더니 "찾아줘"·"언제"·"있는지" 같은 메일에 없는 단어까지
        섞여, 공백으로 구분된 모든 단어를 AND로 요구하는 Gmail 검색이 실제로
        존재하는 메일도 못 찾았다("v2.4.0 업데이트 정기 배포 및 DB 마이그레이션
        작업은 언제 예정돼 있는지 메일에서 찾아줘" → 0건). 카탈로그가 핵심
        키워드 2~5개로 줄이라고 명시하는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("핵심\n  키워드 2~5개만 골라야 한다", system_message)
        self.assertIn("사용자 문장 전체를 그대로 복사해 넣지", system_message)

    def test_calendar_proposal_due_at_is_included_in_the_prompt(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "신규 입사자 환영회 언제인지
        캘린더에서 찾아줘"처럼 순수 조회 질문인데도, Context에 일정 시각이 전혀 없어 LLM이
        `review_proposal`(decision=ignore)을 잘못 골랐다. Context에 `due_at`이 들어가야
        LLM이 직접 답할 수 있다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {"proposals": [{"proposal_id": "p-1", "title": "신규 입사자 환영회", "source_type": "calendar", "calendar_id": "primary", "event_id": "evt-1", "due_at": "2026-08-21T18:00:00+09:00"}]}
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("신규 입사자 환영회 언제인지 캘린더에서 찾아줘", context=context, api_key="test")
        sent_body = json.loads(mocked.call_args.args[0].data)
        system_message = sent_body["messages"][0]["content"]
        self.assertIn("2026-08-21T18:00:00+09:00", system_message)

    def test_prompt_warns_against_treating_read_only_questions_as_write_requests(self) -> None:
        """같은 버그의 근본 원인 절반 — System Prompt에 "정보를 묻는 질문과 실행 요청을
        구분하라"는 안내가 실제로 들어가는지 확인한다(Skill 카탈로그·안내문이 실수로
        지워지는 회귀를 잡는다)."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("명시적인 실행", system_message)
        self.assertIn("정보를 묻는", system_message)

    def test_prompt_forbids_vague_stalling_replies(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — Context에 답이 없거나 있어도, reply가
        "찾아드릴게요"·"확인해드릴게요"처럼 나중에 하겠다는 약속형 문장으로 얼버무리는 사례가 있었다
        (후속 턴이 없어 사실상 답을 안 준 것과 같다). System Prompt가 이를 명시적으로 금지하는지
        확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("약속형 문장", system_message)
        self.assertIn("정직하게 인정", system_message)

    def test_prompt_asks_for_natural_korean_date_formatting_instead_of_raw_iso8601(self) -> None:
        """실사용 중 발견한 문제(2026-08-17) — 일정 시각을 답할 때 ISO 8601 원문
        ("2026-08-19T05:45:00+09:00")을 그대로 옮겨 사용자가 "2026년 8월 19일 오전 9시입니다"
        같은 자연스러운 형식으로 바꿔 달라고 요청했다. System Prompt가 이 지침을 담고 있는지
        확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("자연스러운 한국어로 바꿔", system_message)

    def test_prompt_tells_the_llm_to_list_candidate_meetings_when_analyze_meeting_target_is_ambiguous(self) -> None:
        """사용자 요청(2026-08-17)의 회귀 테스트 — "방금 진행한 회의 분석해줘"처럼
        어떤 회의인지 모호할 때, Router가 아무거나 짐작해서 바로 분석을 걸지 않고
        후보 목록을 먼저 보여주며 되물어야 한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("절대 아무거나 짐작해서 고르지 마라", system_message)
        self.assertIn("항상 실행 전 확인이 필요", system_message)

    def test_prompt_tells_the_llm_to_list_tasks_before_giving_up_on_a_past_request_lookup(self) -> None:
        """실사용 중 발견한 버그(2026-08-17) — "최근에 이메일로 받은 요청중에 타임아웃 나는 api
        수정 요청받은게 있는데 내용 찾아줘"라고 물으니, 그 요청은 이미 승인돼 Task로 남아 있는데도
        Router가 확인도 안 해보고 "정보가 없습니다"로 바로 포기했다. Context엔 대기 중인 제안만
        있고 이미 승인된 제안(=Task)은 없어서다 — manage_tasks(list)로 실제 목록을 먼저 확인하라는
        지침이 System Prompt에 있는지 확인한다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("반드시 먼저", system_message)
        self.assertIn('action="list"', system_message)

    def test_prompt_grounds_the_llm_with_todays_date_in_the_given_timezone(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — "지난 일주일 주간보고"라고 물었는데
        LLM이 오늘이 언제인지 몰라 `week_of`를 비워 보내는 바람에, 서버 기본값("이번 주" = 오늘부터
        7일)이 그대로 나가 사용자가 기대한 "지난 주"와 정반대 기간이 나왔다. System Prompt에 오늘
        날짜가 실제로 들어가는지 확인한다."""

        response = FakeResponse(_completion({"action": "call_skill", "reply": None, "skill_id": "weekly_report", "arguments": {"week_of": "2026-08-10"}}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            with patch("app.workflows.assistant_router._today", return_value="2026-08-17"):
                route("지난 일주일 주간보고 작성해줘", timezone_name="Asia/Seoul", api_key="test")
        sent_body = json.loads(mocked.call_args.args[0].data)
        system_message = sent_body["messages"][0]["content"]
        self.assertIn("2026-08-17", system_message)
        self.assertIn("Asia/Seoul", system_message)

    def test_weekly_report_catalog_defaults_to_the_past_week_not_the_upcoming_week(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — 아무 기간도 말하지 않고
        그냥 "주간보고 작성해줘"라고만 했는데 기간이 "2026-08-17~08-24"(오늘부터
        앞으로 7일)로 나왔다. "주간보고"는 관행적으로 지난 한 주를 되짚는
        보고이므로, week_of를 비워두지 말고 항상 "기준일자-7일"을 직접 채우게
        해야 한다(기준일자가 불분명하면 오늘을 기준일자로 삼는다) — 즉 기본값이
        "오늘-7일"(지난 일주일)이어야 "오늘"(이번 주 순방향)이 아니다."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("아무 질문", api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("week_of는 절대 비워두지 마라", system_message)
        self.assertIn("**아무 상대 표현도 날짜도 없으면**(기본값) week_of=오늘-7일", system_message)
        self.assertIn("두 규칙을 겹쳐 적용하지 마라", system_message)

    def test_route_call_sends_strict_true(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — `arguments`를 느슨한
        자유 `object`로 두고 `strict`를 껐더니, 이 Provider가 그 Schema를
        사실상 무시하고 Schema 정의 자체를 답으로 그대로 반환해 "invalid
        action" 오류가 났다. `route()`는 항상 `strict: true`로 호출해야 한다
        (평평한 Nullable Schema로 바꿔 Strict 모드 요건을 만족시켰다)."""

        response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            route("질문", api_key="test")
        sent_body = json.loads(mocked.call_args.args[0].data)
        self.assertTrue(sent_body["response_format"]["json_schema"]["strict"])

    def test_arguments_with_null_fields_are_stripped_to_only_the_filled_ones(self) -> None:
        response = FakeResponse(
            _completion(
                {
                    "action": "call_skill",
                    "reply": None,
                    "skill_id": "weekly_report",
                    "arguments": {
                        "week_of": "2026-08-10",
                        "query": None,
                        "date_from": None,
                        "date_to": None,
                        "meeting_id": None,
                        "decisions": None,
                        "source_type": None,
                        "decision": None,
                        "message_id": None,
                        "calendar_id": None,
                        "event_id": None,
                        "action": None,
                        "title": None,
                        "due_at": None,
                        "task_id": None,
                    },
                }
            )
        )
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            routed = route("지난 일주일 보고서", api_key="test")
        self.assertEqual(routed.arguments, {"week_of": "2026-08-10"})

    def test_missing_api_key_raises_before_any_network_call(self) -> None:
        # 이 세션의 실제 쉘 환경에 OPENAI_API_KEY가 있을 수 있으므로 명시적으로 비운다
        # (`tests/test_email_action_items.py`와 같은 이유).
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("app.workflows.assistant_router.request.urlopen") as mocked:
                with self.assertRaises(AssistantRouterError):
                    route("질문", api_key=None, base_url="https://example.invalid")
            mocked.assert_not_called()


class DefaultProposalTaskTests(unittest.TestCase):
    def test_looks_up_the_email_proposal_title_from_context(self) -> None:
        context = {"proposals": [{"title": "PayPal 계정 확인 메일", "source_type": "email", "message_id": "msg-9"}]}
        task = _default_proposal_task("user-a", context, {"source_type": "email", "message_id": "msg-9"})
        self.assertEqual(task, {"title": "PayPal 계정 확인 메일", "assignee_user_id": "user-a", "due_at": None, "priority_hint": None})

    def test_looks_up_the_calendar_proposal_title_from_context(self) -> None:
        context = {"proposals": [{"title": "스프린트 리뷰", "source_type": "calendar", "calendar_id": "cal-1", "event_id": "evt-1"}]}
        task = _default_proposal_task("user-a", context, {"source_type": "calendar", "calendar_id": "cal-1", "event_id": "evt-1"})
        self.assertEqual(task["title"], "스프린트 리뷰")

    def test_falls_back_to_a_generic_title_when_no_context_matches(self) -> None:
        task = _default_proposal_task("user-a", None, {"source_type": "email", "message_id": "msg-9"})
        self.assertEqual(task["title"], "제안된 업무")
        self.assertEqual(task["assignee_user_id"], "user-a")


class ResolveContextReferenceTests(unittest.TestCase):
    """실사용 피드백(2026-08-17)의 회귀 테스트 — "신입사원 환영회가 언제지?"처럼 Context
    제목("신규 입사자 환영회")과 표현이 살짝 다른 질문을 문자열 유사도(LCS)로 미리
    찾아주는 첫 시도는, 사용자가 "조사 하나만 바뀌어도 못 찾는 게 확실하다"고 지적한
    대로 근본 해결이 아니었다 — Routing과 분리된 전용 LLM 매칭 호출로 바꿨다."""

    def test_returns_the_matched_candidate(self) -> None:
        response = FakeResponse(_completion({"matched_index": 0}))
        context = {"proposals": [{"title": "신규 입사자 환영회", "source_type": "calendar", "calendar_id": "primary", "event_id": "evt-1", "due_at": "2026-08-19T05:45:00+09:00"}]}
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            match = _resolve_context_reference("신입사원 환영회가 언제지?", context, api_key="test")
        self.assertIsNotNone(match)
        self.assertEqual(match["kind"], "proposal")
        self.assertEqual(match["item"]["title"], "신규 입사자 환영회")

    def test_matches_a_meeting_candidate_too(self) -> None:
        response = FakeResponse(_completion({"matched_index": 0}))
        context = {"meetings": [{"meeting_id": "m-1", "title": "스프린트 리뷰"}]}
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            match = _resolve_context_reference("스프린트 리뷰 회의 요약해줘", context, api_key="test")
        self.assertIsNotNone(match)
        self.assertEqual(match["kind"], "meeting")
        self.assertEqual(match["item"]["meeting_id"], "m-1")

    def test_returns_none_when_the_llm_finds_no_match(self) -> None:
        response = FakeResponse(_completion({"matched_index": None}))
        context = {"proposals": [{"title": "신규 입사자 환영회", "source_type": "calendar"}]}
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            match = _resolve_context_reference("오늘 브리핑 보여줘", context, api_key="test")
        self.assertIsNone(match)

    def test_out_of_range_matched_index_is_treated_as_no_match(self) -> None:
        response = FakeResponse(_completion({"matched_index": 5}))
        context = {"proposals": [{"title": "신규 입사자 환영회", "source_type": "calendar"}]}
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            match = _resolve_context_reference("아무 질문", context, api_key="test")
        self.assertIsNone(match)

    def test_returns_none_when_context_is_empty_without_any_network_call(self) -> None:
        with patch("app.workflows.assistant_router.request.urlopen") as mocked:
            self.assertIsNone(_resolve_context_reference("아무 질문", None, api_key="test"))
            self.assertIsNone(_resolve_context_reference("아무 질문", {}, api_key="test"))
        mocked.assert_not_called()

    def test_route_includes_the_resolved_match_in_the_prompt(self) -> None:
        match_response = FakeResponse(_completion({"matched_index": 0}))
        route_response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {"proposals": [{"title": "신규 입사자 환영회", "source_type": "calendar", "calendar_id": "primary", "event_id": "evt-1", "due_at": "2026-08-19T05:45:00+09:00"}]}
        with patch("app.workflows.assistant_router.request.urlopen", side_effect=[match_response, route_response]) as mocked:
            route("신입사원 환영회가 언제지?", context=context, api_key="test")
        self.assertEqual(mocked.call_count, 2)
        final_system_message = json.loads(mocked.call_args_list[-1].args[0].data)["messages"][0]["content"]
        self.assertIn("Context 항목 확인됨", final_system_message)
        self.assertIn("신규 입사자 환영회", final_system_message)

    def test_route_omits_the_hint_when_the_llm_finds_no_match(self) -> None:
        match_response = FakeResponse(_completion({"matched_index": None}))
        route_response = FakeResponse(_completion({"action": "reply", "reply": "ok", "skill_id": None, "arguments": None}))
        context = {"proposals": [{"title": "신규 입사자 환영회", "source_type": "calendar"}]}
        with patch("app.workflows.assistant_router.request.urlopen", side_effect=[match_response, route_response]) as mocked:
            route("오늘 브리핑 보여줘", context=context, api_key="test")
        final_system_message = json.loads(mocked.call_args_list[-1].args[0].data)["messages"][0]["content"]
        self.assertNotIn("Context 항목 확인됨", final_system_message)


class TodayHelperTests(unittest.TestCase):
    def test_today_returns_an_iso_date_in_the_given_timezone(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        result = assistant_router._today("Asia/Seoul")
        expected = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        self.assertEqual(result, expected)


class PhraseAnswerTests(unittest.TestCase):
    def test_prompt_asks_for_natural_korean_date_formatting_instead_of_raw_iso8601(self) -> None:
        response = FakeResponse(_completion({"reply": "ok"}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            phrase_answer("질문", "daily_briefing", {}, api_key="test")
        system_message = json.loads(mocked.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("자연스러운 한국어로", system_message)

    def test_happy_path_returns_the_llm_reply(self) -> None:
        response = FakeResponse(_completion({"reply": "오늘 일정은 없어요."}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            reply = phrase_answer("오늘 일정 있어?", "daily_briefing", {"calendar_events": []}, api_key="test")
        self.assertEqual(reply, "오늘 일정은 없어요.")

    def test_blank_reply_raises(self) -> None:
        response = FakeResponse(_completion({"reply": "   "}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response):
            with self.assertRaises(AssistantRouterError):
                phrase_answer("질문", "daily_briefing", {}, api_key="test")

    def test_markdown_is_included_as_a_permitted_fact_source(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — weekly_report의
        "주요 회의 내용"이 구조화 JSON(`data`)에는 없고 `markdown`에만 있어서,
        챗봇이 "이 JSON에 있는 사실만"이라는 규칙 때문에 그 내용을 답에 아예
        못 썼다. `markdown`이 주어지면 프롬프트에 실리고, JSON에 없어도 그
        안의 사실은 써도 된다는 지침이 있는지 확인한다."""

        response = FakeResponse(_completion({"reply": "ok"}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            phrase_answer(
                "주요 회의 내용 알려줘",
                "weekly_report",
                {"summary": "완료 0건"},
                markdown="## 주요 회의 내용\n\n- 스프린트 리뷰: 다음 빌드 일정에 합의했다.",
                api_key="test",
            )
        sent_body = json.loads(mocked.call_args.args[0].data)
        system_message = sent_body["messages"][0]["content"]
        user_message = sent_body["messages"][1]["content"]
        self.assertIn("Markdown에만", system_message)
        self.assertIn("스프린트 리뷰: 다음 빌드 일정에 합의했다", user_message)

    def test_markdown_omitted_when_not_given(self) -> None:
        response = FakeResponse(_completion({"reply": "ok"}))
        with patch("app.workflows.assistant_router.request.urlopen", return_value=response) as mocked:
            phrase_answer("질문", "daily_briefing", {}, api_key="test")
        user_message = json.loads(mocked.call_args.args[0].data)["messages"][1]["content"]
        self.assertNotIn("참고용 Markdown 원문", user_message)


class OfferToAddActionItemsAsTasksTests(unittest.TestCase):
    """사용자 요청(2026-08-17)의 회귀 테스트 — 회의 분석 뒤 Action Item을 그냥 보여주기만
    하고, 할 일로 추가할지는 묻지 않았다. `_offer_to_add_action_items_as_tasks()`가
    pending 항목이 있을 때만 `review_action_items` 확인 요청을 만드는지 확인한다."""

    def test_builds_a_review_action_items_confirmation_for_pending_items(self) -> None:
        result_data = {
            "meeting_id": "m-1",
            "action_items": [
                {"action_item_id": "a-1", "approval_status": "pending"},
                {"action_item_id": "a-2", "approval_status": "pending"},
                {"action_item_id": "a-3", "approval_status": "approved"},
            ],
        }
        pending = _offer_to_add_action_items_as_tasks("analyze_meeting", result_data)
        self.assertEqual(pending["skill_id"], "review_action_items")
        self.assertEqual(pending["arguments"]["meeting_id"], "m-1")
        self.assertEqual(pending["arguments"]["decisions"], [{"action_item_id": "a-1", "decision": "approve"}, {"action_item_id": "a-2", "decision": "approve"}])

    def test_also_applies_to_get_meeting_analysis(self) -> None:
        result_data = {"meeting_id": "m-1", "action_items": [{"action_item_id": "a-1", "approval_status": "pending"}]}
        pending = _offer_to_add_action_items_as_tasks("get_meeting_analysis", result_data)
        self.assertIsNotNone(pending)

    def test_returns_none_for_other_skills(self) -> None:
        result_data = {"meeting_id": "m-1", "action_items": [{"action_item_id": "a-1", "approval_status": "pending"}]}
        self.assertIsNone(_offer_to_add_action_items_as_tasks("daily_briefing", result_data))

    def test_returns_none_when_every_action_item_is_already_reviewed(self) -> None:
        result_data = {"meeting_id": "m-1", "action_items": [{"action_item_id": "a-1", "approval_status": "approved"}, {"action_item_id": "a-2", "approval_status": "rejected"}]}
        self.assertIsNone(_offer_to_add_action_items_as_tasks("analyze_meeting", result_data))

    def test_returns_none_when_there_are_no_action_items(self) -> None:
        self.assertIsNone(_offer_to_add_action_items_as_tasks("analyze_meeting", {"meeting_id": "m-1", "action_items": []}))

    def test_returns_none_when_result_data_is_not_a_dict(self) -> None:
        self.assertIsNone(_offer_to_add_action_items_as_tasks("analyze_meeting", "not a dict"))


class AssistantAskWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 대부분의 테스트는 Routing·Skill 실행 자체가 관심사라, 매번 실제 회의
        # Repository·proposal_hub를 건드리지 않도록 기본 Context 채우기를
        # 항등 함수로 바꿔둔다 — 그 채우기 로직 자체는 아래 `DefaultContextTests`가
        # 따로 검증한다(2026-08-17, 20번 문서 4단계).
        patcher = patch.object(assistant_router, "_with_default_context", side_effect=lambda context, user_id: context)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_missing_text_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            await assistant_ask_workflow(_request(text=""))

    async def test_message_key_is_accepted_when_text_is_absent(self) -> None:
        """오케스트레이터가 `text` 없이 `message`만 보내는 직접 호출 경로(2026-08-18)도
        받아준다 — `m51_legacy_adapter.py`를 거치지 않고 `skill_id=assistant_ask`로
        바로 붙을 때 이 폴백이 없으면 매번 `ValueError`가 난다."""

        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="네", skill_id=None, arguments=None)):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(message="오늘 브리핑 보여줘"))
        registry_factory.assert_not_called()
        self.assertEqual(result.markdown, "네")

    async def test_text_takes_priority_over_message_when_both_are_present(self) -> None:
        """`text`가 있으면 기존 계약대로 `text`를 그대로 쓴다 — `message`는 안 본다."""

        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="ok", skill_id=None, arguments=None)) as fake_route:
            with patch("app.a2a.runtime.workflow_registry"):
                await assistant_ask_workflow(_request(text="진짜 질문", message="무시돼야 함"))
        fake_route.assert_called_once()
        self.assertEqual(fake_route.call_args.args[0], "진짜 질문")

    async def test_analyze_meeting_with_pending_action_items_asks_to_add_them_as_tasks(self) -> None:
        """사용자 요청(2026-08-17)의 통합 테스트 — 회의 분석 결과에 pending Action Item이
        있으면, 답변 뒤에 확인을 덧붙이고 `pending_action`으로 `review_action_items`
        전체 승인을 제안해야 한다. `analyze_meeting` 자체도 항상 확인이 필요해진
        뒤라(2026-08-17), 이미 확인을 거친(`confirmed_skill_id`) 경로로 검증한다."""

        sub_result = WorkflowResult(
            artifact_name="analyze_meeting",
            artifact_description="",
            text="{}",
            data={"type": "meeting_analysis", "data": {"meeting_id": "m-1", "summary": "요약", "action_items": [{"action_item_id": "a-1", "approval_status": "pending"}]}},
            mock=False,
        )
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "phrase_answer", return_value="회의를 분석했습니다."):
            with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                result = await assistant_ask_workflow(_request(text="이 회의 분석해줘", confirmed_skill_id="analyze_meeting", confirmed_arguments={"meeting_id": "m-1"}))
        self.assertIn("할 일로 추가할까요", result.markdown)
        pending = result.data["data"]["pending_action"]
        self.assertEqual(pending["skill_id"], "review_action_items")
        self.assertEqual(pending["arguments"], {"meeting_id": "m-1", "decisions": [{"action_item_id": "a-1", "decision": "approve"}]})

    async def test_analyze_meeting_without_pending_action_items_does_not_ask(self) -> None:
        sub_result = WorkflowResult(
            artifact_name="analyze_meeting",
            artifact_description="",
            text="{}",
            data={"type": "meeting_analysis", "data": {"meeting_id": "m-1", "summary": "요약", "action_items": []}},
            mock=False,
        )
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "phrase_answer", return_value="회의를 분석했습니다. Action Item은 없습니다."):
            with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                result = await assistant_ask_workflow(_request(text="이 회의 분석해줘", confirmed_skill_id="analyze_meeting", confirmed_arguments={"meeting_id": "m-1"}))
        self.assertIsNone(result.data["data"]["pending_action"])
        self.assertNotIn("할 일로 추가할까요", result.markdown)

    async def test_analyze_meeting_always_requires_confirmation_before_running(self) -> None:
        """사용자 요청(2026-08-17)의 회귀 테스트 — 회의 분석은 시간·비용이 들고, 여러
        회의 중 엉뚱한 걸 고르면 그 비용이 헛되이 든다. `route()`가 하나로 특정해도
        곧바로 실행하지 않고 먼저 확인을 받아야 한다."""

        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply="이 회의를 분석할까요?", skill_id="analyze_meeting", arguments={"meeting_id": "m-1"})):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="방금 진행한 회의 분석해"))
        registry_factory.assert_not_called()
        self.assertEqual(result.markdown, "이 회의를 분석할까요?")
        pending = result.data["data"]["pending_action"]
        self.assertEqual(pending, {"skill_id": "analyze_meeting", "arguments": {"meeting_id": "m-1"}})

    async def test_get_meeting_analysis_for_an_unanalyzed_meeting_is_redirected_to_analyze_meeting(self) -> None:
        """실사용 중 발견한 버그(2026-08-19) — Context에 `[분석 대기]`(has_analysis=False)로
        명시된 회의인데도 `route()`가 get_meeting_analysis를 골라, 저장된 분석이 없는데
        있는 것처럼 답을 지어낸 사례가 재현됐다. get_meeting_analysis는 읽기 전용이라
        확인 단계 없이 바로 실행되므로, 프롬프트 지시만 믿지 않고 서버가 Context의 실제
        has_analysis 값으로 재검증해 analyze_meeting(확인 필요)으로 바꿔야 한다."""

        context = {"meetings": [{"meeting_id": "m-1", "title": "8월 19일 회의", "has_analysis": False}]}
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply="'8월 19일 회의'의 저장된 분석을 보여드릴게요.", skill_id="get_meeting_analysis", arguments={"meeting_id": "m-1"})):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="오늘 한 회의 분석해줘", context=context))
        registry_factory.assert_not_called()
        pending = result.data["data"]["pending_action"]
        self.assertEqual(pending, {"skill_id": "analyze_meeting", "arguments": {"meeting_id": "m-1"}})
        self.assertIn("8월 19일 회의", result.markdown)
        self.assertIn("아직 분석하지 않았습니다", result.markdown)

    async def test_get_meeting_analysis_for_an_unknown_meeting_id_asks_for_clarification(self) -> None:
        """meeting_id가 Context에 아예 없으면(LLM이 지어낸 ID 포함) 실행하지 않고 되묻는다."""

        context = {"meetings": [{"meeting_id": "m-1", "title": "8월 19일 회의", "has_analysis": False}]}
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply="분석 결과를 보여드릴게요.", skill_id="get_meeting_analysis", arguments={"meeting_id": "m-hallucinated"})):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="그 회의 분석 보여줘", context=context))
        registry_factory.assert_not_called()
        self.assertIsNone(result.data["data"]["pending_action"])
        self.assertIsNone(result.data["data"]["executed_skill_id"])
        self.assertIn("특정할 수 없습니다", result.markdown)

    async def test_get_meeting_analysis_for_an_already_analyzed_meeting_still_runs_normally(self) -> None:
        """Context가 실제로 `[분석 완료]`라고 확인해 주는 정상 경로는 이 검증 때문에
        막히면 안 된다 — get_meeting_analysis는 읽기 전용이라 확인 없이 바로 실행된다."""

        context = {"meetings": [{"meeting_id": "m-1", "title": "8월 19일 회의", "has_analysis": True}]}
        sub_result = WorkflowResult(
            artifact_name="get_meeting_analysis",
            artifact_description="",
            text="{}",
            data={"type": "meeting_analysis", "data": {"meeting_id": "m-1", "summary": "요약", "action_items": []}},
            mock=False,
        )
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="get_meeting_analysis", arguments={"meeting_id": "m-1"})):
            with patch.object(assistant_router, "phrase_answer", return_value="저장된 분석을 보여드릴게요."):
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    result = await assistant_ask_workflow(_request(text="그 회의 분석 보여줘", context=context))
        self.assertEqual(result.data["data"]["executed_skill_id"], "get_meeting_analysis")
        fake_registry.execute.assert_awaited_once()

    async def test_reply_only_route_does_not_call_any_skill(self) -> None:
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="다시 말씀해 주세요.", skill_id=None, arguments=None)):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="음..."))
        registry_factory.assert_not_called()
        self.assertEqual(result.markdown, "다시 말씀해 주세요.")
        self.assertEqual(result.data["data"]["pending_action"], None)

    async def test_non_destructive_call_skill_executes_and_phrases_the_answer(self) -> None:
        sub_result = WorkflowResult(
            artifact_name="daily_briefing",
            artifact_description="",
            text="{}",
            data={"type": "daily_briefing", "data": {"calendar_events": []}},
            mock=False,
        )
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="daily_briefing", arguments={})):
            with patch.object(assistant_router, "phrase_answer", return_value="오늘 일정은 없어요.") as fake_phrase:
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    result = await assistant_ask_workflow(_request(text="오늘 브리핑 보여줘"))
        fake_registry.execute.assert_awaited_once()
        sub_request = fake_registry.execute.await_args.args[0]
        self.assertEqual(sub_request.skill_id, "daily_briefing")
        self.assertEqual(sub_request.user_id, "user-a")
        fake_phrase.assert_called_once_with("오늘 브리핑 보여줘", "daily_briefing", {"calendar_events": []}, markdown=None)
        self.assertEqual(result.markdown, "오늘 일정은 없어요.")
        self.assertEqual(result.data["data"]["executed_skill_id"], "daily_briefing")
        self.assertIsNone(result.data["data"]["pending_action"])

    async def test_phrase_answer_receives_the_sub_skills_markdown_for_facts_missing_from_data(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — weekly_report의
        "주요 회의 내용"은 공개 A2A 계약(구조화 `data` Schema)에는 없고
        `markdown` 본문에만 있다(`app/workflows/weekly_report.py`의
        `_render_weekly_report_markdown()` 참고, 의도적 설계). 챗봇이
        `data`만 보고 답을 지었을 땐 이 내용을 통째로 답하지 못했다.
        `phrase_answer()`가 `markdown`도 함께 받는지 확인한다."""

        sub_result = WorkflowResult(
            artifact_name="weekly_report",
            artifact_description="",
            text="{}",
            data={"type": "weekly_report", "data": {"summary": "완료 0건"}},
            markdown="# 주간 보고서\n\n## 주요 회의 내용\n\n- 스프린트 리뷰: 다음 빌드 일정에 합의했다.",
            mock=False,
        )
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="weekly_report", arguments={})):
            with patch.object(assistant_router, "phrase_answer", return_value="ok") as fake_phrase:
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    await assistant_ask_workflow(_request(text="주간보고 작성해줘"))
        fake_phrase.assert_called_once_with(
            "주간보고 작성해줘",
            "weekly_report",
            {"summary": "완료 0건"},
            markdown="# 주간 보고서\n\n## 주요 회의 내용\n\n- 스프린트 리뷰: 다음 빌드 일정에 합의했다.",
        )

    async def test_destructive_review_action_items_reject_asks_for_confirmation_first(self) -> None:
        arguments = {"meeting_id": "m-1", "decisions": [{"action_item_id": "a-1", "decision": "reject"}]}
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply="이 Action Item을 거절할까요?", skill_id="review_action_items", arguments=arguments)):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="a-1 거절해줘"))
        registry_factory.assert_not_called()
        self.assertEqual(result.markdown, "이 Action Item을 거절할까요?")
        pending = result.data["data"]["pending_action"]
        self.assertEqual(pending["skill_id"], "review_action_items")
        self.assertEqual(pending["arguments"], arguments)

    async def test_review_proposal_approve_fills_in_the_task_from_context_without_asking_the_llm(self) -> None:
        context = {"proposals": [{"title": "PayPal 계정 확인 메일", "source_type": "email", "message_id": "msg-9"}]}
        sub_result = WorkflowResult(artifact_name="review_proposal", artifact_description="", text="{}", data={"type": "proposal_review", "data": {}}, mock=False)
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="review_proposal", arguments={"source_type": "email", "decision": "approve", "message_id": "msg-9"})):
            with patch.object(assistant_router, "phrase_answer", return_value="승인했습니다."):
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    await assistant_ask_workflow(_request(text="이 메일 제안 승인해줘", context=context))
        sub_request = fake_registry.execute.await_args.args[0]
        self.assertEqual(sub_request.payload["task"], {"title": "PayPal 계정 확인 메일", "assignee_user_id": "user-a", "due_at": None, "priority_hint": None})

    async def test_destructive_review_action_items_approve_only_does_not_need_confirmation(self) -> None:
        arguments = {"meeting_id": "m-1", "decisions": [{"action_item_id": "a-1", "decision": "approve"}]}
        sub_result = WorkflowResult(artifact_name="review_action_items", artifact_description="", text="{}", data={"type": "action_items_review", "data": {}}, mock=False)
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="review_action_items", arguments=arguments)):
            with patch.object(assistant_router, "phrase_answer", return_value="승인했습니다."):
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    result = await assistant_ask_workflow(_request(text="a-1 승인해줘"))
        fake_registry.execute.assert_awaited_once()
        self.assertEqual(result.markdown, "승인했습니다.")

    async def test_confirmed_skill_id_skips_routing_and_executes_directly(self) -> None:
        sub_result = WorkflowResult(artifact_name="review_proposal", artifact_description="", text="{}", data={"type": "proposal_review", "data": {"decision": "ignore"}}, mock=False)
        fake_registry = AsyncMock()
        fake_registry.execute.return_value = sub_result
        with patch.object(assistant_router, "route") as fake_route:
            with patch.object(assistant_router, "phrase_answer", return_value="무시했습니다."):
                with patch("app.a2a.runtime.workflow_registry", return_value=fake_registry):
                    result = await assistant_ask_workflow(
                        _request(
                            text="응 무시해줘",
                            confirmed_skill_id="review_proposal",
                            confirmed_arguments={"source_type": "email", "decision": "ignore", "message_id": "msg-9"},
                        )
                    )
        fake_route.assert_not_called()
        fake_registry.execute.assert_awaited_once()
        self.assertEqual(result.markdown, "무시했습니다.")

    async def test_manage_tasks_delete_is_declined_without_confirmation(self) -> None:
        arguments = {"action": "delete", "task_id": "t-1"}
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="call_skill", reply=None, skill_id="manage_tasks", arguments=arguments)):
            with patch("app.a2a.runtime.workflow_registry") as registry_factory:
                result = await assistant_ask_workflow(_request(text="t-1 삭제해줘"))
        registry_factory.assert_not_called()
        self.assertIn("할 일 관리", result.markdown)

    async def test_payload_timezone_is_forwarded_to_route(self) -> None:
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="ok", skill_id=None, arguments=None)) as fake_route:
            await assistant_ask_workflow(_request(text="지난 주 보고서", timezone="America/Los_Angeles"))
        fake_route.assert_called_once_with("지난 주 보고서", context=None, history=None, timezone_name="America/Los_Angeles")

    async def test_missing_payload_timezone_defaults_to_asia_seoul(self) -> None:
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="ok", skill_id=None, arguments=None)) as fake_route:
            await assistant_ask_workflow(_request(text="안녕"))
        fake_route.assert_called_once_with("안녕", context=None, history=None, timezone_name="Asia/Seoul")

    async def test_payload_history_is_forwarded_to_route(self) -> None:
        """실사용 중 발견한 버그(2026-08-17)의 회귀 테스트 — 대화 이력 없이 매 요청을
        독립적으로 처리해, 짧은 후속 메시지가 이전 turn 맥락을 잃었다."""

        history = [{"role": "user", "text": "싱잉사원 환영회 언제?"}, {"role": "assistant", "text": "정보가 없습니다."}]
        with patch.object(assistant_router, "route", return_value=RoutedAction(action="reply", reply="ok", skill_id=None, arguments=None)) as fake_route:
            await assistant_ask_workflow(_request(text="신입사원", history=history))
        fake_route.assert_called_once_with("신입사원", context=None, history=history, timezone_name="Asia/Seoul")

    async def test_unknown_confirmed_skill_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            await assistant_ask_workflow(_request(text="응", confirmed_skill_id="not_a_real_skill", confirmed_arguments={}))


class _FakeMeeting:
    def __init__(self, meeting_id: str, title: str, has_analysis: bool) -> None:
        self.meeting_id = meeting_id
        self.title = title
        self.has_analysis = has_analysis


class _FakeProposal:
    def __init__(self, source_type, source_id, title, due_at=None, metadata=None) -> None:
        self.source_type = source_type
        self.source_id = source_id
        self.title = title
        self.due_at = due_at
        self.metadata = metadata


class DefaultContextTests(unittest.TestCase):
    """호출자가 `context`를 안 보낼 때 서버가 최근 회의·제안을 직접 채우는지 검증한다
    (2026-08-17, 20번 문서 4단계 — 오케스트레이터 어댑터처럼 Drawer 같은 화면 상태가
    없는 호출자를 위한 기본값)."""

    def test_default_meetings_context_reuses_list_meetings_and_caps_at_limit(self) -> None:
        meetings = [_FakeMeeting(f"m-{i}", f"회의 {i}", i % 2 == 0) for i in range(15)]
        with patch("app.meeting_api.list_meetings", return_value=meetings) as fake_list:
            result = assistant_router._default_meetings_context("user-a")
        fake_list.assert_called_once_with(user_id="user-a")
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0], {"meeting_id": "m-0", "title": "회의 0", "has_analysis": True})

    def test_default_proposals_context_shapes_email_and_calendar_items(self) -> None:
        from datetime import datetime, timezone

        proposals = [
            _FakeProposal("email", "msg-1", "메일 제안", metadata={}),
            _FakeProposal(
                "calendar", "cal-1:evt-1", "일정 제안",
                due_at=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
                metadata={"calendar_id": "cal-1", "event_id": "evt-1"},
            ),
        ]
        with patch("app.proposal_api.proposal_hub.recent", return_value=proposals) as fake_recent:
            result = assistant_router._default_proposals_context("user-a")
        fake_recent.assert_called_once_with("user-a", limit=10)
        self.assertEqual(result[0], {"source_type": "email", "title": "메일 제안", "message_id": "msg-1"})
        self.assertEqual(
            result[1],
            {
                "source_type": "calendar", "title": "일정 제안",
                "due_at": "2026-08-20T09:00:00+00:00",
                "calendar_id": "cal-1", "event_id": "evt-1",
            },
        )

    def test_with_default_context_only_fills_missing_keys(self) -> None:
        """호출자(Drawer)가 이미 채워 보낸 값은 그대로 존중하고, 빠진 것만 채운다."""

        caller_context = {"proposals": [{"title": "이미 있음"}], "pending_action_items": [{"meeting_id": "m-1", "action_item_id": "a-1"}]}
        with patch.object(assistant_router, "_default_meetings_context", return_value=[{"meeting_id": "m-9", "title": "서버 기본값", "has_analysis": False}]) as fake_meetings:
            with patch.object(assistant_router, "_default_proposals_context") as fake_proposals:
                result = assistant_router._with_default_context(caller_context, "user-a")
        fake_meetings.assert_called_once_with("user-a")
        fake_proposals.assert_not_called()
        self.assertEqual(result["meetings"], [{"meeting_id": "m-9", "title": "서버 기본값", "has_analysis": False}])
        self.assertEqual(result["proposals"], [{"title": "이미 있음"}])
        self.assertEqual(result["pending_action_items"], [{"meeting_id": "m-1", "action_item_id": "a-1"}])

    def test_with_default_context_degrades_gracefully_when_meetings_lookup_fails(self) -> None:
        """실사용 중 발견한 버그의 회귀 테스트(2026-08-17) — 회의 DB 조회가 실패하면
        (예: 잘못된 경로 설정) Context 채우기 전체가 아니라 이 요청과 무관한
        assistant_ask 호출까지 함께 죽었다. 실패해도 빈 목록으로 넘어가야 한다."""

        with patch.object(assistant_router, "_default_meetings_context", side_effect=FileNotFoundError("no such path")):
            with patch.object(assistant_router, "_default_proposals_context", return_value=[]):
                result = assistant_router._with_default_context(None, "user-a")
        self.assertEqual(result["meetings"], [])
        self.assertEqual(result["proposals"], [])

    def test_with_default_context_degrades_gracefully_when_proposals_lookup_fails(self) -> None:
        with patch.object(assistant_router, "_default_meetings_context", return_value=[]):
            with patch.object(assistant_router, "_default_proposals_context", side_effect=RuntimeError("hub unavailable")):
                result = assistant_router._with_default_context(None, "user-a")
        self.assertEqual(result["meetings"], [])
        self.assertEqual(result["proposals"], [])

    def test_with_default_context_fills_both_when_caller_sends_no_context_at_all(self) -> None:
        with patch.object(assistant_router, "_default_meetings_context", return_value=[{"meeting_id": "m-1", "title": "회의", "has_analysis": False}]):
            with patch.object(assistant_router, "_default_proposals_context", return_value=[]):
                result = assistant_router._with_default_context(None, "user-a")
        self.assertEqual(result["meetings"], [{"meeting_id": "m-1", "title": "회의", "has_analysis": False}])
        self.assertEqual(result["proposals"], [])


if __name__ == "__main__":
    unittest.main()
