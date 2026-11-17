"""Tests for reading settings from a .env file."""

from fieldnote.config import load_settings, read_env_file


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_key_value_lines(tmp_path):
    path = write(
        tmp_path,
        "# a comment\n"
        "\n"
        "LLM_PROVIDER=claude\n"
        "  LLM_MODEL = claude-opus-5-5  \n"
        'QUOTED="hello world"\n'
        "SINGLE='x=y'\n"
        "EMPTY=\n"
        "URL=https://example.com/?a=b\n"
        "not a setting\n"
        "=no key\n",
    )
    assert read_env_file(path) == {
        "LLM_PROVIDER": "claude",
        "LLM_MODEL": "claude-opus-5-5",
        "QUOTED": "hello world",
        "SINGLE": "x=y",
        "EMPTY": "",
        "URL": "https://example.com/?a=b",
    }


def test_missing_file_gives_no_settings(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_values_are_not_evaluated_or_expanded(tmp_path):
    path = write(tmp_path, "A=$HOME\nB=$(whoami)\nC=`date`\n")
    assert read_env_file(path) == {"A": "$HOME", "B": "$(whoami)", "C": "`date`"}


def test_real_environment_wins_over_the_file(tmp_path):
    path = write(tmp_path, "LLM_PROVIDER=claude\nLLM_MODEL=from-file\n")
    settings = load_settings(path, environ={"LLM_MODEL": "from-environment", "OTHER": "1"})
    assert settings == {"LLM_PROVIDER": "claude", "LLM_MODEL": "from-environment", "OTHER": "1"}
