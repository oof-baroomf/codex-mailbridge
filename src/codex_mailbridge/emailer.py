from __future__ import annotations

import base64
from dataclasses import dataclass
import email
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from html.parser import HTMLParser
import imaplib
import logging
import mimetypes
from pathlib import Path
import re
import smtplib
import ssl
from typing import Iterable
import tomllib
from urllib.parse import urlencode

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import markdown
import tomli_w

from .config import Config


LOG = logging.getLogger(__name__)
MAIL_SCOPE = ["https://mail.google.com/"]
IMAP_TIMEOUT_SECONDS = 30
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
_MESSAGE_ID_PATTERN = re.compile(r"<[^<>]+>")


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _xoauth2(user: str, access_token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def _xoauth2_b64(user: str, access_token: str) -> str:
    return base64.b64encode(_xoauth2(user, access_token)).decode("ascii")


def canonical_email_address(address: str) -> str:
    mailbox = parseaddr(address)[1].strip().lower()
    if not mailbox or "@" not in mailbox:
        return mailbox
    local, domain = mailbox.split("@", 1)
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def email_addresses_match(left: str, right: str) -> bool:
    return canonical_email_address(left) == canonical_email_address(right)


class GmailAuth:
    def __init__(self, config: Config) -> None:
        self.config = config

    def oauth_credentials(self) -> Credentials:
        oauth = self.config.gmail.oauth
        creds = Credentials(
            token=None,
            refresh_token=oauth.refresh_token,
            token_uri=oauth.token_uri,
            client_id=oauth.client_id,
            client_secret=oauth.client_secret,
            scopes=MAIL_SCOPE,
        )
        creds.refresh(Request())
        return creds

    def authorize(self) -> None:
        oauth = self.config.gmail.oauth
        client_config = {
            "installed": {
                "client_id": oauth.client_id,
                "client_secret": oauth.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": oauth.token_uri,
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, MAIL_SCOPE)
        creds = flow.run_local_server(port=0)
        data = self.config.config_path.read_text()
        config_dict = tomllib.loads(data)
        config_dict["mail"]["gmail"]["oauth"]["refresh_token"] = creds.refresh_token
        self.config.config_path.write_text(tomli_w.dumps(config_dict))

    def imap_login(self, imap: imaplib.IMAP4_SSL) -> None:
        if self.config.gmail.auth_mode == "oauth":
            creds = self.oauth_credentials()
            imap.authenticate("XOAUTH2", lambda _: _xoauth2(self.config.gmail.address, creds.token))
            return
        imap.login(self.config.gmail.address, self.config.gmail.app_password.password)

    def smtp_login(self, smtp: smtplib.SMTP) -> None:
        if self.config.gmail.auth_mode == "oauth":
            creds = self.oauth_credentials()
            smtp.docmd("AUTH", "XOAUTH2 " + _xoauth2_b64(self.config.gmail.address, creds.token))
            return
        smtp.login(self.config.gmail.address, self.config.gmail.app_password.password)


@dataclass(slots=True)
class IncomingMail:
    uid: str
    gmail_message_id: str
    gmail_thread_id: str
    rfc_message_id: str | None
    subject: str
    from_address: str
    body_text: str
    attachments: list[tuple[str, bytes, str]]
    references: list[str]


def normalize_message_ids(values: Iterable[str | None]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        matches = _MESSAGE_ID_PATTERN.findall(value)
        items = matches or [value.strip()]
        for item in items:
            ref = item.strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            refs.append(ref)
    return refs


def _parse_gmail_fetch_metadata(meta: bytes) -> tuple[str, str]:
    text = meta.decode("utf-8", errors="ignore")
    msgid_match = re.search(r"X-GM-MSGID (\d+)", text)
    thrid_match = re.search(r"X-GM-THRID (\d+)", text)
    if not msgid_match or not thrid_match:
        raise RuntimeError(f"Could not parse Gmail metadata: {text}")
    return msgid_match.group(1), thrid_match.group(1)


def _extract_body(msg: email.message.EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace").strip()
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                parser = _HTMLStripper()
                parser.feed(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                return parser.text().strip()
        return ""
    if msg.get_content_type() == "text/html":
        parser = _HTMLStripper()
        parser.feed(msg.get_content())
        return parser.text().strip()
    return msg.get_content().strip()


def _extract_attachments(msg: email.message.EmailMessage) -> list[tuple[str, bytes, str]]:
    attachments: list[tuple[str, bytes, str]] = []
    for part in msg.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        attachments.append((filename or "attachment.bin", payload, content_type))
    return attachments


def _attachment_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed:
        return guessed
    return ".bin"


def _attachment_basename(index: int) -> str:
    return "attachment" if index == 1 else f"attachment{index}"


class GmailClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.auth = GmailAuth(config)
        self._gmail_api_send_enabled = True

    def _open_imap(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(
            self.config.gmail.imap_host,
            self.config.gmail.imap_port,
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        self.auth.imap_login(imap)
        imap.select("INBOX")
        return imap

    def fetch_incoming(self) -> list[IncomingMail]:
        imap = self._open_imap()
        try:
            status, data = imap.uid("SEARCH", None, "UNSEEN", "FROM", self.config.gmail.allowed_from)
            if status != "OK":
                return []
            uids = [uid.decode("ascii") for uid in data[0].split() if uid]
            messages: list[IncomingMail] = []
            for uid in uids:
                status, fetch_data = imap.uid("FETCH", uid, "(X-GM-MSGID X-GM-THRID BODY.PEEK[])")
                if status != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
                    continue
                meta, raw_message = fetch_data[0]
                gmail_message_id, gmail_thread_id = _parse_gmail_fetch_metadata(meta)
                msg = email.message_from_bytes(raw_message, policy=policy.default)
                body = _extract_body(msg)
                refs = normalize_message_ids([msg.get("References", ""), msg.get("In-Reply-To", "") or ""])
                messages.append(
                    IncomingMail(
                        uid=uid,
                        gmail_message_id=gmail_message_id,
                        gmail_thread_id=gmail_thread_id,
                        rfc_message_id=msg.get("Message-ID"),
                        subject=str(msg.get("Subject", "")).strip(),
                        from_address=parseaddr(str(msg.get("From", "")))[1].lower(),
                        body_text=body,
                        attachments=_extract_attachments(msg),
                        references=refs,
                    )
                )
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                LOG.exception("Failed to close IMAP cleanly")

    def mark_seen(self, uid: str) -> None:
        imap = self._open_imap()
        try:
            imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        finally:
            imap.logout()

    def _send_via_gmail_api(self, msg: EmailMessage, gmail_thread_id: str | None) -> str:
        creds = self.auth.oauth_credentials()
        session = AuthorizedSession(creds)
        payload: dict[str, str] = {
            "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii"),
        }
        if gmail_thread_id:
            payload["threadId"] = gmail_thread_id
        response = session.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        gmail_message_id = str(data["id"])
        params = urlencode([("format", "metadata"), ("metadataHeaders", "Message-ID")])
        metadata = session.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{gmail_message_id}?{params}",
            timeout=30,
        )
        metadata.raise_for_status()
        headers = metadata.json().get("payload", {}).get("headers", [])
        actual_message_id = next(
            (
                str(header.get("value", "")).strip()
                for header in headers
                if str(header.get("name", "")).lower() == "message-id" and str(header.get("value", "")).strip()
            ),
            "",
        )
        return actual_message_id or str(msg["Message-ID"])

    def send_message(
        self,
        *,
        to_address: str,
        subject: str,
        markdown_body: str,
        in_reply_to: str | None,
        references: Iterable[str],
        from_address: str,
        sender_address: str | None,
        reply_to: str | None,
        gmail_thread_id: str | None = None,
    ) -> str:
        msg = EmailMessage()
        msg["To"] = to_address
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="codex-mailbridge.local")
        msg["From"] = from_address
        if sender_address:
            msg["Sender"] = sender_address
        if reply_to:
            msg["Reply-To"] = reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        refs = normalize_message_ids(references)
        if in_reply_to and in_reply_to not in refs:
            refs.append(in_reply_to)
        if refs:
            msg["References"] = " ".join(refs)

        html_body = markdown.markdown(markdown_body, extensions=["fenced_code", "tables", "sane_lists"])
        msg.set_content(markdown_body)
        msg.add_alternative(html_body, subtype="html")

        if self.config.gmail.auth_mode == "oauth" and getattr(self, "_gmail_api_send_enabled", True):
            try:
                return self._send_via_gmail_api(msg, gmail_thread_id)
            except Exception as exc:
                response = getattr(exc, "response", None)
                response_text = ""
                if response is not None:
                    try:
                        response_text = response.text
                    except Exception:
                        response_text = ""
                if getattr(response, "status_code", None) == 403 and "SERVICE_DISABLED" in response_text:
                    self._gmail_api_send_enabled = False
                    LOG.warning("Gmail API send is disabled for this OAuth project; using SMTP fallback")
                else:
                    LOG.exception("Gmail API send failed; falling back to SMTP")

        smtp = smtplib.SMTP(self.config.gmail.smtp_host, self.config.gmail.smtp_port, timeout=30)
        try:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            self.auth.smtp_login(smtp)
            smtp.send_message(msg)
        finally:
            smtp.quit()
        return str(msg["Message-ID"])

    def send_assistant_reply(
        self,
        *,
        subject: str,
        markdown_body: str,
        parent_message_id: str | None,
        references: Iterable[str],
        gmail_thread_id: str | None = None,
    ) -> str:
        return self.send_message(
            to_address=self.config.gmail.allowed_from,
            subject=subject,
            markdown_body=markdown_body,
            in_reply_to=parent_message_id,
            references=references,
            from_address=self.config.gmail.address,
            sender_address=None,
            reply_to=self.config.gmail.address,
            gmail_thread_id=gmail_thread_id,
        )

    def send_cli_user_mirror(
        self,
        *,
        subject: str,
        markdown_body: str,
        parent_message_id: str | None,
        references: Iterable[str],
        gmail_thread_id: str | None = None,
    ) -> str:
        return self.send_message(
            to_address=self.config.gmail.allowed_from,
            subject=subject,
            markdown_body=markdown_body,
            in_reply_to=parent_message_id,
            references=references,
            from_address=self.config.gmail.user_visible_from,
            sender_address=self.config.gmail.address,
            reply_to=self.config.gmail.address,
            gmail_thread_id=gmail_thread_id,
        )


def save_attachments(target_dir: Path, attachments: list[tuple[str, bytes, str]]) -> tuple[list[str], list[str]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    image_paths: list[str] = []
    for index, (filename, payload, content_type) in enumerate(attachments, start=1):
        extension = _attachment_extension(filename, content_type)
        counter = index
        candidate = target_dir / f"{_attachment_basename(counter)}{extension}"
        while candidate.exists():
            counter += 1
            candidate = target_dir / f"{_attachment_basename(counter)}{extension}"
        candidate.write_bytes(payload)
        saved_paths.append(str(candidate))
        mime, _ = mimetypes.guess_type(candidate.name)
        if (mime or content_type).startswith("image/"):
            image_paths.append(str(candidate))
    return saved_paths, image_paths
