"""Recommendation structured-output model: score/verdict alignment.

The local model occasionally emits a confident score paired with the
opposite verdict (~8% in the quality-gate sample, e.g. 0.9 with
recommend=false for an already-watched title). The validator clamps the
score to the verdict's side — the verdict is the user-facing signal, the
score only ranks the queue.
"""

from indexer_utils.ai_tools.agent import Recommendation


def test_high_score_clamped_on_reject() -> None:
    r = Recommendation(recommend=False, score=0.9, reason="already watched")
    assert r.recommend is False
    assert r.score == 0.49


def test_low_score_clamped_on_recommend() -> None:
    r = Recommendation(recommend=True, score=0.2, reason="strong taste match")
    assert r.recommend is True
    assert r.score == 0.5


def test_consistent_pairs_pass_through() -> None:
    assert Recommendation(recommend=True, score=0.8, reason="x").score == 0.8
    assert Recommendation(recommend=False, score=0.3, reason="x").score == 0.3


def test_boundary_scores() -> None:
    assert Recommendation(recommend=True, score=0.5, reason="x").score == 0.5
    assert Recommendation(recommend=False, score=0.49, reason="x").score == 0.49


def test_extremes_still_validated() -> None:
    assert Recommendation(recommend=True, score=1.0, reason="x").score == 1.0
    assert Recommendation(recommend=False, score=0.0, reason="x").score == 0.0
