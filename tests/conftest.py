import pytest

from kanbeat.diff import Changes


@pytest.fixture(autouse=True)
def no_git_polling(request, monkeypatch):
    if request.module.__name__.endswith("test_diff_view"):
        return

    async def collect(project, previous="", source="current"):
        return Changes(signature="unavailable")

    monkeypatch.setattr("kanbeat.ui.app.collect", collect)
