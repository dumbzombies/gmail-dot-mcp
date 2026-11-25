"""gmail-dot — local stdio MCP for Gmail dot-trick signups.

One IMAP-enabled Gmail account becomes many signup identities: mint a dotted
alias, paste it into a form, then poll the shared inbox for the code or link.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import secrets
import smtplib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402  (sibling file, resolved by absolute path above)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
POLL_INTERVAL = 4
SPAM_AFTER = 15.0
MIN_PASS = 3.0
MAX_FETCH = 10
MAX_SCAN = 60
DEFAULT_WAIT = 120
MAX_WAIT = 600
MAX_ALIASES = 500

CONFIG_DIR = Path.home() / ".config" / "gmail-mcp"
ACCOUNTS = CONFIG_DIR / "accounts.json"
STATE = CONFIG_DIR / "state.json"

HEADER_FIELDS = "(BODY.PEEK[HEADER.FIELDS (TO DELIVERED-TO X-ORIGINAL-TO RECEIVED FROM SUBJECT DATE MESSAGE-ID)])"
# Recipient headers used to attribute a message to one exact alias. From/Subject are
# deliberately absent: a matching string in the sender or the subject (e.g. the account
# mailing its own alias) must never count as delivery to the alias.
RECIPIENT_HEADERS = ("To", "X-Original-To", "Delivered-To")
# Maximum number of dots new_address() may insert (overridable with GMAIL_MCP_MAX_DOTS).
try:
    MAX_DOTS = max(1, min(15, int(os.environ.get("GMAIL_MCP_MAX_DOTS") or 8)))
except ValueError:
    MAX_DOTS = 8

_STATE_LOCK = threading.Lock()

# Envelope recipient of the delivering hop: "... for <alias@gmail.com> (version=TLS1_3)".
ENVELOPE_RE = re.compile(
    r"\bfor\s+(?:<([^<>\s]+@[^<>\s]+)>|([^\s<>;()]+@[^\s<>;()]+))", re.IGNORECASE
)

INSTRUCTIONS = """Signup flow in three calls: new_address() -> paste the exact address string into the
signup form -> wait_code() (no arguments; it uses the newest alias). wait_code returns
{"code": "...", "link": "..."}; use whichever the form asks for, and call inbox() to
debug when nothing arrives. Aliases are Gmail dot-tricks with a random dot count in random
positions: every one delivers to the same real inbox, so use the exact string that
new_address returned. Mail is matched only when the literal recipient equals that string,
so a different dot variant never shows up in another alias's inbox."""


# ---------------------------------------------------------------- results


class NewAddress(TypedDict):
    address: str
    account: str
    mode: str


class WaitResult(TypedDict):
    found: bool
    address: str
    code: str
    link: str
    subject: str
    sender: str
    received_at: str
    preview: str
    message_id: str
    waited_s: float


class InboxRow(TypedDict):
    uid: int
    folder: str
    to: str
    sender: str
    subject: str
    received_at: str
    preview: str
    code: str
    link: str


class Inbox(TypedDict):
    address: str
    messages: list[InboxRow]


# ---------------------------------------------------------------- config


def _log(msg: str) -> None:
    print(f"[gmail-dot] {msg}", file=sys.stderr, flush=True)


def _normalize_password(value: object) -> str:
    return re.sub(r"[\s\xa0\u00a0\u2000-\u200b]+", "", str(value or ""))


def _accounts_path() -> Path:
    override = os.environ.get("GMAIL_MCP_ACCOUNTS", "").strip()
    return Path(override).expanduser() if override else ACCOUNTS


def _load_accounts() -> list[dict[str, str]]:
    path = _accounts_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise ToolError(
            "no gmail accounts configured; run: python3 server.py --add you@gmail.com '<app password>'"
        ) from None
    if not isinstance(raw, list):
        raise ToolError(
            "no gmail accounts configured; run: python3 server.py --add you@gmail.com '<app password>'"
        )
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        address = str(item.get("email") or "").strip()
        password = _normalize_password(item.get("app_password"))
        if not address or "@" not in address or not password:
            continue
        out.append({"email": address, "app_password": password})
    if not out:
        raise ToolError(
            "no gmail accounts configured; run: python3 server.py --add you@gmail.com '<app password>'"
        )
    return out


def _empty_state() -> dict[str, Any]:
    return {"aliases": {}, "order": [], "last_account": -1}


def _load_state() -> dict[str, Any]:
    path = STATE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except Exception as exc:
        # Never silently wipe a state file we failed to parse: keep a copy for recovery.
        try:
            backup = path.with_suffix(f".json.corrupt-{int(time.time())}")
            path.replace(backup)
            _log(f"state unreadable ({type(exc).__name__}); moved to {backup.name}")
        except Exception:
            _log(f"state unreadable ({type(exc).__name__}); starting empty")
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    aliases = raw.get("aliases")
    order = raw.get("order")
    if not isinstance(aliases, dict) or not isinstance(order, list):
        return _empty_state()
    return {
        "aliases": aliases,
        "order": [str(a) for a in order],
        "last_account": raw.get("last_account") if isinstance(raw.get("last_account"), int) else -1,
    }


def _save_state(state: dict[str, Any]) -> None:
    aliases = state.get("aliases") or {}
    order = [a for a in (state.get("order") or []) if a in aliases]
    while len(order) > MAX_ALIASES:
        aliases.pop(order.pop(0), None)
    state["order"] = order
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE.parent, 0o700)
    except OSError:
        pass
    tmp = STATE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def _save_accounts(accounts: list[dict[str, str]]) -> None:
    path = _accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(accounts, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _account_for(address: str) -> dict[str, str]:
    accounts = _load_accounts()
    probe = str(address or "").strip()
    if "@" not in probe:
        raise ToolError(f"not an email address: {probe!r}")
    local, _, domain = probe.partition("@")
    canonical = local.replace(".", "").split("+", 1)[0].lower()
    domain = domain.lower()
    for account in accounts:
        account_local, _, account_domain = account["email"].partition("@")
        if (
            canonical
            and canonical == account_local.replace(".", "").split("+", 1)[0].lower()
            and domain == account_domain.lower()
        ):
            return account
    configured = ", ".join(a["email"] for a in accounts)
    raise ToolError(f"no gmail account matches '{probe}' (configured: {configured})")


# ---------------------------------------------------------------- imap


def _uid_list(data: object) -> list[int]:
    """Parse imaplib SEARCH payloads, which arrive as raw ``bytes`` chunks."""
    if not isinstance(data, (list, tuple)):
        return []
    out: list[int] = []
    for chunk in data:
        if isinstance(chunk, (bytes, bytearray)):
            text = bytes(chunk).decode("ascii", "ignore")
        else:
            text = str(chunk or "")
        for value in text.split():
            try:
                out.append(int(value))
            except ValueError:
                continue
    return out


def _decode(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _connect(account: dict[str, str]) -> imaplib.IMAP4_SSL:
    """Open and authenticate one IMAP session.

    Bad credentials raise ``ToolError`` (permanent, the user has to fix it); transport
    problems raise ``_Transient`` so a wait loop can retry instead of dying on a blip.
    """
    try:
        box = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise _Transient(f"imap connect failed: {type(exc).__name__}: {exc}") from None
    try:
        box.login(account["email"], account["app_password"])
        return box
    except imaplib.IMAP4.error as exc:
        _logout(box)
        raise ToolError(
            f"gmail login failed for {account.get('email', '?')}: {type(exc).__name__} — if this is "
            "AUTHENTICATIONFAILED, regenerate the app password at https://myaccount.google.com/apppasswords"
        ) from None
    except Exception as exc:
        _logout(box)
        raise _Transient(f"gmail login interrupted: {type(exc).__name__}: {exc}") from None


def _open(account: dict[str, str]) -> imaplib.IMAP4_SSL:
    """``_connect`` for one-shot callers: a transport failure becomes a clean ToolError."""
    try:
        return _connect(account)
    except _Transient as exc:
        raise ToolError(f"imap unavailable, try again: {exc}") from None


def _logout(box: imaplib.IMAP4_SSL | None) -> None:
    if box is None:
        return
    try:
        box.logout()
    except Exception:
        pass


def _body_parts(message: email.message.Message) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        (text_parts if content_type == "text/plain" else html_parts).append(decoded)
    return "\n".join(text_parts), "\n".join(html_parts)


def _recipients(message: email.message.Message) -> set[str]:
    """Literal recipient addresses (To / X-Original-To / Delivered-To / envelope) of a message.

    Dots are preserved on purpose. Gmail treats ``a.b@`` and ``ab@`` as one mailbox, but
    attribution here is by the exact string, so mail addressed to a different dot variant
    (or to the bare account address) must not be credited to this alias.

    The envelope recipient in the topmost ``Received:`` header ("... for <addr>") is
    included because it is the only dot-exact trace of a BCC delivery that survives
    local delivery, and it is what Gmail's own searches are blind to.
    """
    values: list[str] = []
    for header in RECIPIENT_HEADERS:
        values.extend(str(value) for value in message.get_all(header, []))
    received = message.get("Received")
    if received:
        match = ENVELOPE_RE.search(str(received))
        if match:
            values.append(match.group(1) or match.group(2) or "")
    return {address.strip().lower() for _name, address in getaddresses(values) if address}


def _addressed_exactly(message: email.message.Message, alias: str) -> bool:
    """True only when ``alias`` (dots included) is one of the message's recipients."""
    return alias.strip().lower() in _recipients(message)


def _raw_payload(fetched: object) -> bytes:
    if not isinstance(fetched, (list, tuple)):
        return b""
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return b""


def _max_uid(account: dict[str, str]) -> int:
    box = _open(account)
    try:
        box.select("INBOX", readonly=True)
        status, data = box.uid("SEARCH", None, "ALL")
        if status != "OK":
            return 0
        return max(_uid_list(data), default=0)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"imap error: {type(exc).__name__}: {exc}") from None
    finally:
        _logout(box)


def _fetch(box: imaplib.IMAP4_SSL, uid: int, folder: str) -> dict[str, Any] | None:
    try:
        status, fetched = box.uid("FETCH", str(uid), "(BODY.PEEK[])")
    except Exception:
        return None
    if status != "OK":
        return None
    raw = _raw_payload(fetched)
    if not raw:
        return None
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    text, html = _body_parts(message)
    try:
        parsed = parsedate_to_datetime(str(message.get("Date") or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        received = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        received = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "uid": int(uid),
        "folder": folder,
        "message_id": _decode(message.get("Message-ID")) or str(uid),
        "sender": _decode(message.get("From")),
        "to": _decode(message.get("To")),
        "subject": _decode(message.get("Subject")),
        "text": text[:50_000],
        "html": html[:100_000],
        "received_at": received,
    }


def _search_uids(
    box: imaplib.IMAP4_SSL,
    alias: str,
    *,
    since_uid: int | None = None,
    since_date: str = "",
    cap: int = MAX_FETCH,
) -> list[int]:
    """Search ``To:`` then keep only messages addressed to this exact alias string.

    Gmail's header search ignores dots, so a SEARCH for one dot variant returns every mail
    sent to *any* variant of the account (and to the bare address). ``_addressed_exactly``
    is what makes the result "this alias's inbox only": the literal ``To``/``Delivered-To``
    address must equal ``alias``, dots and all.
    """
    terms: list[list[object]] = [
        ["HEADER", "To", alias],
        ["HEADER", "Delivered-To", alias],
        ["HEADER", "X-Original-To", alias],
    ]
    if since_date:
        terms = [term + ["SINCE", since_date] for term in terms]
    found: set[int] = set()
    failures = 0
    for term in terms:
        try:
            status, data = box.uid("SEARCH", None, *term)
        except Exception as exc:
            failures += 1
            _log(f"search on {term[1]} failed: {type(exc).__name__}: {exc}")
            continue
        if status != "OK":
            failures += 1
            continue
        found.update(_uid_list(data))
    if failures == len(terms):
        raise _Transient("search rejected")
    uids = sorted(found, reverse=True)
    if since_uid is not None:
        uids = [uid for uid in uids if uid > since_uid]
    # The superset can be the whole account; only the newest slice can hold a fresh
    # verification mail, so never spend more than MAX_SCAN header fetches on it.
    uids = uids[:MAX_SCAN]
    kept: list[int] = []
    wanted = alias.strip().lower()
    for uid in uids:
        if len(kept) >= max(1, cap):
            break
        try:
            status, fetched = box.uid("FETCH", str(uid), HEADER_FIELDS)
        except Exception:
            continue
        if status != "OK":
            continue
        raw = _raw_payload(fetched)
        if not raw:
            continue
        try:
            header = email.message_from_bytes(raw, policy=policy.default)
        except Exception:
            continue
        if not _addressed_exactly(header, wanted):
            continue
        kept.append(uid)
    return kept


class _Transient(Exception):
    """Recoverable IMAP problem; the poll loop retries on the next pass."""


def _imap_date(value: str) -> str:
    """``created_at`` ISO string -> IMAP ``DD-Mon-YYYY``, best effort."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        parsed = datetime.now(timezone.utc) - timedelta(days=1)
    return parsed.strftime("%d-%b-%Y")


def _select(box: imaplib.IMAP4_SSL, folder: str) -> bool:
    """Select ``folder`` read-only; False when the mailbox does not exist or is refused."""
    try:
        status, _data = box.select(folder, readonly=True)
    except Exception:
        return False
    return status == "OK"


def _spam_folder(box: imaplib.IMAP4_SSL) -> str:
    """Name of the account's spam mailbox, discovered from the \\Spam attribute.

    Hardcoding ``[Gmail]/Spam`` breaks on accounts whose folders are localised (and on
    legacy ``[Google Mail]`` ones); a missing mailbox would otherwise make every poll
    pass fail once the spam scan kicks in.
    """
    try:
        status, rows = box.list()
    except Exception:
        return "[Gmail]/Spam"
    if status == "OK":
        for row in rows or []:
            if not isinstance(row, (bytes, bytearray)):
                continue
            text = bytes(row).decode("utf-8", "replace")
            match = re.match(r"\(([^)]*)\)\s+(?:\"[^\"]*\"|NIL)\s+(.+)$", text)
            if not match:
                continue
            flags = match.group(1).lower()
            name = match.group(2).strip().strip('"')
            if "\\spam" in flags or "\\junk" in flags:
                return name
    return "[Gmail]/Spam"


def _scan_folder(
    box: imaplib.IMAP4_SSL,
    folder: str,
    alias: str,
    digits: int,
    preview: dict[str, Any] | None,
    since_uid: int | None,
    since_date: str,
    skip_ids: set[str] | None = None,
) -> tuple[tuple[dict[str, Any], str] | None, dict[str, Any] | None]:
    if not _select(box, folder):
        return None, preview
    uids = _search_uids(box, alias, since_uid=since_uid, since_date=since_date, cap=MAX_FETCH)
    for uid in uids:  # newest first
        message = _fetch(box, uid, folder)
        if message is None:
            continue
        if skip_ids and message.get("message_id") in skip_ids:
            continue
        found = extract.find_code(message["subject"], message["text"], message["html"], digits)
        if found:
            return (message, found[0]), preview
        if extract.find_link(message["subject"], message["text"], message["html"], message["sender"]):
            return (message, ""), preview
        if preview is None:
            preview = message
    return None, preview


def _poll(
    account: dict[str, str],
    alias: str,
    since_uid: int,
    digits: int,
    timeout: int,
    skip_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    timeout = max(5, min(MAX_WAIT, int(timeout)))
    started = time.monotonic()
    deadline = started + timeout
    preview: dict[str, Any] | None = None
    passes = 0
    errors = 0
    last_error = ""
    state = _load_state()
    entry = (state.get("aliases") or {}).get(alias) or {}
    created = str(entry.get("created_at") or "")
    spam_since = _imap_date(created)
    skip_ids = set(skip_ids or ())
    if entry.get("last_hit"):
        skip_ids.add(str(entry["last_hit"]))

    box: imaplib.IMAP4_SSL | None = None
    try:
        while True:
            if passes and deadline - time.monotonic() < MIN_PASS:
                break
            passes += 1
            include_spam = (time.monotonic() - started) >= SPAM_AFTER
            pass_started = time.monotonic()
            try:
                # Reuse the session across passes: logging in again every few seconds trips
                # Gmail's connection throttling and stretches a 60s wait into minutes.
                if box is None:
                    box = _connect(account)
                folders: list[tuple[str, int | None, str]] = [("INBOX", since_uid, "")]
                if include_spam:
                    spam = _spam_folder(box)
                    if spam:
                        folders.append((spam, None, spam_since))
                for folder, uid_floor, date_bound in folders:
                    hit, preview = _scan_folder(
                        box, folder, alias, digits, preview, uid_floor, date_bound, skip_ids
                    )
                    if hit is not None:
                        return hit
            except ToolError:
                raise
            except _Transient as exc:
                errors += 1
                last_error = str(exc)
                _log(f"pass failed (retrying): {last_error}")
                _logout(box)
                box = None
            except Exception as exc:
                errors += 1
                last_error = f"{type(exc).__name__}: {exc}"
                _log(f"pass failed (retrying): {last_error}")
                _logout(box)
                box = None
            elapsed = time.monotonic() - pass_started
            if elapsed > 5:
                _log(f"pass {passes} took {elapsed:.1f}s")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(POLL_INTERVAL, remaining))
    finally:
        _logout(box)

    if errors and passes <= errors:
        raise ToolError(f"imap error: {last_error or 'all attempts failed'}")

    return preview, ""


# ---------------------------------------------------------------- aliases


def _dotted_local(local: str, taken: set[str]) -> str:
    """Dotted variant of ``local`` with a random dot count in random positions.

    The count is drawn uniformly from 1..MAX_DOTS (bounded by the local part's length),
    so for a normal-length local part the space is ~10^6 distinct strings -- repeating
    one across hundreds of signups is very unlikely. Uses ``secrets`` so the pattern does
    not depend on the module-level PRNG state.
    """
    if len(local) < 2:
        return ""
    max_dots = max(1, min(MAX_DOTS, len(local) - 1))
    rnd = secrets.SystemRandom()
    for _ in range(40):
        count = rnd.randint(1, max_dots)
        positions = sorted(rnd.sample(range(1, len(local)), count))
        dotted = local
        for position in reversed(positions):
            dotted = dotted[:position] + "." + dotted[position:]
        if dotted == local or ".." in dotted or dotted.startswith(".") or dotted.endswith("."):
            continue
        address = f"{dotted}@gmail.com"
        if address not in taken:
            return address
    return ""


def _plus_local(local: str, taken: set[str]) -> str:
    for _ in range(30):
        address = f"{local}+mm{secrets.token_hex(3)}@gmail.com"
        if address not in taken:
            return address
    return f"{local}+mm{secrets.token_hex(4)}@gmail.com"


def _create_alias(label: str = "", mode: str = "dots") -> NewAddress:
    requested = (mode or "dots").strip().lower()
    if requested not in ("dots", "plus"):
        raise ToolError("mode must be 'dots' or 'plus'")
    accounts = _load_accounts()
    with _STATE_LOCK:
        state = _load_state()
        last = state.get("last_account")
        # -1 on a fresh state so the first alias lands on the first account instead of
        # silently skipping it until the rotation wraps around.
        index = (int(last) + 1) % len(accounts) if isinstance(last, int) else 0
        state["last_account"] = index
        _save_state(state)
    account = accounts[index]
    email_address = account["email"]
    local = email_address.split("@", 1)[0]

    mode_used = requested
    if mode_used == "dots" and len(local) < 6:
        mode_used = "plus"
    address = ""
    if mode_used == "dots":
        for _ in range(30):
            address = _dotted_local(local, set((_load_state().get("aliases") or {}).keys()))
            if address:
                break
        if not address:
            mode_used = "plus"
    if mode_used == "plus":
        address = _plus_local(local, set((_load_state().get("aliases") or {}).keys()))

    base_uid = _max_uid(account)
    with _STATE_LOCK:
        state = _load_state()
        state["aliases"][address] = {
            "account": email_address,
            "base_uid": base_uid,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": str(label or "")[:60],
        }
        order = [a for a in state.get("order") or [] if a != address]
        order.append(address)
        state["order"] = order
        _save_state(state)
    return {"address": address, "account": email_address, "mode": mode_used}


# ---------------------------------------------------------------- tools


def _default_address() -> str:
    state = _load_state()
    order = state.get("order") or []
    if not order:
        raise ToolError("no alias yet; call new_address() first")
    return order[-1]


def _preview_of(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    return extract.collapse(message.get("text") or "")[:300]


def _wait(address: str = "", digits: int = 6, timeout: int = DEFAULT_WAIT) -> WaitResult:
    digits = max(3, min(8, int(digits)))
    timeout = max(5, min(MAX_WAIT, int(timeout)))
    target = str(address or "").strip() or _default_address()
    account = _account_for(target)
    state = _load_state()
    entry = (state.get("aliases") or {}).get(target) or {}
    since_uid = entry.get("base_uid")
    if not isinstance(since_uid, int) or since_uid <= 0:
        since_uid = max(0, _max_uid(account) - 200)

    started = time.monotonic()
    message, code = _poll(account, target, since_uid, digits, timeout)
    waited = round(time.monotonic() - started, 1)
    link = ""
    if message is not None:
        found = extract.find_link(
            message.get("subject", ""), message.get("text", ""), message.get("html", ""), message.get("sender", "")
        )
        if found:
            link = found[0]
    # "found" means a mail addressed exactly to this alias arrived -- not that a code was
    # parsed out of it. A mail with neither code nor link still gets reported (subject,
    # sender, preview) so the caller can read it instead of waiting out the timeout.
    hit = message is not None
    if hit:
        with _STATE_LOCK:
            live = _load_state()
            fresh = {
                **entry,
                "account": account["email"],
                # Spam UIDs live in a different sequence, so the INBOX watermark only
                # advances on an INBOX hit; the message-id guard below covers both.
                "base_uid": int(message["uid"])
                if message.get("folder") == "INBOX"
                else int(entry.get("base_uid") or 0),
                "last_hit": str(message.get("message_id") or ""),
                "created_at": entry.get("created_at")
                or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "label": entry.get("label", ""),
            }
            live.setdefault("aliases", {})[target] = fresh
            _save_state(live)
    return {
        "found": hit,
        "address": target,
        "code": code if hit else "",
        "link": link if hit else "",
        "subject": str(message.get("subject") or "") if message else "",
        "sender": str(message.get("sender") or "") if message else "",
        "received_at": str(message.get("received_at") or "") if message else "",
        "preview": _preview_of(message),
        "message_id": str(message.get("message_id") or "") if message else "",
        "waited_s": waited,
    }


def _inbox(address: str = "", limit: int = 10) -> Inbox:
    limit = max(1, min(25, int(limit)))
    target = str(address or "").strip() or _default_address()
    account = _account_for(target)
    state = _load_state()
    entry = (state.get("aliases") or {}).get(target) or {}
    since_uid = entry.get("base_uid")
    if not isinstance(since_uid, int) or since_uid <= 0:
        since_uid = max(0, _max_uid(account) - 200)
    spam_since = _imap_date(str(entry.get("created_at") or ""))
    rows: list[InboxRow] = []
    box = _open(account)
    try:
        # Include spam: wait_code() scans it, so a debug view that hid it would report
        # "nothing arrived" for exactly the mail the waiter just matched.
        folders: list[tuple[str, int | None, str]] = [("INBOX", since_uid, "")]
        spam = _spam_folder(box)
        if spam:
            folders.append((spam, None, spam_since))
        for folder, uid_floor, date_bound in folders:
            if not _select(box, folder):
                continue
            uids = _search_uids(
                box, target, since_uid=uid_floor, since_date=date_bound, cap=limit
            )
            for uid in uids:
                message = _fetch(box, uid, folder)
                if message is None:
                    continue
                found_code = extract.find_code(
                    message["subject"], message["text"], message["html"], 6
                )
                found_link = extract.find_link(
                    message["subject"], message["text"], message["html"], message["sender"]
                )
                rows.append(
                    {
                        "uid": int(uid),
                        "folder": folder,
                        "to": message["to"],
                        "sender": message["sender"],
                        "subject": message["subject"],
                        "received_at": message["received_at"],
                        "preview": _preview_of(message),
                        "code": found_code[0] if found_code else "",
                        "link": found_link[0] if found_link else "",
                    }
                )
    except _Transient as exc:
        raise ToolError(f"imap error: {exc}") from None
    finally:
        _logout(box)
    rows.sort(key=lambda row: str(row.get("received_at") or ""), reverse=True)
    return {"address": target, "messages": rows[:limit]}


srv = MCPServer("gmail-dot", version="1.0.0", instructions=INSTRUCTIONS)


@srv.tool()
def new_address(label: str = "", mode: str = "dots") -> NewAddress:
    """Get a fresh Gmail dot-trick address for one signup. Put the returned address string verbatim into the form, then call wait_code()."""
    return _create_alias(label, mode)


@srv.tool()
def wait_code(address: str = "", digits: int = 6, timeout: int = DEFAULT_WAIT) -> WaitResult:
    """Block until a mail arrives for the alias (default 120s) and return it. Call right after submitting the form; no address = the newest alias from new_address(). found=true means the mail arrived; code/link are empty when the mail carried neither, so read subject/preview then."""
    return _wait(address, digits, timeout)


@srv.tool()
def inbox(address: str = "", limit: int = 10) -> Inbox:
    """Debug view of one alias's inbox: mail addressed to *that exact dotted address only*, newest first, with any code or link already extracted. Shows only messages newer than the last UID wait_code() consumed (so it reads empty right after a successful wait — that mail was already handed to you)."""
    return _inbox(address, limit)


# ---------------------------------------------------------------- cli


def _self_test() -> int:
    alias = _create_alias("self-test", "dots")
    address = alias["address"]
    account = _account_for(address)
    entry = (_load_state().get("aliases") or {}).get(address) or {}
    since_uid = int(entry.get("base_uid") or 0)
    nonce = secrets.token_hex(3)
    subject = f"gmail-dot self-test {nonce}"
    body = "Your verification code is 314159.\nOr open https://example.com/verify?token=abc"
    print(f"alias: {address}", flush=True)

    def attempt(why: str) -> tuple[bool, dict[str, Any] | None, str]:
        started = time.monotonic()
        message, code = _poll(account, address, since_uid, 6, 90)
        waited = round(time.monotonic() - started, 1)
        link = ""
        if message is not None:
            found = extract.find_link(
                message.get("subject", ""), message.get("text", ""), message.get("html", ""), message.get("sender", "")
            )
            if found:
                link = found[0]
        ok = code == "314159" and link.startswith("https://example.com/verify")
        if ok:
            print(f"self-test OK {address} waited={waited}s ({why})", flush=True)
        else:
            print(
                f"self-test FAIL ({why}) waited={waited}s code={code!r} link={link!r} "
                f"subject={message.get('subject') if message else None!r} "
                f"uid={message.get('uid') if message else None}",
                flush=True,
            )
        return ok, message, code

    msg = EmailMessage()
    msg["From"] = account["email"]
    msg["To"] = address
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as client:
            client.starttls()
            client.login(account["email"], account["app_password"])
            client.send_message(msg)
        print("sent via smtp", flush=True)
    except Exception as exc:
        print(f"smtp send failed: {type(exc).__name__}: {exc}", flush=True)

    ok, _message, _code = attempt("smtp")
    if ok:
        return 0

    print("falling back to IMAP APPEND fixture", flush=True)
    box = _open(account)
    try:
        box.append("INBOX", "\\Seen", None, msg.as_bytes())
    finally:
        _logout(box)
    ok, _message, _code = attempt("append")
    return 0 if ok else 1


def _cli_add(address: str, password: str) -> int:
    normalized = _normalize_password(password)
    if not address or "@" not in address or not normalized:
        print("usage: server.py --add <email> <app_password>", file=sys.stderr)
        return 2
    account = {"email": address, "app_password": normalized}
    try:
        box = _open(account)
        try:
            box.select("INBOX", readonly=True)
            status, data = box.uid("SEARCH", None, "ALL")
            count = len(_uid_list(data)) if status == "OK" else 0
        finally:
            _logout(box)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        accounts = _load_accounts()
    except ToolError:
        accounts = []
    accounts = [a for a in accounts if a["email"].lower() != address.lower()]
    accounts.append(account)
    _save_accounts(accounts)
    print(f"added {address} (inbox {count} messages)")
    return 0


def _cli_list() -> int:
    try:
        accounts = _load_accounts()
    except ToolError:
        print("no accounts; run --add")
        return 0
    state = _load_state()
    aliases = state.get("aliases") or {}
    for account in accounts:
        owned = sum(1 for entry in aliases.values() if isinstance(entry, dict) and entry.get("account") == account["email"])
        print(f"{account['email']}  aliases={owned}")
    return 0


def _cli_doctor() -> int:
    try:
        accounts = _load_accounts()
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    total = 0
    worst = 0
    for account in accounts:
        started = time.monotonic()
        try:
            box = _open(account)
            try:
                box.select("INBOX", readonly=True)
                status, data = box.uid("SEARCH", None, "ALL")
                count = len(_uid_list(data)) if status == "OK" else 0
            finally:
                _logout(box)
        except ToolError as exc:
            print(f"FAIL {account['email']} {exc}")
            return 1
        latency = int((time.monotonic() - started) * 1000)
        total += count
        worst = max(worst, latency)
        print(f"ok {account['email']} inbox_msgs={count} latency_ms={latency}")
    print(f"ok accounts={len(accounts)} inbox_msgs={total} latency_ms={worst}")
    return 0


def cli() -> None:
    argv = sys.argv[1:]
    if not argv:
        srv.run("stdio")
        return
    flag, *rest = argv
    if flag == "--add":
        if len(rest) < 2:
            print("usage: server.py --add <email> <app_password>", file=sys.stderr)
            sys.exit(2)
        sys.exit(_cli_add(rest[0], rest[1]))
    if flag == "--list":
        sys.exit(_cli_list())
    if flag == "--doctor":
        sys.exit(_cli_doctor())
    if flag == "--self-test":
        sys.exit(_self_test())
    print(f"unknown flag: {flag}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    cli()
