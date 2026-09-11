"""增长集合 Explain 基准的安全输出与查询契约测试。"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "scripts" / "benchmark_db_growth.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_db_growth", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_contains_stage = _MODULE._contains_stage
query_cases = _MODULE.query_cases
summarize_explain = _MODULE.summarize_explain


def test_summarize_explain_detects_ixscan_and_hides_plan_details():
    """只输出扫描统计，不把完整计划树或查询正文带到报告。"""
    summary = summarize_explain({
        "queryPlanner": {"winningPlan": {"stage": "FETCH", "inputStage": {"stage": "IXSCAN"}}},
        "executionStats": {"nReturned": 20, "totalKeysExamined": 20, "totalDocsExamined": 20,
                            "executionTimeMillis": 2},
    })
    assert summary == {"plan": "IXSCAN", "nReturned": 20, "totalKeysExamined": 20,
                       "totalDocsExamined": 20, "executionTimeMillis": 2, "selective": True}
    assert "winningPlan" not in summary


def test_summarize_explain_classifies_collection_scan():
    """缺少索引时报告 COLLSCAN，作为需调整仓储索引的直接证据。"""
    summary = summarize_explain({"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}},
                                 "executionStats": {"nReturned": 1, "totalDocsExamined": 10000}})
    assert summary["plan"] == "COLLSCAN"
    assert summary["selective"] is False


def test_query_cases_have_bounded_stable_pagination():
    """所有增长查询均声明上限和稳定排序，避免无界读取。"""
    cases = query_cases()
    assert set(cases) == {"authentication_recent", "authentication_deep_page", "audit_filtered",
                          "runtime_filtered", "request_filtered"}
    for case in cases.values():
        assert 0 <= case["skip"]
        assert 1 <= case["limit"] <= 100
        assert case["sort"]["createdAt"] == -1
        assert case["collection"] in {"authentication_records", "audit", "events", "request_events"}


def test_contains_stage_handles_nested_sbe_plan():
    """Mongo 不同版本的 explain 计划嵌套形式都能被识别。"""
    assert _contains_stage({"queryPlan": {"stage": "IXSCAN"}}, "IXSCAN")
    assert not _contains_stage({"stage": "FETCH", "inputStage": {"stage": "COLLSCAN"}}, "IXSCAN")
