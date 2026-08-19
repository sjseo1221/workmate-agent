"""M4.5 실제 64 Chunk·60 Case 검색 평가 원본의 계약 검증."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = ROOT / "embedding-evaluation" / "corpus" / "real_meeting_chunks.jsonl"
CASES_PATH = ROOT / "embedding-evaluation" / "cases" / "real_cases_60.jsonl"
METRICS_PATH = ROOT / "embedding-evaluation" / "results" / "postgres-metrics-20260811-101253.json"


def load_jsonl(path: Path) -> list[dict[str, object]]:
    """평가 JSONL을 읽고 각 행을 객체로 반환한다."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class M45SearchEvaluationTests(unittest.TestCase):
    """승인된 실제 검색 평가가 현재 M4.5 통과 기준을 만족하는지 확인한다."""

    def test_corpus_case_integrity_and_thresholds(self) -> None:
        corpus = load_jsonl(CORPUS_PATH)
        cases = load_jsonl(CASES_PATH)
        metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        chunk_ids = {str(row["chunk_id"]) for row in corpus}
        meeting_ids = {str(row["meeting_id"]) for row in corpus}
        self.assertEqual(len(corpus), 64)
        self.assertEqual(len(cases), 60)
        self.assertEqual(len(chunk_ids), 64)
        self.assertTrue(all(str(case["case_id"]) for case in cases))
        for case in cases:
            self.assertTrue(set(case.get("relevant_chunk_ids", [])) <= chunk_ids)
            self.assertTrue(set(case.get("relevant_meeting_ids", [])) <= meeting_ids)
        self.assertGreaterEqual(metrics["rrf_hnsw_trigram"]["hit_at_3"], 0.80)
        self.assertGreaterEqual(metrics["mean_hnsw_recall_at_5"], 0.90)
        self.assertLessEqual(metrics["latency"]["hybrid_sequential_db"]["p95_ms"], 300)
        self.assertLessEqual(metrics["latency"]["query_embedding"]["p95_ms"], 2000)
        self.assertTrue(metrics["hnsw_index_used"])


if __name__ == "__main__":
    unittest.main()
