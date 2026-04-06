from pathlib import Path

import pytest

from codex_mailbridge.daemon import SubjectParseError, normalize_workspace_path, parse_subject


def test_parse_simple_subject() -> None:
    path, agent_id = parse_subject("~/coding/catbench new feature")
    assert path == "~/coding/catbench"
    assert agent_id == "new feature"


def test_parse_quoted_subject() -> None:
    path, agent_id = parse_subject('"~/coding/with spaces" agent id with spaces')
    assert path == "~/coding/with spaces"
    assert agent_id == "agent id with spaces"


def test_reject_missing_agent_id() -> None:
    with pytest.raises(SubjectParseError):
        parse_subject("~/coding/catbench")


def test_accept_home_root_workspace() -> None:
    path, agent_id = parse_subject("~ test")
    assert path == "~"
    assert agent_id == "test"
    assert normalize_workspace_path("~") == Path("/home/d")


def test_reject_relative_path() -> None:
    with pytest.raises(SubjectParseError):
        normalize_workspace_path("coding/catbench")
