from codex_mailbridge.daemon import (
    _extract_latest_reply_text,
    _format_turn_failure,
    normalize_workspace_path,
)


def test_extract_latest_reply_text_keeps_literal_message_body() -> None:
    body = "please keep this exact block\n\n```bash\nls -1\n```\n\nthanks"
    assert _extract_latest_reply_text(body) == body


def test_extract_latest_reply_text_strips_gmail_quoted_history() -> None:
    body = (
        "fix and finish the whole thing\n\n"
        "On Wed, Apr 1, 2026 at 5:33 PM Dhruv Saini <dhruv9saini@gmail.com> wrote:\n"
        "> older text\n"
    )
    assert _extract_latest_reply_text(body) == "fix and finish the whole thing"


def test_extract_latest_reply_text_strips_forwarded_headers() -> None:
    body = "new request\n\nFrom: Dhruv Saini <dhruv9saini@gmail.com>\nSent: today\n"
    assert _extract_latest_reply_text(body) == "new request"


def test_format_turn_failure_wraps_error_text() -> None:
    assert _format_turn_failure("refresh failed") == "Codex error:\n\nrefresh failed"


def test_home_root_workspace_is_still_allowed() -> None:
    assert normalize_workspace_path("~").name == "d"
