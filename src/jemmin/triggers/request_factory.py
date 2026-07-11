from __future__ import annotations

from typing import Iterable

from jemmin.models import ReviewRequest

from .base import TriggerAdapter, TriggerEvent


class ReviewRequestFactory:
    """Dispatches TriggerEvent objects to the first adapter that supports them."""

    def __init__(self, adapters: Iterable[TriggerAdapter]) -> None:
        self._adapters = list(adapters)

    def build_request(self, event: TriggerEvent) -> ReviewRequest:
        for adapter in self._adapters:
            if adapter.supports(event):
                return adapter.build_request(event)
        raise ValueError(f"no trigger adapter registered for source={event.source!r}")