"""Incremental sales package extracted from ``sales_agent`` (Phase 11).

Public entry points remain on ``app.sales_agent`` for compatibility.
"""

from .policies.confirmation import confirmation_text_kind
from .workflows.catalog_ranking import rank_candidates, score_candidate

__all__ = [
    "confirmation_text_kind",
    "rank_candidates",
    "score_candidate",
]
