"""M5.1 레거시 Orchestrator 어댑터의 경로·응답 변환 검증.

2026-08-17(20번 문서 구현)부터 이 어댑터는 Skill을 스스로 판별하지 않는다 —
모든 채팅 요청을 `assistant_ask`로 고정해 넘기고, 실제 Skill 판별은
Workmate 쪽 자연어 Router가 한다. 그래서 이 파일은 더 이상 `_infer_skill`류
키워드 매핑을 검증하지 않고, "항상 assistant_ask로 간다"는 것과 응답
변환(실행된 Skill 노출, 확인 필요 안내)만 검증한다.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.m51_legacy_adapter import (
    ASSIGNEE_USER_IDS,
    ASSISTANT_ASK_SKILL_ID,
    DEFAULT_USER_ID,
    _legacy_response,
    _workmate_request,
    app,
)


class LegacyAdapterTests(unittest.TestCase):
    """HTTP+JSON 경로와 레거시 응답 변환을 검증한다."""

    def test_adapter_rewrites_agent_card_endpoint(self) -> None:
        """HTTP+JSON endpoint를 어댑터 주소로만 변환한다."""

        with patch(
            "tools.m51_legacy_adapter._request_json",
            return_value=(
                200,
                b'{"supportedInterfaces":[{"url":"http://workmate:8001/a2a","protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}]}',
                "application/json",
            ),
        ):
            response = TestClient(app).get("/.well-known/agent-card.json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["supportedInterfaces"][0]["url"].endswith("/a2a/v1/message:send"))
        self.assertTrue(response.json()["url"].endswith("/a2a/v1/message:send"))
        self.assertEqual(response.json()["name"], "workmate-agent")

    def test_adapter_converts_any_chat_text_to_a_fixed_assistant_ask_request(self) -> None:
        """어떤 문장이 오든 Skill 판별 없이 `assistant_ask`로 고정해 넘긴다."""

        body = _workmate_request(
            json.dumps({"message": {"messageId": "main-agent", "parts": [{"text": "오늘 브리핑 보여줘"}]}}).encode()
        )
        part = json.loads(body)["message"]["parts"][0]
        self.assertEqual(part["data"]["skill_id"], ASSISTANT_ASK_SKILL_ID)
        self.assertEqual(part["data"]["text"], "오늘 브리핑 보여줘")
        self.assertEqual(part["data"]["user_id"], DEFAULT_USER_ID)
        self.assertNotEqual(json.loads(body)["message"]["messageId"], "main-agent")

    def test_adapter_reads_text_from_the_orchestrator_chat_default_data_shape(self) -> None:
        """`{skill_id, message}` 모양(오케스트레이터 채팅 기본값)도 문장을 그대로 뽑아낸다."""

        body = _workmate_request(
            json.dumps(
                {
                    "message": {
                        "parts": [{"data": {"skill_id": "daily_briefing", "message": "주간 보고서 작성해줘"}}]
                    }
                }
            ).encode()
        )
        data = json.loads(body)["message"]["parts"][0]["data"]
        self.assertEqual(data["skill_id"], ASSISTANT_ASK_SKILL_ID)
        self.assertEqual(data["text"], "주간 보고서 작성해줘")

    def test_metadata_owner_resolves_to_the_matching_workmate_user_id(self) -> None:
        """`metadata.owner`로 담당자 이름이 오면 고정 데모 사용자 대신 매핑된 user_id를 쓴다.

        오케스트레이터가 아직 이 필드를 안 보내(20번 문서 R5) 지금은 도달하지
        않는 경로지만, 반영된 뒤 바로 동작하도록 구조를 미리 검증해 둔다.
        """

        body = _workmate_request(
            json.dumps(
                {"message": {"parts": [{"text": "오늘 브리핑 보여줘"}], "metadata": {"owner": "서선정"}}}
            ).encode()
        )
        part = json.loads(body)["message"]["parts"][0]
        # `ASSIGNEE_USER_IDS["서선정"]`과 비교하는 자기 참조가 아니라 독립적으로 아는
        # 실제 Google OIDC `sub` 리터럴과 직접 비교한다 — 2026-08-19, 이 상수에 자릿수
        # 오타(21자리, 정상은 20자리)가 있었는데도 자기 참조 검증이라 못 잡아낸 전례가
        # 있다(`app/a2a/runtime.py`의 같은 이름 상수에서 실제로 재현·수정됨).
        self.assertEqual(part["data"]["user_id"], "10464531542706509691")
        self.assertNotEqual(ASSIGNEE_USER_IDS["서선정"], DEFAULT_USER_ID)

    def test_unrecognized_or_missing_owner_falls_back_to_the_default_user(self) -> None:
        """모르는 이름이거나 `metadata.owner` 자체가 없으면 기존처럼 고정 데모 사용자를 쓴다."""

        with_unknown_owner = _workmate_request(
            json.dumps(
                {"message": {"parts": [{"text": "오늘 브리핑 보여줘"}], "metadata": {"owner": "홍길동"}}}
            ).encode()
        )
        without_owner = _workmate_request(
            json.dumps({"message": {"parts": [{"text": "오늘 브리핑 보여줘"}]}}).encode()
        )
        self.assertEqual(json.loads(with_unknown_owner)["message"]["parts"][0]["data"]["user_id"], DEFAULT_USER_ID)
        self.assertEqual(json.loads(without_owner)["message"]["parts"][0]["data"]["user_id"], DEFAULT_USER_ID)

    def test_empty_text_returns_actionable_400_without_calling_upstream(self) -> None:
        """빈 문장은 upstream의 `text is required` 내부 오류 대신 400으로 미리 거른다."""

        with patch("tools.m51_legacy_adapter._request_json") as upstream:
            response = TestClient(app).post(
                "/a2a/v1/message:send",
                json={"message": {"parts": [{"text": ""}]}},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["status"], "INVALID_ARGUMENT")
        upstream.assert_not_called()

    def test_adapter_forwards_assistant_ask_requests_to_upstream(self) -> None:
        """빈 문장이 아니면 그대로 upstream `assistant_ask`로 전달한다."""

        upstream = {"task": {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": []}}
        with patch(
            "tools.m51_legacy_adapter._request_json",
            return_value=(200, json.dumps(upstream).encode(), "application/json"),
        ) as fake_upstream:
            response = TestClient(app).post(
                "/a2a/v1/message:send",
                json={"message": {"parts": [{"text": "이번 주 우선순위 알려줘"}]}},
            )

        self.assertEqual(response.status_code, 200)
        sent_body = json.loads(fake_upstream.call_args.kwargs["body"])
        sent_data = sent_body["message"]["parts"][0]["data"]
        self.assertEqual(sent_data["skill_id"], ASSISTANT_ASK_SKILL_ID)
        self.assertEqual(sent_data["text"], "이번 주 우선순위 알려줘")

    def test_legacy_response_exposes_the_skill_assistant_ask_actually_executed(self) -> None:
        """레거시 호환 Part의 `invoked_skill`은 assistant_ask가 아니라 실제 실행된 Skill이다."""

        upstream = {
            "task": {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {
                        "parts": [
                            {"data": {"type": "assistant_reply", "data": {"reply": "오늘 일정이 없습니다.", "executed_skill_id": "daily_briefing", "pending_action": None}}, "mediaType": "application/json"},
                            {"text": "오늘 일정이 없습니다.", "mediaType": "text/markdown"},
                        ]
                    }
                ],
            }
        }

        transformed = _legacy_response(upstream)

        parts = transformed["task"]["artifacts"][0]["parts"]
        legacy_part = parts[-1]
        self.assertEqual(legacy_part["data"]["invoked_skill"], "daily_briefing")
        self.assertNotIn("text", legacy_part)
        # 원본 답변 Part는 그대로 유지된다 — 새 Part에 텍스트를 복사하지 않는다
        # (오케스트레이터가 한 Artifact의 모든 .text를 이어붙이므로 중복 방지).
        self.assertEqual(parts[1]["text"], "오늘 일정이 없습니다.")

    def test_legacy_response_appends_a_confirmation_notice_when_pending_action_is_present(self) -> None:
        """확인이 필요한 응답(pending_action)엔 안내 문구를 덧붙인다 — 오케스트레이터엔
        확인 버튼이 없어 자동으로 실행되면 안 된다(15번 문서 G5)."""

        upstream = {
            "task": {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {
                        "parts": [
                            {
                                "data": {
                                    "type": "assistant_reply",
                                    "data": {
                                        "reply": "이 회의를 분석할까요?",
                                        "executed_skill_id": None,
                                        "pending_action": {"skill_id": "analyze_meeting", "arguments": {"meeting_id": "m-1"}},
                                    },
                                },
                                "mediaType": "application/json",
                            },
                            {"text": "이 회의를 분석할까요?", "mediaType": "text/markdown"},
                        ]
                    }
                ],
            }
        }

        transformed = _legacy_response(upstream)

        parts = transformed["task"]["artifacts"][0]["parts"]
        # 안내는 새 Part가 아니라 원본 답변 Part(.text)에 직접 붙는다 — 이게
        # 오케스트레이터가 실제로 읽는 Part다.
        self.assertIn("이 회의를 분석할까요?", parts[1]["text"])
        self.assertIn("Workmate 화면에서 직접 진행해 주세요", parts[1]["text"])
        self.assertEqual(parts[-1]["data"]["invoked_skill"], ASSISTANT_ASK_SKILL_ID)
        self.assertNotIn("text", parts[-1])

    def test_legacy_response_also_transforms_a_bare_task_from_task_polling(self) -> None:
        """`GET /a2a/v1/tasks/{id}`(`returnImmediately` 사용 시 실제 완료된 답이
        오는 경로)는 `{"task": ...}`로 안 감싸고 Task를 그대로 최상위에 준다
        (`GetTask` RPC 응답, `message:send`의 `SendMessageResponse`와 다름) —
        이 모양도 똑같이 변환돼야 한다(2026-08-17, 20번 문서 G7 대응)."""

        bare_task = {
            "id": "task-1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "parts": [
                        {"data": {"type": "assistant_reply", "data": {"reply": "완료했습니다.", "executed_skill_id": "weekly_report", "pending_action": None}}, "mediaType": "application/json"},
                        {"text": "완료했습니다.", "mediaType": "text/markdown"},
                    ]
                }
            ],
        }

        transformed = _legacy_response(bare_task)

        parts = transformed["artifacts"][0]["parts"]
        self.assertEqual(parts[-1]["data"]["invoked_skill"], "weekly_report")
        self.assertNotIn("text", parts[-1])
        self.assertEqual(parts[1]["text"], "완료했습니다.")

    def test_legacy_response_without_a_data_part_falls_back_gracefully(self) -> None:
        """구형 Fixture(순수 text Part만 있는 응답)에도 예외 없이 동작한다."""

        upstream = {
            "task": {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "실제 업무 결과"}]}],
            }
        }

        transformed = _legacy_response(upstream)

        parts = transformed["task"]["artifacts"][0]["parts"]
        self.assertEqual(parts[-1]["data"]["invoked_skill"], ASSISTANT_ASK_SKILL_ID)
        self.assertNotIn("text", parts[-1])
        self.assertEqual(parts[0]["text"], "실제 업무 결과")


if __name__ == "__main__":
    unittest.main()
