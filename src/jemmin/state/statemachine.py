from __future__ import annotations

from jemmin.models import ReviewState


ALLOWED_TRANSITIONS: dict[ReviewState, set[ReviewState]] = {
    ReviewState.QUEUED: {ReviewState.PRECHECK_FAILED, ReviewState.CONTEXT_READY, ReviewState.FAILED},
    ReviewState.PRECHECK_FAILED: set(),
    ReviewState.CONTEXT_READY: {ReviewState.ANALYZING, ReviewState.DEGRADED, ReviewState.FAILED},
    ReviewState.ANALYZING: {ReviewState.CONSENSUS_REACHED, ReviewState.DEGRADED, ReviewState.FAILED},
    ReviewState.CONSENSUS_REACHED: {ReviewState.PATCH_PROPOSED, ReviewState.DELIVERED, ReviewState.FAILED},
    ReviewState.PATCH_PROPOSED: {ReviewState.VERIFIED, ReviewState.FAILED},
    ReviewState.VERIFIED: {ReviewState.DELIVERED, ReviewState.FAILED},
    ReviewState.DELIVERED: set(),
    ReviewState.DEGRADED: {ReviewState.DELIVERED, ReviewState.FAILED},
    ReviewState.FAILED: set(),
}


def can_transition(from_state: ReviewState, to_state: ReviewState) -> bool:
    return to_state in ALLOWED_TRANSITIONS[from_state]
