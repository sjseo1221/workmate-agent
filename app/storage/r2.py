"""Cloudflare R2 S3 호환 Storage Adapter."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    region: str = "auto"

    @classmethod
    def from_env(cls) -> "R2Config":
        """R2 환경 변수를 읽고 Secret 값은 반환 결과에 노출하지 않는다."""
        env_to_field = {
            "R2_ENDPOINT": "endpoint",
            "R2_ACCESS_KEY_ID": "access_key_id",
            "R2_SECRET_ACCESS_KEY": "secret_access_key",
            "R2_BUCKET": "bucket",
        }
        values = {field: os.getenv(env_name, "") for env_name, field in env_to_field.items()}
        missing = [env_name for env_name, field in env_to_field.items() if not values[field]]
        if missing:
            raise RuntimeError("R2 configuration is missing: " + ", ".join(missing))
        return cls(**values, region=os.getenv("R2_REGION", "auto"))


class R2StorageAdapter:
    """Private R2 버킷의 presigned URL과 checksum 경계를 제공한다."""

    def __init__(self, config: R2Config):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for R2StorageAdapter") from exc
        self.config = config
        self.client = boto3.client("s3", endpoint_url=config.endpoint, aws_access_key_id=config.access_key_id, aws_secret_access_key=config.secret_access_key, region_name=config.region)

    def object_key(self, user_id: str, meeting_id: str, sha256: str) -> str:
        """사용자·회의 범위의 비밀 없는 Object Key를 생성한다."""
        return f"meetings/{user_id}/{meeting_id}/{sha256}.audio"

    def presigned_put(self, key: str, content_type: str, expires: int = 900) -> str:
        """단기 업로드 URL을 생성하며 URL을 DB에 저장하지 않는다."""
        return self.client.generate_presigned_url("put_object", Params={"Bucket": self.config.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=expires)

    def put_object(self, key: str, data: bytes, content_type: str) -> dict[str, object]:
        """서버가 받은 파일 바이트를 직접 R2에 저장한다.

        `workmate-ui`는 `multipart/form-data`로 Workmate 서버에 파일을 올리고
        서버가 R2에 저장하는 방식을 쓴다(Presigned 직접 업로드 아님,
        2026-08-15 결정 — `workmate-ui-integration-gaps.md` #14). 브라우저에는
        R2 자격 증명이나 Presigned URL을 전달하지 않는다.
        """
        return self.client.put_object(Bucket=self.config.bucket, Key=key, Body=data, ContentType=content_type)

    def get_object(self, key: str) -> bytes:
        """STT 등 서버 내부 처리를 위해 R2에 저장된 원본 바이트를 내려받는다."""
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        return response["Body"].read()

    def head(self, key: str) -> dict[str, object]:
        """Object 존재·크기·ETag 메타데이터를 확인한다."""
        return self.client.head_object(Bucket=self.config.bucket, Key=key)

    @staticmethod
    def checksum(data: bytes) -> str:
        """업로드 전송 데이터의 SHA-256 checksum을 계산한다."""
        return hashlib.sha256(data).hexdigest()
