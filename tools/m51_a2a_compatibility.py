"""M5.1 Workmate A2A HTTP+JSON 호환성 검수 클라이언트.

실행 중인 Workmate Agent의 Agent Card와 공개 `message:send` Route를 실제로
호출한다. 응답에 런타임 준비용 Mock Artifact가 포함되면 검수를 실패시켜
업무 Workflow가 연결되지 않은 상태를 완료로 오인하지 않게 한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from a2a.types import Message, Part, Role
from google.protobuf import json_format
from jsonschema import Draft202012Validator, FormatChecker


SKILLS: tuple[dict[str, Any], ...] = (
    {"skill_id": "daily_briefing", "as_of": "2026-08-12T09:00:00+09:00"},
    {"skill_id": "weekly_report", "week_of": "2026-08-10"},
    {
        "skill_id": "analyze_meeting",
        "meeting_id": "00000000-0000-4000-8000-000000000051",
    },
    {"skill_id": "search_meetings", "query": "decision", "limit": 5},
    {"skill_id": "rank_priorities", "as_of": "2026-08-12T09:00:00+09:00"},
)


def _root_url(base_url: str) -> str:
    """A2A Base URL에서 Agent Card 조회용 Origin을 계산한다."""

    parsed = urlsplit(base_url.rstrip("/"))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _schema_root() -> Path:
    """승인된 Artifact Schema 경로를 찾는다."""

    configured = os.getenv("WORKMATE_SCHEMA_ROOT")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[2] / "docs" / "schemas",
    ]
    for candidate in candidates:
        if candidate and (candidate / "a2a-artifact.schema.json").is_file():
            return candidate
    raise RuntimeError("WORKMATE_SCHEMA_ROOT 또는 superproject docs/schemas가 필요합니다.")


def _message_payload(skill: dict[str, Any]) -> dict[str, Any]:
    """승인된 Skill 입력을 SDK Message의 HTTP+JSON 형태로 직렬화한다."""

    data = {
        "schema_version": "1.0",
        "user_id": os.getenv(
            "WORKMATE_TEST_USER_ID", "00000000-0000-4000-8000-000000000501"
        ),
        "timezone": "Asia/Seoul",
        "locale": "ko-KR",
        **skill,
    }
    message = Message(
        message_id=f"m51-{skill['skill_id']}",
        role=Role.ROLE_USER,
        parts=[Part(media_type="application/json")],
    )
    json_format.ParseDict(data, message.parts[0].data)
    return {
        "message": json_format.MessageToDict(
            message, preserving_proto_field_name=False
        ),
        "metadata": {"request_id": f"m51-{skill['skill_id']}"},
    }


def _post_json(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """Bearer 인증으로 JSON POST를 수행하고 응답 JSON을 반환한다."""

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "A2A-Version": "1.0",
            "Accept": "application/a2a+json",
            "Content-Type": "application/a2a+json",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def run() -> dict[str, Any]:
    """Agent Card와 5개 Skill의 실제 HTTP 응답을 검증한다."""

    base_url = os.getenv("WORKMATE_A2A_BASE_URL", "http://127.0.0.1:8001/a2a").rstrip("/")
    token = os.getenv("WORKMATE_SERVICE_TOKEN")
    if not token:
        raise RuntimeError("WORKMATE_SERVICE_TOKEN이 설정되지 않았습니다.")

    card_request = Request(
        f"{_root_url(base_url)}/.well-known/agent-card.json",
        headers={"Accept": "application/json"},
    )
    with urlopen(card_request, timeout=10) as response:
        card = json.loads(response.read().decode("utf-8"))
    advertised = {item.get("id") for item in card.get("skills", [])}
    expected = {item["skill_id"] for item in SKILLS}
    if not expected.issubset(advertised):
        raise AssertionError(f"Agent Card에 Skill이 누락되었습니다: {sorted(expected - advertised)}")

    artifact_schema = json.loads(
        (_schema_root() / "a2a-artifact.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(artifact_schema, format_checker=FormatChecker())
    checks: dict[str, str] = {}
    for skill in SKILLS:
        skill_id = str(skill["skill_id"])
        try:
            body = _post_json(f"{base_url}/message:send", _message_payload(skill), token)
            task = body.get("task", {})
            artifacts = task.get("artifacts", [])
            if task.get("status", {}).get("state") not in {"TASK_STATE_COMPLETED", "completed"}:
                raise AssertionError("Task가 terminal completed 상태가 아닙니다.")
            if not artifacts:
                raise AssertionError("Artifact가 없습니다.")
            for artifact in artifacts:
                validator.validate(artifact)
                if artifact.get("name") == "runtime_bootstrap":
                    raise AssertionError("runtime_bootstrap Mock Artifact가 반환되었습니다.")
                if any(part.get("metadata", {}).get("mock") is True for part in artifact.get("parts", [])):
                    raise AssertionError("Mock Artifact metadata가 반환되었습니다.")
            checks[skill_id] = "pass"
        except (HTTPError, URLError, AssertionError, json.JSONDecodeError, ValueError) as exc:
            checks[skill_id] = f"fail: {exc}"

    return {"base_url": base_url, "agent_card_skills": sorted(advertised), "checks": checks}


if __name__ == "__main__":
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 - CLI는 안전한 요약만 출력한다.
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(value == "pass" for value in result["checks"].values()) else 1)
