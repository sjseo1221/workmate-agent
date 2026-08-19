"""M3.6 R2 Adapter 계약 테스트."""

import os
import unittest

from app.storage.r2 import R2Config, R2StorageAdapter


class R2StorageTests(unittest.TestCase):
    def test_key_and_checksum_are_deterministic(self):
        self.assertEqual(R2StorageAdapter.checksum(b"audio"), R2StorageAdapter.checksum(b"audio"))
        config = R2Config("https://example.invalid", "key", "secret", "workmate")
        adapter = object.__new__(R2StorageAdapter)
        adapter.config = config
        self.assertTrue(adapter.object_key("u1", "m1", "abc").startswith("meetings/u1/m1/"))

    def test_missing_config_is_rejected(self):
        for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            os.environ.pop(name, None)
        with self.assertRaises(RuntimeError):
            R2Config.from_env()

    def test_from_env_maps_env_var_names_to_dataclass_fields(self):
        """회귀 테스트 — env var 이름(R2_ENDPOINT)을 그대로 키워드 인자로 넘겨
        `endpoint` 필드와 안 맞던 버그(2026-08-15 실제 R2 연동 검증 중 발견)."""
        previous = {name: os.environ.get(name) for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_REGION")}
        try:
            os.environ["R2_ENDPOINT"] = "https://example.invalid"
            os.environ["R2_ACCESS_KEY_ID"] = "key-1"
            os.environ["R2_SECRET_ACCESS_KEY"] = "secret-1"
            os.environ["R2_BUCKET"] = "bucket-1"
            os.environ.pop("R2_REGION", None)
            config = R2Config.from_env()
            self.assertEqual(config.endpoint, "https://example.invalid")
            self.assertEqual(config.access_key_id, "key-1")
            self.assertEqual(config.secret_access_key, "secret-1")
            self.assertEqual(config.bucket, "bucket-1")
            self.assertEqual(config.region, "auto")
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
