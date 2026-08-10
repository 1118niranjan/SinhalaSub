"""Shared test helpers.

Adds the project root to sys.path so `from sinhalasub import ...` resolves when
pytest is run from anywhere in the repository.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or ""

    def json(self):
        return self._json


def make_response(status_code=200, json_data=None, text=""):
    """Build a stand-in for requests.Response for provider tests."""
    return FakeResponse(status_code, json_data, text)
