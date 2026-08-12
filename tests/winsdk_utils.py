"""Minimal winsdk async-operation stand-in for tests.

winsdk's IAsyncOperation is a COM object we can't construct by hand.
For tests we substitute a tiny sync wrapper with the same surface the
app uses: .completed assignment + .get_results().
"""
from __future__ import annotations


class CompletedAsync:
    """An IAsyncOperation that is already complete."""

    def __init__(self, result):
        self._result = result
        self.completed = None

    def get_results(self):
        if self.completed is not None:
            try:
                self.completed(None, None)
            except Exception:
                pass
        return self._result
