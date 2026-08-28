# Gmail MCP — Development Guide

## What it is

A local stdio MCP server giving an AI agent unified access to **multiple
Gmail accounts at once**. The native Anthropic Gmail connector binds one
account per OAuth grant; this server holds refresh tokens for N accounts in
a local SQLite store and routes each tool call to the right inbox via an
`account` argument. `search_all_accounts` fans one query across every inbox.

## Layout

```
src/gmail_mcp/
  server.py   — MCP server entry point, tool registration, dispatch, per-account routing
  auth.py     — gmail-mcp-auth CLI: browser OAuth bootstrap (NOT an MCP tool)
  store.py    — SQLite token store (accounts table keyed by email)
  gmail.py    — per-account service build + token refresh; pure MIME parsing/formatting
  config.py   — path resolution (DB, client secret) + SCOPES constant
tests/        — pytest; Gmail client is mocked, no live network
```

## Module responsibilities

| Module | Purpose |
|--------|---------|
| `server.py` | Registers MCP tools, dispatches calls, resolves `account` → service. All Gmail network I/O runs in `asyncio.to_thread` (the google client is sync). |
| `auth.py` | `add` / `list` / `remove` subcommands. `add` runs `InstalledAppFlow.run_local_server`, reads the granted email from `users.getProfile`, upserts into the store. |
| `store.py` | `TokenStore` CRUD over `sqlite3`. `upsert` preserves `added_at`; `touch` stamps `last_used_at`; `update_token` persists refreshed access tokens. |
| `gmail.py` | `build_service(account, store)` builds google-auth `Credentials`, refreshes if stale, persists the new token, returns a Gmail client. Plus pure helpers — `parse_message`, `extract_body_and_attachments`, `strip_html`, `resolve_label_ids`, `build_mime_message`, `sanitize_filename`, `screen_attachment`, `decode_b64url_bytes`, formatters. |
| `config.py` | `db_path()`, `client_secret_path()`, `attachments_dir()`, `max_attachment_bytes()`, `SCOPES`. |

## OAuth model

- **Auth is a CLI, not a tool** — the OAuth flow needs a browser, which an
  MCP tool can't drive. `gmail-mcp-auth add` runs the installed-app loopback
  flow and stores the refresh token.
- **Client config** comes from a downloaded Google "Desktop app" OAuth client
  JSON at `~/.gmail-mcp/client_secret.json` (env `GMAIL_MCP_CLIENT_SECRET`).
  Never hardcode `client_id`/`client_secret`.
- **Token store**: SQLite at `~/.gmail-mcp/tokens.db` (env `GMAIL_MCP_DB`).
  Keyed by email. Holds refresh_token + last access-token blob + scopes.
- **Refresh**: google-auth's `Request` transport refreshes the access token
  on demand; `build_service` persists the refreshed blob back to the DB.
- **Scopes** (one constant, `config.SCOPES`): `gmail.readonly`,
  `gmail.compose`, `gmail.modify`, `gmail.settings.basic`. Granular — NOT full
  `mail.google.com`, and **no `gmail.send`** / **no `gmail.settings.sharing`**
  (see Security model). Widening `SCOPES` does NOT retro-grant existing
  accounts — each must re-run `gmail-mcp-auth add` to re-consent, or the new
  tool returns `403 insufficient scope`.

## Security model

- **No autonomous send.** There is no `send_message` tool and the `gmail.send`
  scope is not requested. The server can only `create_draft` — a draft sits in
  Gmail's drafts folder until the user sends it by hand, so an injected instruction
  can't exfiltrate mail. If send is ever wanted, it's a separate explicit
  decision; do not re-add it casually.
- **Email content is untrusted.** Sender, subject, snippet, body, and
  attachment filenames are attacker-controlled. Every tool returning email
  content wraps that region via `gmail.wrap_untrusted()` (delimiters:
  `⟦UNTRUSTED EMAIL CONTENT …⟧` / `⟦END UNTRUSTED EMAIL CONTENT⟧`). Ids
  (message/thread/label) stay OUTSIDE the wrapper so follow-up calls stay
  clean. Read-tool descriptions also carry `_UNTRUSTED_NOTICE` so the model is
  warned in-band. When adding a tool that surfaces message content, route it
  through the shared formatters / `wrap_untrusted`.
- **No off-account forwarding.** The filter tools (`create_filter` et al.) run
  on `gmail.settings.basic`, which manages filters but cannot set a forwarding
  address (that needs `gmail.settings.sharing`, deliberately not requested).
  `create_filter` exposes label/archive/trash/star actions only — never the
  filter `forward` action — so a filter can't exfiltrate mail. Keep it that way.
- **One fence per response, not per message.** Aggregators
  (`format_search_results`, `format_thread`) emit a single `wrap_untrusted`
  pair around the whole content blob and precede it with a TRUSTED `#N [id]
  (thread)` manifest; fenced bodies are keyed by the same `#N` ordinals. This
  cuts repeated-delimiter token overhead AND keeps real ids exclusively outside
  the fence (no genuine id inside for an attacker to mimic). Per-message
  `format_message_summary` / `format_parsed_message` still fence once each. The
  unwrapped inner content is built by `_summary_body` / `_parsed_body`; reuse
  those when adding an aggregator so the single-fence invariant holds.
- **Body length is capped.** `read_message` / `read_thread` truncate each body
  to `config.max_body_chars()` (env `GMAIL_MCP_MAX_BODY_CHARS`, default 500,
  `<=0` = unlimited) via `gmail.truncate_body`, which appends a recoverable
  `… [truncated N chars — re-fetch with max_body_chars=0 …]` marker. Both tools
  accept a per-call `max_body_chars` arg (`_resolve_body_cap` resolves arg →
  config → unlimited). Search results are unaffected — they use Gmail's short
  `snippet`, not the full body.
- **`strip_html` is the HTML→text fallback** (used only when a message has no
  `text/plain` part). It strips comments (incl. Outlook `<!--[if mso]>` blocks
  whose `>` chars defeat a bare tag regex), `<head>`, and `<script>/<style>`,
  then decodes entities via the stdlib `html.unescape` (numeric + all named).
  Entity decoding runs AFTER tag removal so a decoded `<` can't be re-parsed as
  a tag — keep that ordering if you touch it.
- **Attachment downloads write to exactly one root.** `download_attachments`
  is the only filesystem write in the server. It writes under
  `config.attachments_dir()/<message_id>/` (env `GMAIL_MCP_ATTACHMENT_DIR`,
  default `~/.gmail-mcp/attachments`) and takes **no destination argument** by
  design: one would be an arbitrary-file-write primitive reachable by an
  instruction embedded in an email. Do not add one. `sanitize_filename`
  reduces the attacker-chosen filename to an inert index-prefixed ASCII
  basename (separators dropped, leading dots stripped, bidi overrides removed,
  length capped) and `_write_attachment` re-checks the resolved path against
  the root, then writes `0o600` with `O_NOFOLLOW`. The message id is validated
  against `_MESSAGE_ID_RE` before it becomes a directory name.
- **Attachments are type-screened, not virus-scanned.** `screen_attachment`
  refuses Gmail's own blocked-file-type list, macro-enabled Office documents,
  executable MIME types, oversized payloads, and every attachment on a
  `SPAM`-labeled message. It checks *all* dot-suffixes, so `invoice.pdf.exe` is
  caught. Archives are allowed with a warning. Gmail does **not** expose its
  malware verdict through the API and `attachments.get` will serve bytes the
  web UI blocks, so never call this a scan in code comments, tool descriptions,
  or docs.
- **Attachments are addressed by `#N`, not by attachment id.** `_parsed_body`
  numbers them and deliberately omits the raw `attachmentId`; the download tool
  takes the same ordinal. Both sides get their ordering from one payload walk
  (`extract_body_and_attachments`), so if you change that walk, both move
  together. Gmail inlines small attachments in `body.data` with no
  `attachmentId`, so `Attachment.data` carries that case, and
  `decode_b64url_bytes` (never the text decoder, which mangles binary) turns
  either form into bytes.
- **No audit log** — intentionally not implemented.

## Gotchas

- **Refresh token only issues on first consent.** `add` passes
  `prompt="consent"` to force it. If a user already granted the app, Google
  may omit the refresh token; revoke at myaccount.google.com/permissions and
  re-run. `auth._add` errors clearly when no refresh token comes back.
- **Test-user / unverified-app expiry.** While the OAuth app is in "Testing",
  every account must be a listed test user, and refresh tokens expire after
  7 days. Publish the app for non-expiring tokens. See README.
- **Dead grant → `GmailAuthError`.** `build_service` raises it on refresh
  failure with a "re-run gmail-mcp-auth add" message; the server surfaces it.
- **`web` vs `installed` JSON section.** `_read_client_config` accepts either.
- **Sync client under async server.** Gmail API client is blocking; `call_tool`
  wraps `_dispatch` in `asyncio.to_thread`. Keep new tool logic in `_dispatch`
  (sync) so this holds.
- **Label resolution does not create labels.** `resolve_label_ids` matches
  existing ids/names (case-insensitive) and raises listing available names.
- **The protocol layer is two adapter functions, and that is on purpose.**
  `list_tools()` and `call_tool()` are plain async functions returning
  `list[Tool]` and `list[TextContent]`. `_on_list_tools` / `_on_call_tool`
  wrap them in the `ListToolsResult` / `CallToolResult` that mcp 2.x wants,
  and `build_app()` passes those to `Server(...)`. Keep new tool work behind
  that seam — it is what kept the 2.x port to one file and 16 untouched
  schemas.
- **Tool schemas are hand-written, not derived.** `Tool(input_schema={...})`.
  Do not switch to `MCPServer` / `@app.tool()`: those build the schema from
  the function signature, which would regenerate all 16 and lose the wording.
  The field is `input_schema` in 2.x; the `inputSchema` alias still constructs
  at runtime but mypy rejects it, so use the real name.
- **Tool errors return, they do not raise.** `call_tool` catches ValueError,
  GmailAuthError and HttpError and returns readable text with
  `is_error=False`. That predates the 2.x port and was preserved through it.
  Changing it is a decision, not a refactor.
- **Header values are flattened, not validated.** Python's compat32 policy
  *raises* `HeaderParseError` on an embedded header rather than injecting one,
  and that raise misses the `ValueError` branch in `call_tool` and costs the
  caller the whole draft. `_header_safe` collapses CR/LF so a hostile or merely
  malformed reply Subject still drafts. Route every new header through it.

## Releasing

Tag-driven, no manual upload. `publish.yml` fires on `v*.*.*` and pushes to
PyPI via Trusted Publishing (OIDC, no stored token).

```bash
# bump ALL FOUR, they drift independently:
#   pyproject.toml, src/gmail_mcp/__init__.py, server.json (x2 version fields)
git tag -a v0.5.0 -m "..." && git push origin v0.5.0
```

`server.json` is the MCP registry entry and has **two** version fields. It sat
at 0.3.0 through the 0.4.0 release because only `pyproject.toml` got bumped.

A merge is not a release. The mcp 2.0 breakage shipped to every fresh
`uvx multi-account-gmail-mcp` for weeks; the fix only reached anyone once
v0.4.1 existed on PyPI. Verify a release by installing it clean, not by
trusting the green workflow:

```bash
uv venv /tmp/v && uv pip install --python /tmp/v/bin/python --refresh multi-account-gmail-mcp
/tmp/v/bin/python -c "import gmail_mcp.server"
```

## Outside contributors

This repo is public and takes PRs from strangers. Two things about that:

- **Fork PRs need a workflow approval, and the UI hides this.** The default
  `first_time_contributors` policy parks the run at `action_required`, and
  `gh pr checks` reports *"no checks reported on the branch"* — not "pending",
  not "blocked". PR #2 read as untested for a month because of it. Check
  `gh run list --status action_required` whenever a PR shows no checks, then
  `gh api -X POST repos/{owner}/{repo}/actions/runs/{id}/approve`. Read the
  diff for `.github/` changes before approving.
- **Merging two PRs that touch the same test fakes can conflict silently.**
  #2 and #4 each added a `FakeDrafts` class and a `FakeUsers.drafts()` to
  `tests/test_server.py`. Git merged them as duplicates with no conflict
  marker, Python kept the later definition, and an assertion was quietly
  testing the wrong fake. After resolving any test-file conflict:
  `grep -c "class Fake" tests/test_server.py`.

## Running tests

```bash
pip install -e ".[dev]"
pytest                  # no network (Gmail client mocked; stdio tests use a scratch DB)
ruff check src/ tests/  # lint
mypy src/               # type check — 28 pre-existing errors, mostly bare `dict`
```

`tests/test_stdio.py` is the one that matters after any change to the protocol
layer. Everything else calls `_dispatch` or the handlers directly, so the suite
stays green even if the server answers nothing over the wire. Those three tests
spawn the real process and speak MCP to it.
