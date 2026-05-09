import base64
import email
from email import policy
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mailbridge.emailer import (
    GmailClient,
    _xoauth2,
    _xoauth2_b64,
    canonical_email_address,
    email_addresses_match,
    normalize_message_ids,
    save_attachments,
)


def test_xoauth2_imap_payload_is_raw_bytes() -> None:
    payload = _xoauth2("user@example.com", "token123")
    assert payload == b"user=user@example.com\x01auth=Bearer token123\x01\x01"


def test_xoauth2_smtp_payload_is_base64_encoded() -> None:
    encoded = _xoauth2_b64("user@example.com", "token123")
    assert base64.b64decode(encoded) == b"user=user@example.com\x01auth=Bearer token123\x01\x01"


def test_canonical_email_address_normalizes_gmail_aliases() -> None:
    assert canonical_email_address("Dhruv.Saini+canned.response@googlemail.com") == "dhruvsaini@gmail.com"


def test_email_addresses_match_accepts_gmail_plus_aliases() -> None:
    assert email_addresses_match("dhruv9saini+canned.response@gmail.com", "dhruv9saini@gmail.com")


def test_normalize_message_ids_extracts_and_deduplicates_headers() -> None:
    assert normalize_message_ids(
        [
            "<root@msg> <mid@msg>",
            "text <mid@msg> <leaf@msg>",
            None,
            " <leaf@msg> ",
        ]
    ) == ["<root@msg>", "<mid@msg>", "<leaf@msg>"]


def test_open_imap_sets_timeout(monkeypatch) -> None:
    calls: list[tuple[str, int, int | None]] = []

    class _FakeImap:
        def select(self, mailbox: str):
            assert mailbox == "INBOX"
            return "OK", [b""]

    def _fake_imap(host: str, port: int, *, timeout: int | None = None):
        calls.append((host, port, timeout))
        return _FakeImap()

    client = object.__new__(GmailClient)
    client.config = SimpleNamespace(gmail=SimpleNamespace(imap_host="imap.example.com", imap_port=993))
    client.auth = SimpleNamespace(imap_login=lambda imap: None)

    monkeypatch.setattr("codex_mailbridge.emailer.imaplib.IMAP4_SSL", _fake_imap)

    client._open_imap()

    assert calls == [("imap.example.com", 993, 30)]


def test_save_attachments_uses_default_attachment_names(tmp_path: Path) -> None:
    saved_paths, image_paths = save_attachments(
        tmp_path,
        [
            ("photo.png", b"one", "image/png"),
            ("report.pdf", b"two", "application/pdf"),
            ("", b"three", "image/jpeg"),
        ],
    )
    assert [Path(path).name for path in saved_paths] == ["attachment.png", "attachment2.pdf", "attachment3.jpg"]
    assert [Path(path).name for path in image_paths] == ["attachment.png", "attachment3.jpg"]


def test_save_attachments_appends_number_when_name_already_exists(tmp_path: Path) -> None:
    (tmp_path / "attachment.png").write_bytes(b"existing")
    saved_paths, _ = save_attachments(tmp_path, [("photo.png", b"new", "image/png")])
    assert [Path(path).name for path in saved_paths] == ["attachment2.png"]


def test_send_message_uses_gmail_api_thread_id_for_oauth(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class _FakeSession:
        def __init__(self, creds) -> None:
            self.creds = creds

        def post(self, url: str, *, json: dict, timeout: int):
            calls.append(("post", {"url": url, "json": json, "timeout": timeout}))
            return _FakeResponse({"id": "gmail-msg-1", "threadId": "thread-123"})

        def get(self, url: str, *, timeout: int):
            calls.append(("get", {"url": url, "timeout": timeout}))
            return _FakeResponse({"payload": {"headers": [{"name": "Message-ID", "value": "<actual@msg>"}]}})

    monkeypatch.setattr("codex_mailbridge.emailer.AuthorizedSession", _FakeSession)

    client = object.__new__(GmailClient)
    client.config = SimpleNamespace(
        gmail=SimpleNamespace(
            auth_mode="oauth",
            allowed_from="to@example.com",
            address="bridge@example.com",
            user_visible_from="user@example.com",
        )
    )
    client.auth = SimpleNamespace(oauth_credentials=lambda: object())

    message_id = client.send_message(
        to_address="to@example.com",
        subject="Re: subject",
        markdown_body="hello",
        in_reply_to="<parent@msg>",
        references=["<root@msg>"],
        from_address="bridge@example.com",
        sender_address=None,
        reply_to="bridge@example.com",
        gmail_thread_id="thread-123",
    )

    assert message_id == "<actual@msg>"
    post_call = calls[0][1]
    assert post_call["url"].endswith("/users/me/messages/send")
    assert post_call["json"]["threadId"] == "thread-123"

    raw_message = base64.urlsafe_b64decode(post_call["json"]["raw"] + "===")
    parsed = email.message_from_bytes(raw_message, policy=policy.default)
    assert parsed["In-Reply-To"] == "<parent@msg>"
    assert parsed["References"] == "<root@msg> <parent@msg>"


def test_send_message_resolves_api_thread_id_from_parent_message_for_numeric_imap_thread_id(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class _FakeSession:
        def __init__(self, creds) -> None:
            self.creds = creds

        def post(self, url: str, *, json: dict, timeout: int):
            calls.append(("post", {"url": url, "json": json, "timeout": timeout}))
            return _FakeResponse({"id": "gmail-msg-1", "threadId": "api-thread-123"})

        def get(self, url: str, *, timeout: int):
            calls.append(("get", {"url": url, "timeout": timeout}))
            if "messages?q=" in url:
                return _FakeResponse({"messages": [{"id": "gmail-msg-1", "threadId": "api-thread-123"}]})
            return _FakeResponse({"payload": {"headers": [{"name": "Message-ID", "value": "<actual@msg>"}]}})

    monkeypatch.setattr("codex_mailbridge.emailer.AuthorizedSession", _FakeSession)

    client = object.__new__(GmailClient)
    client.config = SimpleNamespace(
        gmail=SimpleNamespace(
            auth_mode="oauth",
            allowed_from="to@example.com",
            address="bridge@example.com",
            user_visible_from="user@example.com",
        )
    )
    client.auth = SimpleNamespace(oauth_credentials=lambda: object())

    message_id = client.send_message(
        to_address="to@example.com",
        subject="Re: subject",
        markdown_body="hello",
        in_reply_to="<parent@msg>",
        references=["<root@msg>"],
        from_address="bridge@example.com",
        sender_address=None,
        reply_to="bridge@example.com",
        gmail_thread_id="1863067398965977352",
    )

    assert message_id == "<actual@msg>"
    get_call = calls[0][1]
    assert "rfc822msgid%3A%3Cparent%40msg%3E" in get_call["url"]
    post_call = calls[1][1]
    assert post_call["json"]["threadId"] == "api-thread-123"


def test_send_message_raises_when_gmail_api_send_fails(monkeypatch) -> None:
    sent = {"count": 0}

    class _FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert host == "smtp.example.com"
            assert port == 587
            assert timeout == 30

        def ehlo(self) -> None:
            return None

        def starttls(self, context) -> None:
            return None

        def send_message(self, msg) -> None:
            sent["count"] += 1

        def quit(self) -> None:
            return None

    monkeypatch.setattr("codex_mailbridge.emailer.smtplib.SMTP", _FakeSMTP)

    client = object.__new__(GmailClient)
    client.config = SimpleNamespace(
        gmail=SimpleNamespace(
            auth_mode="oauth",
            allowed_from="to@example.com",
            address="bridge@example.com",
            user_visible_from="user@example.com",
            smtp_host="smtp.example.com",
            smtp_port=587,
        )
    )
    client.auth = SimpleNamespace(
        oauth_credentials=lambda: object(),
        smtp_login=lambda smtp: None,
    )
    client._send_via_gmail_api = lambda msg, gmail_thread_id, in_reply_to, refs: (_ for _ in ()).throw(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        client.send_message(
            to_address="to@example.com",
            subject="Re: subject",
            markdown_body="hello",
            in_reply_to="<parent@msg>",
            references=["<root@msg>"],
            from_address="bridge@example.com",
            sender_address=None,
            reply_to="bridge@example.com",
            gmail_thread_id="thread-123",
        )

    assert sent["count"] == 0
