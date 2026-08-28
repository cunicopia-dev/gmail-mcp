"""Server-layer tests: account validation and dispatch with a mocked client.

No live network calls — the Gmail service is replaced with a fake that
mimics the chained-builder API (users().messages().list().execute()).
"""

from __future__ import annotations

import asyncio
import base64
from email import message_from_bytes

import pytest
from googleapiclient.errors import HttpError

import gmail_mcp.server as server
from gmail_mcp.store import TokenStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = TokenStore(path=tmp_path / "tokens.db")
    monkeypatch.setattr(server, "_store", st)
    return st


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


# --- account validation -----------------------------------------------------

def test_require_account_unknown_lists_available(store):
    store.upsert("known@example.com", "r")
    with pytest.raises(ValueError) as exc:
        server._require_account("ghost@example.com")
    msg = str(exc.value)
    assert "ghost@example.com" in msg
    assert "known@example.com" in msg
    assert "gmail-mcp-auth add" in msg


def test_require_account_none_authorized(store):
    with pytest.raises(ValueError, match="none authorized yet"):
        server._require_account("x@example.com")


def test_require_account_ok(store):
    store.upsert("known@example.com", "r")
    acct = server._require_account("known@example.com")
    assert acct.email == "known@example.com"


# --- list_accounts dispatch -------------------------------------------------

def test_list_accounts_empty(store):
    out = server._dispatch("list_accounts", {})
    assert "No accounts authorized" in out


def test_list_accounts_nonempty(store):
    store.upsert("a@example.com", "r")
    out = server._dispatch("list_accounts", {})
    assert "a@example.com" in out
    assert "1 authorized account" in out


# --- mocked Gmail client ----------------------------------------------------

class FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    # Overridable per-test so a case can supply a message carrying attachments.
    full_message: dict | None = None

    def __init__(self, recorder):
        self.r = recorder

    def list(self, **kw):
        self.r["list"] = kw
        return FakeExec({"messages": [{"id": "m1"}, {"id": "m2"}]})

    def get(self, **kw):
        self.r.setdefault("get", []).append(kw)
        if kw.get("format") == "metadata":
            return FakeExec({
                "id": kw["id"], "threadId": "t1", "snippet": "snip",
                "payload": {"headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "To", "value": "a@example.com, third@x.com"},
                    {"name": "Message-ID", "value": "<orig@mail>"},
                    {"name": "References", "value": "<older@mail>"},
                    {"name": "Subject", "value": f"subj-{kw['id']}"},
                ]},
            })
        if FakeMessages.full_message is not None:
            return FakeExec(FakeMessages.full_message)
        return FakeExec({
            "id": kw["id"], "threadId": "t1",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Full"}],
                "body": {"data": _b64url("full body")},
            },
        })

    def attachments(self):
        return FakeAttachments(self.r)

    def modify(self, **kw):
        self.r["modify"] = kw
        return FakeExec({"id": kw["id"]})

    def batchModify(self, **kw):  # noqa: N802 — mirrors the Gmail client method
        self.r["batchModify"] = kw
        return FakeExec({})


class FakeAttachments:
    """users().messages().attachments().get(): serves bytes by attachment id."""

    # attachment id -> base64url payload. Overridable per-test.
    payloads: dict[str, str] = {}
    # attachment ids that should raise, mimicking a Gmail-side refusal.
    errors: set[str] = set()

    def __init__(self, recorder):
        self.r = recorder

    def get(self, **kw):
        self.r.setdefault("attachment_get", []).append(kw)
        att_id = kw["id"]
        if att_id in FakeAttachments.errors:
            raise HttpError(_FakeResp(404), b"not found")
        data = FakeAttachments.payloads[att_id]
        return FakeExec({"data": data, "size": len(data)})


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "Not Found"


class FakeThreads:
    def __init__(self, recorder):
        self.r = recorder

    def get(self, **kw):
        self.r["thread_get"] = kw
        return FakeExec({"messages": [
            {
                "id": "m1", "threadId": "t1",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "First"}],
                    "body": {"data": _b64url("x" * 200)},
                },
            },
            {
                "id": "m2", "threadId": "t1",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "Second"}],
                    "body": {"data": _b64url("reply body")},
                },
            },
        ]})


class FakeLabels:
    def list(self, **kw):
        return FakeExec({"labels": [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "TRASH", "name": "TRASH"},
            {"id": "UNREAD", "name": "UNREAD"},
            {"id": "STARRED", "name": "STARRED"},
            {"id": "Label_5", "name": "Receipts"},
        ]})


class FakeFilters:
    def __init__(self, recorder):
        self.r = recorder

    def list(self, **kw):
        return FakeExec({"filter": [
            {
                "id": "filt_1",
                "criteria": {"from": "noise@x.com"},
                "action": {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
            },
        ]})

    def create(self, **kw):
        self.r["filter_create"] = kw
        return FakeExec({"id": "filt_new", **kw.get("body", {})})

    def delete(self, **kw):
        self.r["filter_delete"] = kw
        return FakeExec({})


class FakeSettings:
    def __init__(self, recorder):
        self.r = recorder

    def filters(self):
        return FakeFilters(self.r)


class FakeDrafts:
    def __init__(self, recorder):
        self.r = recorder

    def create(self, **kw):
        self.r["draft_create"] = kw
        return FakeExec({"id": "d1"})

    def list(self, **kw):
        return FakeExec({"drafts": [{"id": "d1", "message": {"id": "m1"}}]})


class FakeUsers:
    def __init__(self, recorder):
        self.r = recorder

    def messages(self):
        return FakeMessages(self.r)

    def threads(self):
        return FakeThreads(self.r)

    def labels(self):
        return FakeLabels()

    def drafts(self):
        return FakeDrafts(self.r)

    def settings(self):
        return FakeSettings(self.r)


class FakeService:
    def __init__(self):
        self.recorder = {}

    def users(self):
        return FakeUsers(self.recorder)


@pytest.fixture
def fake_service(store, monkeypatch):
    store.upsert("a@example.com", "refresh-token")
    svc = FakeService()
    # Bypass real credential building / network.
    monkeypatch.setattr(server, "_service_for", lambda email: svc)
    monkeypatch.setattr(server, "build_service", lambda acct, st: svc)
    return svc


def test_search_messages_dispatch(fake_service):
    out = server._dispatch("search_messages", {"account": "a@example.com", "query": "is:unread"})
    assert "2 message(s) in a@example.com" in out
    assert "subj-m1" in out
    assert fake_service.recorder["list"]["q"] == "is:unread"


def test_read_message_dispatch(fake_service):
    out = server._dispatch("read_message", {"account": "a@example.com", "message_id": "m1"})
    assert "full body" in out
    assert "Full" in out
    # Email content is wrapped in untrusted-data delimiters; the id is not.
    assert "UNTRUSTED EMAIL CONTENT" in out
    assert "Message m1" in out.split("UNTRUSTED EMAIL CONTENT")[0]


def test_read_message_respects_max_body_chars(fake_service):
    # "full body" is 9 chars; cap at 4 must truncate with a recoverable marker.
    out = server._dispatch(
        "read_message",
        {"account": "a@example.com", "message_id": "m1", "max_body_chars": 4},
    )
    assert "truncated" in out
    assert "max_body_chars=0" in out


def test_read_message_max_body_zero_is_full(fake_service):
    out = server._dispatch(
        "read_message",
        {"account": "a@example.com", "message_id": "m1", "max_body_chars": 0},
    )
    assert "full body" in out
    assert "truncated" not in out


def test_read_thread_single_wrapper(fake_service):
    out = server._dispatch(
        "read_thread",
        {"account": "a@example.com", "thread_id": "t1", "max_body_chars": 0},
    )
    # One thread, two messages, exactly one untrusted fence.
    assert out.count("⟦UNTRUSTED") == 1
    assert "Thread t1" in out.split("UNTRUSTED EMAIL CONTENT")[0]
    assert "[m1]" in out and "[m2]" in out
    assert "reply body" in out


def test_read_thread_default_cap_truncates(fake_service, monkeypatch):
    # With a small server default and no per-call override, the 200-char body
    # in the fake thread is truncated.
    monkeypatch.setattr(server.config, "max_body_chars", lambda: 50)
    out = server._dispatch(
        "read_thread", {"account": "a@example.com", "thread_id": "t1"}
    )
    assert "truncated" in out


def test_send_is_gone():
    # send_message must be fully removed — no tool, no dispatch case.
    assert "Unknown tool" in server._dispatch("send_message", {})
    assert not hasattr(server, "_do_send")


def test_modify_labels_resolves_names(fake_service):
    out = server._dispatch("modify_labels", {
        "account": "a@example.com", "message_id": "m1", "add": ["Receipts"],
    })
    assert "Label_5" in out
    assert "1 message(s)" in out
    body = fake_service.recorder["batchModify"]["body"]
    assert body["ids"] == ["m1"]
    assert body["addLabelIds"] == ["Label_5"]


def test_modify_labels_by_query(fake_service):
    # The fake list() returns m1, m2 for any query.
    server._dispatch("modify_labels", {
        "account": "a@example.com", "query": "older_than:1y", "remove": ["INBOX"],
    })
    body = fake_service.recorder["batchModify"]["body"]
    assert body["ids"] == ["m1", "m2"]
    assert body["removeLabelIds"] == ["INBOX"]


def test_trash_by_ids(fake_service):
    out = server._dispatch("trash", {
        "account": "a@example.com", "message_ids": ["m1", "m2"],
    })
    assert "2 message(s)" in out
    body = fake_service.recorder["batchModify"]["body"]
    assert body["ids"] == ["m1", "m2"]
    assert body["addLabelIds"] == ["TRASH"]


def test_trash_by_query(fake_service):
    out = server._dispatch("trash", {
        "account": "a@example.com", "query": "from:spam@x.com",
    })
    assert "2 message(s)" in out
    assert fake_service.recorder["batchModify"]["body"]["addLabelIds"] == ["TRASH"]


def test_trash_requires_selection(fake_service):
    with pytest.raises(ValueError, match="No selection"):
        server._dispatch("trash", {"account": "a@example.com"})


def test_modify_labels_unknown_name(fake_service):
    with pytest.raises(ValueError, match="Unknown label"):
        server._dispatch("modify_labels", {
            "account": "a@example.com", "message_id": "m1", "add": ["DoesNotExist"],
        })


def test_modify_labels_noop(fake_service):
    out = server._dispatch("modify_labels", {"account": "a@example.com", "message_id": "m1"})
    assert "Nothing to do" in out


def test_list_labels_dispatch(fake_service):
    out = server._dispatch("list_labels", {"account": "a@example.com"})
    assert "Receipts" in out
    assert "Label_5" in out


def test_search_all_accounts(store, monkeypatch):
    store.upsert("a@example.com", "r")
    store.upsert("b@example.com", "r")
    svc = FakeService()
    monkeypatch.setattr(server, "build_service", lambda acct, st: svc)
    out = server._dispatch("search_all_accounts", {"query": "test"})
    assert "a@example.com" in out
    assert "b@example.com" in out
    assert "2 account(s)" in out


def test_search_all_accounts_none(store):
    out = server._dispatch("search_all_accounts", {"query": "test"})
    assert "No accounts authorized" in out


# --- filters ----------------------------------------------------------------

def test_list_filters_dispatch(fake_service):
    out = server._dispatch("list_filters", {"account": "a@example.com"})
    assert "filt_1" in out
    assert "from=noise@x.com" in out
    # Label ids are rendered as names.
    assert "TRASH" in out and "INBOX" in out


def test_create_filter_convenience_flags(fake_service):
    out = server._dispatch("create_filter", {
        "account": "a@example.com",
        "from_address": "spam@x.com OR promo@y.com",
        "delete": True,
    })
    assert "filt_new" in out
    body = fake_service.recorder["filter_create"]["body"]
    assert body["criteria"] == {"from": "spam@x.com OR promo@y.com"}
    assert body["action"]["addLabelIds"] == ["TRASH"]


def test_create_filter_archive_and_label(fake_service):
    server._dispatch("create_filter", {
        "account": "a@example.com",
        "subject": "receipt",
        "archive": True,
        "mark_read": True,
        "add_labels": ["Receipts"],
    })
    body = fake_service.recorder["filter_create"]["body"]
    assert body["criteria"] == {"subject": "receipt"}
    assert body["action"]["addLabelIds"] == ["Label_5"]
    assert set(body["action"]["removeLabelIds"]) == {"INBOX", "UNREAD"}


def test_create_filter_requires_criteria(fake_service):
    out = server._dispatch("create_filter", {"account": "a@example.com", "delete": True})
    assert "no criteria" in out
    assert "filter_create" not in fake_service.recorder


def test_create_filter_requires_action(fake_service):
    out = server._dispatch("create_filter", {
        "account": "a@example.com", "from_address": "x@y.com",
    })
    assert "no action" in out
    assert "filter_create" not in fake_service.recorder


def test_create_filter_unknown_label(fake_service):
    with pytest.raises(ValueError, match="Unknown label"):
        server._dispatch("create_filter", {
            "account": "a@example.com", "subject": "x", "add_labels": ["Nope"],
        })


def test_delete_filter_dispatch(fake_service):
    out = server._dispatch("delete_filter", {
        "account": "a@example.com", "filter_id": "filt_1",
    })
    assert "filt_1" in out
    assert fake_service.recorder["filter_delete"]["id"] == "filt_1"


def test_unknown_tool():
    assert "Unknown tool" in server._dispatch("bogus", {})


# --- create_draft dispatch --------------------------------------------------

def _drafted_message(fake_service):
    body = fake_service.recorder["draft_create"]["body"]["message"]
    return body, message_from_bytes(base64.urlsafe_b64decode(body["raw"]))


def test_create_draft_standalone(fake_service):
    out = server._dispatch("create_draft", {
        "account": "a@example.com", "to": "b@c.com",
        "subject": "Hi", "body": "text",
    })
    assert out == "Created draft d1."
    message, mime = _drafted_message(fake_service)
    assert "threadId" not in message
    assert mime["Subject"] == "Hi"
    assert mime["In-Reply-To"] is None


def test_create_draft_reply_joins_thread(fake_service):
    out = server._dispatch("create_draft", {
        "account": "a@example.com", "reply_to_message_id": "m1", "body": "text",
    })
    assert "in thread t1" in out
    message, mime = _drafted_message(fake_service)
    assert message["threadId"] == "t1"
    assert mime["To"] == "a@b.com"
    assert mime["Subject"] == "Re: subj-m1"
    assert mime["In-Reply-To"] == "<orig@mail>"
    assert mime["References"] == "<older@mail> <orig@mail>"
    # The answered message is fetched by metadata only — its body is never read.
    assert fake_service.recorder["get"][-1]["format"] == "metadata"


def test_create_draft_reply_all_ccs_others_but_not_self(fake_service):
    server._dispatch("create_draft", {
        "account": "a@example.com", "reply_to_message_id": "m1",
        "body": "text", "reply_all": True,
    })
    _, mime = _drafted_message(fake_service)
    assert "third@x.com" in mime["Cc"]
    assert "a@example.com" not in mime["Cc"]


def test_create_draft_reply_honours_explicit_overrides(fake_service):
    server._dispatch("create_draft", {
        "account": "a@example.com", "reply_to_message_id": "m1",
        "to": "someone@else.com", "subject": "Own subject", "body": "text",
    })
    message, mime = _drafted_message(fake_service)
    assert message["threadId"] == "t1"
    assert mime["To"] == "someone@else.com"
    assert mime["Subject"] == "Own subject"


def test_create_draft_without_recipient_is_refused(fake_service):
    with pytest.raises(ValueError, match="needs 'to' and 'subject'"):
        server._dispatch("create_draft", {"account": "a@example.com", "body": "text"})
    assert "draft_create" not in fake_service.recorder


# --- create_draft: send-as alias --------------------------------------------

BASE_DRAFT = {
    "account": "a@example.com",
    "to": "customer@example.com",
    "subject": "Re: Help",
    "body": "Answer.",
}


def _draft_mime(fake_service) -> str:
    raw = fake_service.recorder["draft_create"]["body"]["message"]["raw"]
    return base64.urlsafe_b64decode(raw).decode("utf-8")


def test_create_draft_defaults_sender_to_account(fake_service):
    out = server._dispatch("create_draft", dict(BASE_DRAFT))
    assert "d1" in out
    assert "From: a@example.com" in _draft_mime(fake_service)


def test_create_draft_from_addr_overrides_sender(fake_service):
    server._dispatch("create_draft", {**BASE_DRAFT, "from_addr": "support@example.org"})
    mime = _draft_mime(fake_service)
    assert "From: support@example.org" in mime
    assert "From: a@example.com" not in mime


def test_create_draft_schema_exposes_from_addr():
    tools = asyncio.run(server.list_tools())
    schema = next(t.input_schema for t in tools if t.name == "create_draft")
    assert "from_addr" in schema["properties"]
    # Optional either way; #2 made to/subject conditionally required instead.
    assert schema["required"] == ["account", "body"]
# --- download_attachments ---------------------------------------------------

def _b64url_bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


PDF_BYTES = b"%PDF-1.4\n\x00\x01\x02\xff\xfe not really a pdf"


def _message_with(parts, label_ids=None):
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": label_ids if label_ids is not None else ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "See attached"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("hi")}},
                *parts,
            ],
        },
    }


def _part(filename, mime, attachment_id=None, size=100, data=None):
    body = {"size": size}
    if attachment_id:
        body["attachmentId"] = attachment_id
    if data is not None:
        body["data"] = data
    return {"mimeType": mime, "filename": filename, "body": body}


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Point the attachment root at a temp dir and reset the fake payloads."""
    root = tmp_path / "attachments"
    monkeypatch.setattr(server.config, "attachments_dir", lambda: root)
    monkeypatch.setattr(FakeMessages, "full_message", None)
    monkeypatch.setattr(FakeAttachments, "payloads", {})
    monkeypatch.setattr(FakeAttachments, "errors", set())
    return root


def test_download_saves_external_attachment(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("invoice.pdf", "application/pdf", "att-1", len(PDF_BYTES))]
    ))
    monkeypatch.setattr(
        FakeAttachments, "payloads", {"att-1": _b64url_bytes(PDF_BYTES)}
    )
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    saved = downloads / "m1" / "01-invoice.pdf"
    assert saved.exists()
    assert saved.read_bytes() == PDF_BYTES  # binary survives, not utf-8 mangled
    assert str(saved) in out


def test_download_saves_inline_attachment(fake_service, downloads, monkeypatch):
    """Small attachments have no attachmentId; bytes are inline in the part."""
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("tiny.csv", "text/csv", data=_b64url_bytes(b"a,b,c"), size=5)]
    ))
    server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert (downloads / "m1" / "01-tiny.csv").read_bytes() == b"a,b,c"


def test_download_writes_owner_only_permissions(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("notes.txt", "text/plain", data=_b64url_bytes(b"x"), size=1)]
    ))
    server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    mode = (downloads / "m1" / "01-notes.txt").stat().st_mode & 0o777
    assert mode == 0o600


def test_download_refuses_blocked_type_and_writes_nothing(
    fake_service, downloads, monkeypatch
):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("setup.exe", "application/octet-stream", "att-1")]
    ))
    monkeypatch.setattr(
        FakeAttachments, "payloads", {"att-1": _b64url_bytes(b"MZ evil")}
    )
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert "Refused" in out
    assert ".exe" in out
    assert not (downloads / "m1").exists() or not any((downloads / "m1").iterdir())
    # Refused attachments are never even fetched.
    assert "attachment_get" not in fake_service.recorder


def test_download_refuses_everything_on_spam_message(
    fake_service, downloads, monkeypatch
):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("invoice.pdf", "application/pdf", "att-1")],
        label_ids=["SPAM"],
    ))
    monkeypatch.setattr(
        FakeAttachments, "payloads", {"att-1": _b64url_bytes(PDF_BYTES)}
    )
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert "spam" in out.lower()
    assert not (downloads / "m1" / "01-invoice.pdf").exists()


def test_download_refuses_oversized(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(server.config, "max_attachment_bytes", lambda: 10)
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("big.pdf", "application/pdf", "att-1", size=5000)]
    ))
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert "Refused" in out
    assert not (downloads / "m1" / "01-big.pdf").exists()


def test_download_index_selects_one(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with([
        _part("one.pdf", "application/pdf", "att-1"),
        _part("two.pdf", "application/pdf", "att-2"),
    ]))
    monkeypatch.setattr(FakeAttachments, "payloads", {
        "att-1": _b64url_bytes(b"one"), "att-2": _b64url_bytes(b"two"),
    })
    server._dispatch("download_attachments", {
        "account": "a@example.com", "message_id": "m1", "index": 2,
    })
    assert not (downloads / "m1" / "01-one.pdf").exists()
    assert (downloads / "m1" / "02-two.pdf").read_bytes() == b"two"


def test_download_rejects_out_of_range_index(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("one.pdf", "application/pdf", "att-1")]
    ))
    with pytest.raises(ValueError, match="index"):
        server._dispatch("download_attachments", {
            "account": "a@example.com", "message_id": "m1", "index": 5,
        })


def test_download_no_attachments_is_clear(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with([]))
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert "no attachments" in out.lower()


def test_download_sanitizes_traversal_filename(fake_service, downloads, monkeypatch):
    """A filename is attacker-chosen; it must not escape the download root."""
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("../../../../../../tmp/pwned.txt", "text/plain", "att-1")]
    ))
    monkeypatch.setattr(
        FakeAttachments, "payloads", {"att-1": _b64url_bytes(b"nope")}
    )
    server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    written = list((downloads / "m1").iterdir())
    assert len(written) == 1
    assert written[0].name == "01-pwned.txt"
    assert written[0].resolve().is_relative_to(downloads.resolve())


def test_download_rejects_message_id_with_path_chars(fake_service, downloads):
    with pytest.raises(ValueError, match="message id"):
        server._dispatch("download_attachments", {
            "account": "a@example.com", "message_id": "../../etc",
        })


def test_download_reports_gmail_error_per_attachment(
    fake_service, downloads, monkeypatch
):
    """One attachment failing must not lose the others."""
    monkeypatch.setattr(FakeMessages, "full_message", _message_with([
        _part("good.pdf", "application/pdf", "att-1"),
        _part("gone.pdf", "application/pdf", "att-2"),
    ]))
    monkeypatch.setattr(FakeAttachments, "payloads", {"att-1": _b64url_bytes(b"ok")})
    monkeypatch.setattr(FakeAttachments, "errors", {"att-2"})
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert (downloads / "m1" / "01-good.pdf").read_bytes() == b"ok"
    assert "#2" in out
    assert not (downloads / "m1" / "02-gone.pdf").exists()


def test_download_warns_on_archive_but_saves(fake_service, downloads, monkeypatch):
    monkeypatch.setattr(FakeMessages, "full_message", _message_with(
        [_part("docs.zip", "application/zip", "att-1")]
    ))
    monkeypatch.setattr(FakeAttachments, "payloads", {"att-1": _b64url_bytes(b"PK")})
    out = server._dispatch(
        "download_attachments", {"account": "a@example.com", "message_id": "m1"}
    )
    assert (downloads / "m1" / "01-docs.zip").exists()
    assert "archive" in out.lower()


def test_download_tool_is_registered():
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "download_attachments" in names
