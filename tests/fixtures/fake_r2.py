"""실제 R2를 호출하지 않는 `R2StorageAdapter` 대역.

`put_object`/`head`/`get_object`만 인메모리 dict로 구현한다 — 회의 녹음
업로드·실시간 병합 테스트가 네트워크 없이 빠르게 돌게 하기 위함이다. 실제
R2 연결 자체는 `tests/test_r2_storage.py`가 `r2-storage-test/.env` 자격증명으로
별도 검증한다.
"""

from __future__ import annotations


class FakeR2Storage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_object(self, key: str, data: bytes, content_type: str) -> dict[str, object]:
        self.objects[key] = (data, content_type)
        return {"ETag": '"fake-etag"'}

    def head(self, key: str) -> dict[str, object]:
        if key not in self.objects:
            raise KeyError(f"object not found: {key}")
        data, content_type = self.objects[key]
        return {"ContentLength": len(data), "ContentType": content_type}

    def get_object(self, key: str) -> bytes:
        return self.objects[key][0]
