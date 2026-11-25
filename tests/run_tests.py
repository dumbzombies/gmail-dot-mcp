"""Offline + live checks for gmail-dot.

    python tests/run_tests.py          # unit tests, no network
    python tests/run_tests.py --live   # unit tests + a real Gmail round trip

Plain asserts, no pytest: the venv this server runs in has no test runner.
"""
from __future__ import annotations

import email
import json
import os
import pathlib
import secrets
import sys
import tempfile
import time
from email import policy
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import extract  # noqa: E402
import server as S  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILED.append(f"{name}{f': {detail}' if detail else ''}")
        print(f"FAIL {name} {detail}")


def header(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


# ------------------------------------------------------------------ dotted / aliases


def test_dotted_local() -> None:
    local = "haiwuejwkaoskjjejksjdkksk"
    seen = set()
    for _ in range(150):
        value = S._dotted_local(local, seen)
        check("dotted: non-empty", bool(value), repr(value))
        if not value:
            continue
        part = value.split("@")[0]
        check("dotted: keeps letters", part.replace(".", "") == local, part)
        check("dotted: has dots", "." in part, part)
        check("dotted: no edge dots", not part.startswith(".") and not part.endswith("."), part)
        check("dotted: no double dots", ".." not in part, part)
        check("dotted: dot count in range", 1 <= part.count(".") <= S.MAX_DOTS, str(part.count(".")))
        check("dotted: unique", value not in seen, value)
        seen.add(value)
    check("dotted: short local falls back", S._dotted_local("ab", set()) != "", "")
    check("dotted: single char rejected", S._dotted_local("a", set()) == "", "")


def test_plus_local() -> None:
    value = S._plus_local("someone", set())
    check("plus: shape", value.startswith("someone+mm") and value.endswith("@gmail.com"), value)


def test_rotation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = (S.STATE, S.ACCOUNTS, S._max_uid)
        try:
            S.STATE = pathlib.Path(tmp) / "state.json"
            S.ACCOUNTS = pathlib.Path(tmp) / "accounts.json"
            S.ACCOUNTS.write_text(
                json.dumps(
                    [
                        {"email": "alphaone@gmail.com", "app_password": "x" * 16},
                        {"email": "betatwo@gmail.com", "app_password": "y" * 16},
                        {"email": "gammathree@gmail.com", "app_password": "z" * 16},
                    ]
                ),
                encoding="utf-8",
            )
            S._max_uid = lambda account: 0  # type: ignore[assignment]
            got = [S._create_alias("rot", "dots")["account"] for _ in range(7)]
            check(
                "rotation: starts at first account and cycles",
                got[:4] == ["alphaone@gmail.com", "betatwo@gmail.com", "gammathree@gmail.com", "alphaone@gmail.com"],
                str(got),
            )
            state = json.loads(S.STATE.read_text(encoding="utf-8"))
            check("rotation: state persisted", len(state["aliases"]) == 7, str(len(state["aliases"])))
            check("rotation: all dotted", all("." in a.split("@")[0] for a in state["aliases"]), "")
        finally:
            S.STATE, S.ACCOUNTS, S._max_uid = saved  # type: ignore[assignment]


def test_state_trim_and_corruption() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = (S.STATE, S.MAX_ALIASES)
        try:
            S.STATE = pathlib.Path(tmp) / "state.json"
            S.MAX_ALIASES = 3
            S.STATE.write_text("not json", encoding="utf-8")
            state = S._load_state()
            check("state: corrupt file yields empty state", state["aliases"] == {}, str(state))
            backups = list(pathlib.Path(tmp).glob("state.json.corrupt-*"))
            check("state: corrupt file backed up", len(backups) == 1, str(backups))
            aliases = {f"a{i}@gmail.com": {"account": "x@gmail.com"} for i in range(5)}
            S._save_state({"aliases": aliases, "order": list(aliases), "last_account": 0})
            saved_state = json.loads(S.STATE.read_text(encoding="utf-8"))
            check("state: trimmed to MAX_ALIASES", len(saved_state["aliases"]) == 3, str(saved_state["order"]))
            check(
                "state: newest kept",
                saved_state["order"] == ["a2@gmail.com", "a3@gmail.com", "a4@gmail.com"],
                str(saved_state["order"]),
            )
            check("state: file mode 600", (S.STATE.stat().st_mode & 0o777) == 0o600, oct(S.STATE.stat().st_mode))
        finally:
            S.STATE, S.MAX_ALIASES = saved


# ------------------------------------------------------------------ recipients


def test_recipient_matching() -> None:
    alias = "j.ane.doe@gmail.com"
    ok = header(b"To: Jane <j.ane.doe@gmail.com>\r\nSubject: hi\r\n\r\nx")
    check("recipient: display name matches", S._addressed_exactly(ok, alias))
    check("recipient: case-insensitive", S._addressed_exactly(ok, alias.upper()))

    cc = header(b"To: other@gmail.com\r\nCc: j.ane.doe@gmail.com\r\n\r\nx")
    check("recipient: cc is not a recipient header", not S._addressed_exactly(cc, alias), "Cc must not count")

    delivered = header(b"To: undisclosed-recipients:;\r\nDelivered-To: j.ane.doe@gmail.com\r\n\r\nx")
    check("recipient: BCC via Delivered-To", S._addressed_exactly(delivered, alias))

    other_variant = header(b"To: ja.ne.doe@gmail.com\r\n\r\nx")
    check("recipient: different dot variant rejected", not S._addressed_exactly(other_variant, alias))

    bare = header(b"To: jane.doe@gmail.com, j.ane.doe@gmail.com\r\n\r\nx")
    check("recipient: multi-recipient", S._addressed_exactly(bare, alias))

    mentioned = header(
        b"From: j.ane.doe@gmail.com\r\nTo: someone@else.com\r\nSubject: j.ane.doe@gmail.com\r\n\r\nx"
    )
    check("recipient: from/subject mention rejected", not S._addressed_exactly(mentioned, alias))

    # BCC-style delivery: the To header names the account, only the envelope knows the alias.
    bcc_local = header(
        b"Received: from mx.example.com by smtp.gmail.com with ESMTPSA id abc\r\n"
        b" for <j.ane.doe@gmail.com> (version=TLS1_3 cipher=TLS_AES_256); Tue, 24 Nov 2026\r\n"
        b"To: janedoe@gmail.com\r\nSubject: code\r\n\r\nx"
    )
    check("recipient: BCC attributed via envelope For", S._addressed_exactly(bcc_local, alias))

    bcc_variant = header(
        b"Received: from mx.example.com by smtp.gmail.com for <ja.ne.doe@gmail.com>; Tue, 24 Nov 2026\r\n"
        b"To: janedoe@gmail.com\r\n\r\nx"
    )
    check("recipient: other dot variant via envelope rejected", not S._addressed_exactly(bcc_variant, alias))

    no_envelope = header(b"To: janedoe@gmail.com\r\nReceived: from a by b; Tue, 24 Nov 2026\r\n\r\nx")
    check("recipient: envelope-less message to base rejected", not S._addressed_exactly(no_envelope, alias))


def test_account_for() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = (S.ACCOUNTS,)
        try:
            S.ACCOUNTS = pathlib.Path(tmp) / "accounts.json"
            S.ACCOUNTS.write_text(
                json.dumps([{"email": "jane.doe@gmail.com", "app_password": "a" * 16}]),
                encoding="utf-8",
            )
            check("account: dotted alias", S._account_for("j.a.n.e.doe@gmail.com")["email"] == "jane.doe@gmail.com")
            check("account: plus alias", S._account_for("jane.doe+x@GMAIL.com")["email"] == "jane.doe@gmail.com")
            for probe, why in (
                ("jane.doe@yahoo.com", "foreign domain"),
                ("nobody@gmail.com", "unknown local"),
                ("garbage", "no @"),
            ):
                try:
                    S._account_for(probe)
                    check(f"account: rejects {why}", False, probe)
                except S.ToolError:
                    check(f"account: rejects {why}", True)
        finally:
            S.ACCOUNTS = saved[0]


# ------------------------------------------------------------------ imap plumbing


class FakeBox:
    """Stands in for imaplib.IMAP4_SSL; serves headers keyed by UID."""

    def __init__(self, messages: dict[int, bytes], search_hits: dict[str, list[int]], spam_row: bytes | None = b'(\\HasNoChildren \\Spam) "/" "[Gmail]/Spam"', select_status: str = "OK"):
        self.messages = messages
        self.search_hits = search_hits
        self.spam_row = spam_row
        self.select_status = select_status
        self.searches: list[str] = []

    def list(self):
        return ("OK", [self.spam_row] if self.spam_row else [])

    def select(self, folder, readonly=False):
        return (self.select_status, [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            header_name = args[2] if len(args) > 2 else ""
            self.searches.append(str(header_name))
            return ("OK", [b" ".join(str(u).encode() for u in self.search_hits.get(header_name, []))])
        if command == "FETCH":
            raw = self.messages.get(int(args[0]))
            if raw is None:
                return ("NO", [None])
            return ("OK", [(f"{args[0]} (BODY[] {{{len(raw)}}}".encode(), raw), b")"])
        return ("OK", [b""])


def test_uid_list() -> None:
    check("uid_list: bytes", S._uid_list([b"3 1 2"]) == [3, 1, 2], str(S._uid_list([b"3 1 2"])))
    check("uid_list: str chunks", S._uid_list(["10", "9"]) == [10, 9], "")
    check("uid_list: junk", S._uid_list([b"1 x 2", None]) == [1, 2], "")
    check("uid_list: non-list", S._uid_list(None) == [], "")


def test_imap_date() -> None:
    check("imap_date: iso", S._imap_date("2026-01-02T03:04:05+00:00") == "02-Jan-2026", S._imap_date("2026-01-02T03:04:05+00:00"))
    check("imap_date: zulu", S._imap_date("2026-01-02T03:04:05Z") == "02-Jan-2026", S._imap_date("2026-01-02T03:04:05Z"))
    check("imap_date: junk falls back", S._imap_date("nonsense").count("-") == 2, S._imap_date("nonsense"))


def test_spam_folder() -> None:
    box = FakeBox({}, {})
    check("spam: discovered", S._spam_folder(box) == "[Gmail]/Spam", S._spam_folder(box))
    localized = FakeBox({}, {}, spam_row=b'(\\HasNoChildren \\Spam) "/" "[Gmail]/Werbung"')
    check("spam: localized name", S._spam_folder(localized) == "[Gmail]/Werbung", S._spam_folder(localized))
    none = FakeBox({}, {}, spam_row=None)
    check("spam: fallback", S._spam_folder(none) == "[Gmail]/Spam", S._spam_folder(none))

    class Broken(FakeBox):
        def list(self):
            raise RuntimeError("boom")

    check("spam: list error fallback", S._spam_folder(Broken({}, {})) == "[Gmail]/Spam", "")


def test_select() -> None:
    check("select: ok", S._select(FakeBox({}, {}), "INBOX") is True, "")
    check("select: missing folder", S._select(FakeBox({}, {}, select_status="NO"), "INBOX") is False, "")


def test_search_union_and_filter() -> None:
    alias = "j.ane.doe@gmail.com"
    addressed = f"To: {alias}\r\nMessage-ID: <a@x>\r\nSubject: code 123456\r\n\r\nhi".encode()
    bcc = f"To: undisclosed-recipients:;\r\nDelivered-To: {alias}\r\nMessage-ID: <b@x>\r\nSubject: code 654321\r\n\r\nhi".encode()
    variant = "To: ja.ne.doe@gmail.com\r\nMessage-ID: <c@x>\r\n\r\nhi".encode()
    box = FakeBox(
        messages={1: variant, 2: addressed, 3: bcc},
        search_hits={"To": [1, 2], "Delivered-To": [3], "X-Original-To": []},
    )
    uids = S._search_uids(box, alias)
    check("search: union of headers", uids == [3, 2], str(uids))
    check("search: all three headers queried", set(box.searches) == {"To", "Delivered-To", "X-Original-To"}, str(box.searches))

    only_bad = FakeBox(messages={}, search_hits={"To": [], "Delivered-To": [], "X-Original-To": []})
    check("search: empty result", S._search_uids(only_bad, alias) == [], "")

    class Dead(FakeBox):
        def uid(self, command, *args):
            raise RuntimeError("connection reset")

    try:
        S._search_uids(Dead({}, {}), alias)
        check("search: total failure raises transient", False)
    except S._Transient:
        check("search: total failure raises transient", True)


def test_scan_skips_replays() -> None:
    alias = "j.ane.doe@gmail.com"
    raw = f"To: {alias}\r\nMessage-ID: <hit@x>\r\nSubject: Your code is 123456\r\n\r\nYour code is 123456".encode()
    box = FakeBox(messages={5: raw}, search_hits={"To": [5], "Delivered-To": [], "X-Original-To": []})
    hit, _preview = S._scan_folder(box, "INBOX", alias, 6, None, 0, "")
    check("scan: finds code", hit is not None and hit[1] == "123456", str(hit))
    again, _ = S._scan_folder(box, "INBOX", alias, 6, None, 0, "", {"<hit@x>"})
    check("scan: skips already-delivered message", again is None, str(again))
    missing, _ = S._scan_folder(FakeBox({}, {}, select_status="NO"), "NOWHERE", alias, 6, None, 0, "")
    check("scan: missing folder is not an error", missing is None, "")


# ------------------------------------------------------------------ extraction


def test_extract_code() -> None:
    found = extract.find_code("Your verification code", "Please use 481516 to sign in.", "")
    check("code: labelled", found is not None and found[0] == "481516", str(found))
    labelled_after = extract.find_code("", "481516 is your login code", "")
    check("code: label after", labelled_after is not None and labelled_after[0] == "481516", str(labelled_after))
    year = extract.find_code("", "Copyright 2026 example.com. Order total: 123456", "")
    check("code: year/order rejected", year is None, str(year))
    seven = extract.find_code("", "Your code is 1234567", "", digits=7)
    check("code: custom digit count", seven is not None and seven[0] == "1234567", str(seven))
    hexcode = extract.find_code("Verify", "code: a1b2c3", "")
    check("code: alphanumeric ignored by design", hexcode is None, str(hexcode))


def test_extract_link() -> None:
    html = (
        '<html><body><p>Confirm your account</p>'
        '<a href="https://app.example.com/verify?token=abc123">Confirm email</a>'
        '<a href="https://app.example.com/unsubscribe">Unsubscribe</a>'
        "</body></html>"
    )
    found = extract.find_link("Confirm your email", "", html)
    check("link: verification wins", found is not None and "/verify" in found[0], str(found))

    only_noise = extract.find_link("newsletter", "", '<a href="https://x.example.com/unsubscribe?token=1">Unsubscribe</a>')
    check("link: unsubscribe-only yields nothing", only_noise is None or "unsubscribe" not in only_noise[0], str(only_noise))

    escaped = extract.find_link("", "", '<a href="https://a.example.com/verify?x=1&amp;y=2">Verify</a>')
    check("link: html entity unescaped", escaped is not None and "&amp;" not in escaped[0], str(escaped))

    relative = extract.find_link("", "", '<a href="/verify?token=1">Verify</a>')
    check("link: relative href ignored", relative is None, str(relative))

    # A link whose only merit is "it exists" must not be reported: that is how a
    # newsletter's "read online" link used to get handed back as a fake confirmation.
    tracking = extract.find_link(
        "weekly digest", "Read online: https://click.mailchimp.com/abc", '<a href="https://click.mailchimp.com/abc">Read</a>'
    )
    check("link: bare tracking link yields nothing", tracking is None, str(tracking))


def test_connect_classification() -> None:
    account = {"email": "nobody@gmail.com", "app_password": "abcdefghijklmnop"}
    saved_port, saved_host = S.IMAP_PORT, S.IMAP_HOST
    try:
        # Localhost port 1 is refused instantly, so this stays a fast offline check.
        S.IMAP_HOST, S.IMAP_PORT = "127.0.0.1", 1
        try:
            S._connect(account)
            check("connect: refused port is transient", False, "no error raised")
        except S._Transient:
            check("connect: refused port is transient", True)
        except S.ToolError as exc:
            check("connect: refused port is transient", False, f"ToolError: {exc}")
        try:
            S._open(account)
            check("open: transport failure becomes ToolError", False, "no error raised")
        except S.ToolError:
            check("open: transport failure becomes ToolError", True)
    finally:
        S.IMAP_HOST, S.IMAP_PORT = saved_host, saved_port


# ------------------------------------------------------------------ live


def live_tests() -> None:
    accounts = S._load_accounts()
    account = accounts[0]
    print(f"\nlive: account {account['email']}")

    box = S._connect(account)
    try:
        spam = S._spam_folder(box)
        check("live: spam folder discovered", bool(spam) and S._select(box, spam), spam)
        check("live: inbox selectable", S._select(box, "INBOX"), "")
    finally:
        S._logout(box)

    alias = S._create_alias("live-test", "dots")["address"]
    account = S._account_for(alias)
    base = account["email"]
    entry = (S._load_state().get("aliases") or {}).get(alias) or {}
    since = int(entry.get("base_uid") or 0)
    print(f"live: alias {alias}")

    def send(to_header: str, envelope_to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = base
        msg["To"] = to_header
        msg["Subject"] = subject
        msg.set_content(body)
        import smtplib
        import ssl

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as client:
            client.login(base, account["app_password"])
            client.send_message(msg, to_addrs=[envelope_to])

    nonce = secrets.token_hex(3)
    # BCC-style delivery: envelope goes to the alias, the To: header names the base account.
    send(base, alias, f"live bcc {nonce}", "Your verification code is 271828")
    started = time.monotonic()
    message, code = S._poll(account, alias, since, 6, 120)
    waited = round(time.monotonic() - started, 1)
    check("live: BCC-only delivery found", code == "271828", f"code={code!r} waited={waited}s")
    check(
        "live: BCC attributed by envelope, not by To",
        bool(message) and alias not in str(message.get("to") or "").lower(),
        str(message.get("to") if message else None),
    )

    if message is not None:
        # Replay guard: the same message must not be handed out twice.
        again, again_code = S._poll(account, alias, since, 6, 6, {str(message["message_id"])})
        check("live: replay suppressed by message-id", again is None, f"got {again_code!r}")

    rows = S._inbox(alias, 5)["messages"]
    check("live: inbox lists the mail", any(r["code"] == "271828" for r in rows), str([r["subject"][:40] for r in rows]))
    check("live: inbox row carries folder/to", all({"folder", "to"} <= set(r) for r in rows), "")

    removed = S._load_state()
    removed.get("aliases", {}).pop(alias, None)
    removed["order"] = [a for a in removed.get("order") or [] if a != alias]
    S._save_state(removed)
    print(f"live: cleaned up {alias}")


def test_wait_reports_codeless_mail() -> None:
    """A matched mail with no code and no link must come back found=true, not as a timeout."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = (S.STATE, S.ACCOUNTS)
        account = {"email": "jane.doe@gmail.com", "app_password": "a" * 16}
        alias = "j.an.e.doe@gmail.com"
        try:
            S.STATE = pathlib.Path(tmp) / "state.json"
            S.ACCOUNTS = pathlib.Path(tmp) / "accounts.json"
            S.ACCOUNTS.write_text(json.dumps([account]), encoding="utf-8")
            S.STATE.write_text(
                json.dumps(
                    {
                        "aliases": {alias: {**account, "base_uid": 10, "created_at": "2026-11-24T10:00:00Z"}},
                        "order": [alias],
                        "last_account": 0,
                    }
                ),
                encoding="utf-8",
            )
            message = {
                "uid": 11,
                "folder": "INBOX",
                "subject": "Confirm your signup",
                "sender": "hi@site.example",
                "received_at": "2026-11-24T10:00:05+00:00",
                "text": "Reply YES to this mail to confirm.",
                "html": "",
                "message_id": "<m@x>",
            }
            orig_poll, orig_account = S._poll, S._account_for
            S._poll = lambda *a, **k: (message, "")  # type: ignore[assignment]
            S._account_for = lambda addr: account  # type: ignore[assignment]
            try:
                result = S._wait(alias, 6, 5)
            finally:
                S._poll, S._account_for = orig_poll, orig_account
            check("wait: codeless mail counts as found", result["found"] is True, str(result))
            check("wait: codeless mail keeps subject", result["subject"] == "Confirm your signup", result["subject"])
            check("wait: codeless mail has empty code", result["code"] == "", result["code"])
            entry = (S._load_state().get("aliases") or {}).get(alias) or {}
            check("wait: watermark advances for codeless mail", entry.get("base_uid") == 11, str(entry.get("base_uid")))
            check("wait: replay guard recorded", entry.get("last_hit") == "<m@x>", str(entry.get("last_hit")))
        finally:
            S.STATE, S.ACCOUNTS = saved


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if "--live" in sys.argv:
        live_tests()
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())