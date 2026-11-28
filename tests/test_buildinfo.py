import tomllib
from pathlib import Path

from deedoop import __version__
from deedoop.buildinfo import build_label


def test_version_matches_pyproject():
    root = Path(__file__).resolve().parent.parent
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__


def test_label_without_build_info_is_the_version():
    assert build_label({}) == __version__


def test_label_shows_short_commit_and_date():
    env = {"DEEDOOP_COMMIT": "1ae420bf417b93ca5d276c6ff14a159f19eb452d", "DEEDOOP_BUILD_DATE": "2026-11-28"}
    assert build_label(env) == f"{__version__} (1ae420b, 2026-11-28)"


def test_label_ignores_values_that_are_not_a_commit_or_a_date():
    env = {"DEEDOOP_COMMIT": "<script>x</script>", "DEEDOOP_BUILD_DATE": "yesterday"}
    assert build_label(env) == __version__


def test_footer_shows_the_build(unraid, monkeypatch):
    from tests.test_web import make_client

    monkeypatch.setenv("DEEDOOP_COMMIT", "1ae420b")
    monkeypatch.setenv("DEEDOOP_BUILD_DATE", "2026-11-28")
    with make_client(unraid) as client:
        assert f"deedoop {__version__} (1ae420b, 2026-11-28)" in client.get("/").text
        assert client.get("/api/status").json()["build"] == f"{__version__} (1ae420b, 2026-11-28)"
