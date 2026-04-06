import base64
from pathlib import Path

from codex_mailbridge.emailer import _xoauth2, _xoauth2_b64, save_attachments


def test_xoauth2_imap_payload_is_raw_bytes() -> None:
    payload = _xoauth2("user@example.com", "token123")
    assert payload == b"user=user@example.com\x01auth=Bearer token123\x01\x01"


def test_xoauth2_smtp_payload_is_base64_encoded() -> None:
    encoded = _xoauth2_b64("user@example.com", "token123")
    assert base64.b64decode(encoded) == b"user=user@example.com\x01auth=Bearer token123\x01\x01"


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
