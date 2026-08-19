"""메일 본문 → LLM Action Item 추출 계약 테스트."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from app.email_action_items import (
    EmailActionItemProviderError,
    EmailRelevanceProviderError,
    classify_work_related_emails,
    extract_action_items,
)


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._body).encode()


def _completion(action_items: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"action_items": action_items}, ensure_ascii=False)}}]}


class EmailActionItemsTests(unittest.TestCase):
    def test_extracts_action_items_from_llm_response(self) -> None:
        response = FakeResponse(_completion([
            {"title": "회의 일정 확정", "reason": "발신자가 이번 주 안에 회신을 요청함"},
        ]))
        with patch("app.email_action_items.request.urlopen", return_value=response) as mocked:
            items = extract_action_items("회의 일정 문의", "이번 주 안에 회신 부탁드립니다.", api_key="test")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "회의 일정 확정")
        # Subject·본문이 모두 LLM 입력에 포함됐는지 확인한다.
        sent_body = json.loads(mocked.call_args.args[0].data)
        user_message = sent_body["messages"][1]["content"]
        self.assertIn("회의 일정 문의", user_message)
        self.assertIn("회신 부탁드립니다", user_message)

    def test_returns_empty_list_when_llm_finds_nothing_actionable(self) -> None:
        response = FakeResponse(_completion([]))
        with patch("app.email_action_items.request.urlopen", return_value=response):
            items = extract_action_items("뉴스레터", "이번 달 소식을 전해드립니다.", api_key="test")
        self.assertEqual(items, [])

    def test_empty_subject_and_body_short_circuits_without_calling_provider(self) -> None:
        with patch("app.email_action_items.request.urlopen") as mocked:
            items = extract_action_items("  ", "  ", api_key="test")
        self.assertEqual(items, [])
        mocked.assert_not_called()

    def test_missing_api_key_raises_before_any_network_call(self) -> None:
        # 이 세션의 실제 쉘 환경에 OPENAI_API_KEY가 있을 수 있으므로 명시적으로 비운다.
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("app.email_action_items.request.urlopen") as mocked:
                with self.assertRaises(EmailActionItemProviderError):
                    extract_action_items("제목", "본문", api_key=None, base_url="https://example.invalid")
            mocked.assert_not_called()

    def test_malformed_llm_response_raises_provider_error(self) -> None:
        response = FakeResponse({"choices": [{"message": {"content": "not json"}}]})
        with patch("app.email_action_items.request.urlopen", return_value=response):
            with self.assertRaises(EmailActionItemProviderError):
                extract_action_items("제목", "본문", api_key="test")

    def test_openai_model_alias_is_translated_for_direct_api(self) -> None:
        response = FakeResponse(_completion([]))
        with patch("app.email_action_items.request.urlopen", return_value=response) as mocked:
            extract_action_items("제목", "본문", api_key="test", base_url="https://api.openai.com/v1", model="openai/gpt-4.1-mini")
        sent_body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(sent_body["model"], "gpt-4.1-mini")


def _judgments(items: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"judgments": items}, ensure_ascii=False)}}]}


class ClassifyWorkRelatedEmailsTests(unittest.TestCase):
    """오늘 브리핑이 메일 신호를 업무 관련 여부로 거를 때 쓰는 일괄 판단 계약 테스트."""

    def test_returns_message_id_keyed_judgments_from_llm_response(self) -> None:
        response = FakeResponse(_judgments([
            {"message_id": "message-1", "is_work_related": True},
            {"message_id": "message-2", "is_work_related": False},
        ]))
        with patch("app.email_action_items.request.urlopen", return_value=response) as mocked:
            result = classify_work_related_emails(
                [("message-1", "검토 요청 — 이번 주 안에 회신 부탁드립니다."), ("message-2", "이번 달 특가 소식을 전해드립니다.")],
                api_key="test",
            )
        self.assertEqual(result, {"message-1": True, "message-2": False})
        sent_body = json.loads(mocked.call_args.args[0].data)
        user_message = sent_body["messages"][1]["content"]
        self.assertIn("message-1", user_message)
        self.assertIn("message-2", user_message)

    def test_empty_message_list_short_circuits_without_calling_provider(self) -> None:
        with patch("app.email_action_items.request.urlopen") as mocked:
            result = classify_work_related_emails([], api_key="test")
        self.assertEqual(result, {})
        mocked.assert_not_called()

    def test_missing_api_key_raises_before_any_network_call(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with patch("app.email_action_items.request.urlopen") as mocked:
                with self.assertRaises(EmailRelevanceProviderError):
                    classify_work_related_emails([("message-1", "제목")], api_key=None, base_url="https://example.invalid")
            mocked.assert_not_called()

    def test_malformed_llm_response_raises_provider_error(self) -> None:
        response = FakeResponse({"choices": [{"message": {"content": "not json"}}]})
        with patch("app.email_action_items.request.urlopen", return_value=response):
            with self.assertRaises(EmailRelevanceProviderError):
                classify_work_related_emails([("message-1", "제목")], api_key="test")


if __name__ == "__main__":
    unittest.main()
