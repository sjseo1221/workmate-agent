import json
import os
import urllib.error
import urllib.request


BASE_URL = os.getenv("WORKMATE_TEST_BASE_URL", "http://localhost:8001").rstrip("/")
TOKEN = os.getenv("WORKMATE_SERVICE_TOKEN", "")


def request(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    a2a_version: str = "1.0",
) -> tuple[int, dict]:
    headers = {"A2A-Version": a2a_version}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/a2a+json"
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def envelope(skill_id: str) -> dict:
    return {
        "message": {
            "messageId": f"msg-{skill_id}",
            "role": "ROLE_USER",
            "parts": [
                {
                    "data": {"skill_id": skill_id, "user_id": "smoke-user"},
                    "mediaType": "application/json",
                }
            ],
        },
        "metadata": {"request_id": f"req-{skill_id}"},
    }


def main() -> None:
    if not TOKEN:
        raise SystemExit("WORKMATE_SERVICE_TOKEN must be set")

    assert request("/health/live")[0] == 200
    assert request("/health/ready")[0] == 200

    status, card = request("/.well-known/agent-card.json")
    assert status == 200
    skills = {skill["id"] for skill in card["skills"]}
    assert len(skills) == 5

    assert request("/a2a/message:send", "POST", envelope("daily_briefing"))[0] == 401
    assert request("/a2a/message:send", "POST", envelope("daily_briefing"), "wrong-token")[0] == 401
    assert request("/a2a/message:send", "POST", envelope("daily_briefing"), TOKEN, "2.0")[0] == 400
    assert request("/a2a/message:send", "POST", envelope("unknown"), TOKEN)[0] == 400

    for skill_id in skills:
        status, result = request("/a2a/message:send", "POST", envelope(skill_id), TOKEN)
        assert status == 200
        task = result["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["artifacts"][0]["parts"][1]["data"]["result"]["type"] == skill_id
        assert request(f"/a2a/tasks/{task['id']}", token=TOKEN)[0] == 200

    assert request("/a2a/tasks/missing", token=TOKEN)[0] == 404
    assert request(f"/a2a/tasks/{task['id']}:cancel", "POST", token=TOKEN)[0] == 409
    print(f"PASS: Workmate A2A smoke test ({BASE_URL}, {len(skills)} skills)")


if __name__ == "__main__":
    main()
