"""Server-layer tests: account validation and dispatch with a mocked client.

No live network calls — the Gmail service is replaced with a fake that
mimics the chained-builder API (users().messages().list().execute()).
"""

from __future__ import annotations

import asyncio
import base64
from email import message_from_bytes

import pytest

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
        return FakeExec({
            "id": kw["id"], "threadId": "t1",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Full"}],
                "body": {"data": _b64url("full body")},
            },
        })

    def modify(self, **kw):
        self.r["modify"] = kw
        return FakeExec({"id": kw["id"]})

    def batchModify(self, **kw):  # noqa: N802 — mirrors the Gmail client method
        self.r["batchModify"] = kw
        return FakeExec({})


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
