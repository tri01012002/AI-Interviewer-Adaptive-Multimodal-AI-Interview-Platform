import math

from rag.evaluation import ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank


def test_ranking_metrics_handle_empty_relevance_and_short_results():
    assert recall_at_k(["a"], [], 3) == 0.0
    assert precision_at_k([], {"a"}, 3) == 0.0
    assert reciprocal_rank(["b"], {"a"}) == 0.0
    assert ndcg_at_k(["a"], {}, 3) == 0.0


def test_ranking_metrics_are_correct_at_configured_k():
    ranked = ["wrong", "relevant", "other"]
    relevance = {"relevant": 3, "other": 1}

    assert recall_at_k(ranked, relevance, 1) == 0.0
    assert recall_at_k(ranked, relevance, 3) == 1.0
    assert precision_at_k(ranked, relevance, 3) == 2 / 3
    assert reciprocal_rank(ranked, relevance) == 0.5
    expected = ((2**0 - 1) / math.log2(2) + (2**3 - 1) / math.log2(3) + (2**1 - 1) / math.log2(4))
    ideal = ((2**3 - 1) / math.log2(2) + (2**1 - 1) / math.log2(3))
    assert ndcg_at_k(ranked, relevance, 3) == expected / ideal