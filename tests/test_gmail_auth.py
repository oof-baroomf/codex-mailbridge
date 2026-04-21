import base64
from pathlib import Path
from types import SimpleNamespace

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
        def select(self, mailbox: str) -> None:
            assert mailbox == "INBOX"

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
