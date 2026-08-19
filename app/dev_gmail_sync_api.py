"""실제 Gmail 계정으로 제안함(Proposal) 파이프라인을 수동으로 검증하는 개발용 API.

운영 자동 동기화 Job은 아직 없다(`ExternalSyncWorkflow.sync_gmail`는 있지만
어떤 HTTP Route·스케줄러에도 연결되지 않았다). 이 Route는 `../google-oauth-test`에서
이미 동의를 마친 실제 Google 계정 Credential로 최근 메일을 읽어 `proposal_hub`에
발행해, 실제 Gmail 데이터가 `/api/v1/notifications/stream` → 제안함 UI까지
흐르는지 눈으로 확인하는 용도다.

기본값은 비활성화이며 `WORKMATE_DEV_GMAIL_SYNC_ENABLED=true`일 때만 열린다.
Client Secret과 Refresh Token은 이 모듈이 저장하거나 새로 발급하지 않고
`google-oauth-test`가 이미 만든 `token.json`을 읽기만 한다.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from google.auth.transport.requests import AuthorizedSession

from app.email_action_items import EmailActionItemProviderError, extract_action_items
from app.internal_chat import _authenticated_user_or_assignee
from app.proposal_api import ignored_proposal_repository, publish_email_proposal
from app.providers.google import GmailAdapter, GoogleProviderError, SyncCursorExpiredError
from app.providers.google_auth import (
    GoogleCredentialError,
    build_authorized_session,
    build_authorized_session_for_user,
)
from app.task_api import task_repository

logger = logging.getLogger("workmate-agent.dev-gmail-sync")

DEV_GMAIL_SYNC_ENABLED_ENV = "WORKMATE_DEV_GMAIL_SYNC_ENABLED"
CLIENT_SECRET_FILE_ENV = "GOOGLE_CLIENT_SECRET_FILE"
TOKEN_FILE_ENV = "GOOGLE_TOKEN_FILE"
GMAIL_PUSH_TOPIC_ENV = "WORKMATE_DEV_GMAIL_PUSH_TOPIC"
GMAIL_PUSH_SECRET_ENV = "WORKMATE_DEV_GMAIL_PUSH_SECRET"
_DEFAULT_OAUTH_TEST_DIR = Path(__file__).resolve().parents[2] / "google-oauth-test"
_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

router = APIRouter(prefix="/api/v1/dev", tags=["dev-gmail-sync"])

# Gmail Watch 등록 상태(Email → 소유 사용자·마지막 History ID).
#
# 프로세스 메모리에만 두는 개발용 상태다. 운영에서는 SyncStateRepository처럼
# 영속 저장소에 둬야 하며, Watch는 최대 7일마다 재등록해야 만료되지 않는다.
_watch_state: dict[str, dict[str, str]] = {}


def _enabled() -> bool:
    return os.getenv(DEV_GMAIL_SYNC_ENABLED_ENV, "").lower() == "true"


def _credential_paths() -> tuple[Path, Path]:
    secret = Path(os.getenv(CLIENT_SECRET_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "client_secret.json")))
    token = Path(os.getenv(TOKEN_FILE_ENV, str(_DEFAULT_OAUTH_TEST_DIR / "token.json")))
    return secret, token


def _load_authorized_session(user_id: str) -> AuthorizedSession:
    """`user_id`가 개별로 연결한 Google 계정이 있으면 그걸 쓰고, 없으면 기존 공유
    `token.json`(고정 데모 계정)으로 폴백한다.

    2026-08-18, "제안함" 화면에 `app/google_oauth_web.py`의 웹 OAuth 연결(Google
    로그인 화면으로 이동해 자기 계정으로 동의)을 추가하며 확장 — 아직 아무도 개별
    연결을 안 했으면 예전과 완전히 같게(공유 계정) 동작한다(원칙: 기존 기능
    비영향). 서버 프로세스에는 대화형 브라우저가 없으므로, 공유 계정 Token도 없거나
    Refresh에 실패하면 안내하고 503으로 거부한다. 실제 Credential 로드·갱신은
    `app/providers/google_auth.py`가 공통으로 담당한다 — Refresh Token이 있으면
    `credentials.expired` 값과 무관하게 매번 강제로 갱신한다(그 모듈의 Docstring
    참고, 저장된 만료 시각이 실제 유효성을 보장하지 않아서다. 2026-08-17, "Google
    authorization required" 반복 발생 근본 수정).
    """

    try:
        return build_authorized_session_for_user(user_id, _SCOPES)
    except GoogleCredentialError:
        pass  # 개별 연결 없음 — 아래 공유 계정으로 폴백

    secret_path, token_path = _credential_paths()
    if not token_path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "연결된 Google 계정이 없습니다. 화면에서 'Google 계정 연결'을 누르세요. "
                f"(개발 환경 공유 계정을 쓰려면 {token_path}가 있는지 확인 — "
                "google-oauth-test에서 `python auth_flow.py` 먼저 실행)"
            ),
        )
    try:
        return build_authorized_session(token_path, _SCOPES)
    except GoogleCredentialError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{exc} 화면에서 'Google 계정 연결'을 다시 누르거나, google-oauth-test에서 재동의하세요 (Client Secret: {secret_path}).",
        ) from exc


def _already_approved(user_id: str, message_id: str) -> bool:
    """이 메일에서 파생된 Task가 이미 승인돼 있는지 확인한다.

    후보 `source_id`는 `message_id` 그대로(제안 하나) 또는 `message_id:index`
    (LLM이 여러 후보를 뽑았을 때)다. 재동기화 때마다 LLM 추출 순서가 달라질
    수 있어 정확한 index까지는 비교하지 않고, 이 메일에서 나온 Task가
    하나라도 있으면 그 메일 전체를 "이미 처리함"으로 본다 — 사용자가 후보
    일부만 승인하고 나머지는 무시했더라도, 재동기화 버튼을 다시 눌렀을 때
    같은 메일을 통째로 다시 검토하게 하지 않기 위해서다.
    """

    return any(
        task.source_type == "email" and (task.source_id == message_id or (task.source_id or "").startswith(f"{message_id}:"))
        for task in task_repository().list(user_id)
    )


def _already_ignored(user_id: str, message_id: str) -> bool:
    """이 메일에서 나온 후보를 사용자가 이미 "무시"했는지 확인한다.

    `_already_approved`와 같은 이유로 메시지 단위로 판단한다 — 후보 중
    하나라도(`message_id` 또는 `message_id:index`) 무시된 적이 있으면 그
    메일 전체를 다시 검토 대상으로 삼지 않는다(2026-08-16, 17번 갭 문서
    #2). 이전엔 이 기록 자체가 없어 재동기화 버튼을 누를 때마다 이미
    무시한 후보가 그대로 다시 노출됐다.
    """

    ignored = ignored_proposal_repository().ignored_source_ids(user_id, "email")
    return any(source_id == message_id or source_id.startswith(f"{message_id}:") for source_id in ignored)


async def _publish_action_items_for_message(user_id: str, adapter: GmailAdapter, message_id: str) -> tuple[list[dict[str, str]], str | None]:
    """한 메일에서 LLM Action Item을 추출해 각각 별도 제안으로 발행한다.

    메일 하나가 제안 하나가 아니다 — LLM이 "할 일로 등록할 가치가 있다"고
    판단한 항목만, 있는 만큼(0개~여러 개) 별도 제안으로 만든다. Gmail
    조회나 LLM 호출이 실패하면 예전처럼 메일 제목을 그대로 제안으로 만드는
    방식으로 조용히 되돌아가지 않고, 아무것도 발행하지 않은 채 실패 사유를
    반환한다 — 실패를 성공처럼 보이게 하지 않기 위해서다.
    """

    try:
        message, body = adapter.get_message_with_body(message_id)
    except GoogleProviderError as exc:
        return [], f"gmail_fetch_failed: {exc}"
    try:
        items = extract_action_items(message.subject or "", body)
    except EmailActionItemProviderError as exc:
        logger.warning("email_action_items_failed message_id=%s error=%s", message_id, exc)
        return [], f"llm_extraction_failed: {exc}"

    published: list[dict[str, str]] = []
    for index, item in enumerate(items):
        source_id = f"{message_id}:{index}"
        await publish_email_proposal(user_id, message, title=item.title, source_id=source_id, reason=item.reason)
        published.append({"message_id": message_id, "source_id": source_id, "title": item.title})
    return published, None


@router.post("/gmail-sync")
async def trigger_gmail_sync(
    limit: int = Query(default=5, ge=1, le=20),
    user_id: str = Depends(_authenticated_user_or_assignee),
) -> dict[str, Any]:
    """최근 Gmail 메시지를 LLM으로 분석해 할 일 후보만 제안함 SSE로 발행한다."""

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev gmail sync is disabled")
    session = _load_authorized_session(user_id)
    adapter = GmailAdapter(session)
    try:
        message_ids = [item.message_id for item in adapter.list_messages(max_results=limit)]
    except GoogleProviderError as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code >= 400 else 502, detail=str(exc)) from exc

    published: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for message_id in message_ids:
        if _already_approved(user_id, message_id):
            skipped.append({"message_id": message_id, "reason": "already_approved"})
            continue
        if _already_ignored(user_id, message_id):
            skipped.append({"message_id": message_id, "reason": "already_ignored"})
            continue
        items, error_reason = await _publish_action_items_for_message(user_id, adapter, message_id)
        published.extend(items)
        if error_reason:
            skipped.append({"message_id": message_id, "reason": error_reason})

    return {
        "fetched": len(message_ids),
        "published": published,
        "skipped": skipped,
        "note": "LLM이 메일 본문을 분석해 할 일로 등록할 가치가 있는 항목만 제안으로 만들었습니다. 광고·전달·실패 알림 등은 제외됩니다.",
    }


@router.post("/gmail-watch/register")
async def register_gmail_watch(user_id: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """Gmail Pub/Sub Watch를 등록하고 이후 Push를 받을 소유 사용자를 기록한다.

    Google Cloud Pub/Sub Topic은 이 Route가 만들지 않는다(사전에 Topic 생성과
    `gmail-api-push@system.gserviceaccount.com`의 Publisher 권한 부여가
    끝나 있어야 한다). Watch는 최대 7일 뒤 만료되므로 재등록이 필요하다.
    """

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev gmail sync is disabled")
    topic = os.getenv(GMAIL_PUSH_TOPIC_ENV, "").strip()
    if not topic:
        raise HTTPException(status_code=503, detail=f"{GMAIL_PUSH_TOPIC_ENV}가 설정되지 않았습니다.")
    session = _load_authorized_session(user_id)
    adapter = GmailAdapter(session)
    profile = adapter.profile()
    try:
        watch = adapter.watch(topic)
    except GoogleProviderError as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code >= 400 else 502, detail=str(exc)) from exc
    email = str(profile.get("emailAddress", ""))
    history_id = str(watch.get("historyId") or profile.get("historyId", ""))
    _watch_state[email] = {"user_id": user_id, "last_history_id": history_id}
    logger.info(
        "gmail_watch_registered email=%s user_id=%s history_id=%s expiration=%s",
        email, user_id, history_id, watch.get("expiration"),
    )
    return {"email": email, "history_id": history_id, "expiration": watch.get("expiration")}


@router.get("/gmail-watch/status")
def gmail_watch_status(_: str = Depends(_authenticated_user_or_assignee)) -> dict[str, Any]:
    """현재 프로세스가 기억하는 Watch 등록 상태를 그대로 보여준다.

    상태는 메모리에만 있어 백엔드를 재기동하면 비워진다 — "메일이 왔는데
    Push가 안 온 것 같다"를 디버깅할 때 재등록이 필요한지 이 Route로 먼저
    확인한다.
    """

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev gmail sync is disabled")
    return {"watches": [{"email": email, **state} for email, state in _watch_state.items()]}


def _new_message_ids(history_body: dict[str, Any]) -> set[str]:
    """History 응답에서 새로 추가된 메시지 ID만 뽑는다(삭제·라벨 변경 제외)."""

    return {
        str(added["message"]["id"])
        for record in history_body.get("history", [])
        for added in record.get("messagesAdded", [])
        if added.get("message", {}).get("id")
    }


@router.post("/gmail-push")
async def receive_gmail_push(request: Request, secret: str = Query(default="")) -> dict[str, Any]:
    """Google Cloud Pub/Sub Push 구독의 Webhook 대상.

    Pub/Sub Push 요청은 우리 OIDC 인증 체계를 통과할 수 없어(Google이
    `_authenticated_user`가 요구하는 사용자 Bearer Token을 붙일 수 없다)
    쿼리스트링 공유 비밀값으로만 검증한다. 이 값은 구독 생성 시 넣은
    URL에만 있고 로그에 원문 그대로 남지 않도록 각별히 취급해야 하며,
    운영 전환 시에는 Pub/Sub의 OIDC Push 인증으로 교체해야 한다.
    """

    if not _enabled():
        raise HTTPException(status_code=404, detail="dev gmail sync is disabled")
    expected_secret = os.getenv(GMAIL_PUSH_SECRET_ENV, "")
    if not expected_secret or not hmac.compare_digest(secret, expected_secret):
        raise HTTPException(status_code=401, detail="invalid push secret")

    envelope = await request.json()
    data_b64 = str(envelope.get("message", {}).get("data", ""))
    try:
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - Pub/Sub 페이로드 형식 오류를 하나로 통일한다.
        raise HTTPException(status_code=400, detail=f"invalid Pub/Sub payload: {exc}") from exc

    email = str(payload.get("emailAddress", ""))
    new_history_id = str(payload.get("historyId", ""))
    logger.info("gmail_push_received email=%s history_id=%s", email, new_history_id)
    state = _watch_state.get(email)
    if state is None:
        # 등록되지 않은 계정의 알림은 조용히 ACK한다 — 501/4xx로 거부하면
        # Pub/Sub가 동일 메시지를 계속 재시도해 쌓이게 된다. 백엔드를
        # 재기동하면 Watch 상태가 비워지므로 재기동 뒤 첫 Push는 대부분
        # 여기로 온다 — `/gmail-watch/register`를 다시 호출해야 한다.
        logger.warning("gmail_push_ignored email=%s reason=no_watch_registered", email)
        return {"status": "ignored", "reason": "no watch registered for this email"}

    session = _load_authorized_session(state["user_id"])
    adapter = GmailAdapter(session)
    try:
        history_body = adapter.history(state["last_history_id"])
    except SyncCursorExpiredError:
        logger.warning("gmail_push_cursor_reset email=%s history_id=%s", email, new_history_id)
        state["last_history_id"] = new_history_id
        return {"status": "cursor_reset", "history_id": new_history_id}
    except GoogleProviderError as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code >= 400 else 502, detail=str(exc)) from exc

    new_ids = _new_message_ids(history_body)
    published: list[dict[str, str]] = []
    for message_id in new_ids:
        items, error_reason = await _publish_action_items_for_message(state["user_id"], adapter, message_id)
        published.extend(items)
        if error_reason:
            logger.warning("gmail_push_action_items_failed email=%s message_id=%s reason=%s", email, message_id, error_reason)

    state["last_history_id"] = new_history_id
    logger.info("gmail_push_published email=%s new_messages=%s published=%s", email, len(new_ids), len(published))
    return {"status": "ok", "published": published}


__all__ = [
    "router",
    "DEV_GMAIL_SYNC_ENABLED_ENV",
    "CLIENT_SECRET_FILE_ENV",
    "TOKEN_FILE_ENV",
    "GMAIL_PUSH_TOPIC_ENV",
    "GMAIL_PUSH_SECRET_ENV",
]
