"""Message normalization, OTP and verification-link extraction.

Pure functions over plain strings; no I/O, no network.  Ported from
``aio-auto-register/mailmcp/extract.py`` with the same scoring.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://[^\s<>\"'\\]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
ANCHOR_RE = re.compile(
    r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
CSS_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
CODE_WORDS = re.compile(
    r"\b(?:code|otp|passcode|verification|verify|confirm|confirmation|pin|security|signin|sign-in|login)\b",
    re.IGNORECASE,
)
TRACKING_HINTS = (
    "sendgrid.net",
    "mandrillapp.com",
    "mailchimp.com",
    "list-manage.com",
    "hubspotlinks.com",
    "customeriomail.com",
    "sparkpostmail.com",
    "mailgun.org",
    "click.",
)
BAD_LINK_HINTS = (
    "unsubscribe",
    "privacy",
    "terms",
    "preferences",
    "favicon",
    "spam",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".woff",
)
GOOD_LINK_WORDS = (
    "verify",
    "verification",
    "confirm",
    "confirmation",
    "activate",
    "activation",
    "magic",
    "token",
    "reset",
    "signup",
    "auth",
    "login",
)


class _TextExtractor(HTMLParser):
    """Small HTML-to-text pass; ignores script/style/head elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", unescape(value))
    return "".join(parser.parts)


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def visible_text(subject: str, text: str, html: str) -> str:
    """Subject + text + visible HTML, with noise that resembles codes removed."""
    combined = "\n".join(
        part for part in (str(subject or ""), str(text or ""), html_to_text(str(html or ""))) if part
    )
    # Templates interleave CSS hex colours and asset names with the body.
    combined = CSS_COLOR_RE.sub(" ", combined)
    combined = EMAIL_RE.sub(" ", combined)
    combined = URL_RE.sub(" ", combined)
    return combined


def _code_candidates(text: str, digits: int):
    pattern = re.compile(rf"(?<!\d)(\d{{{digits}}})(?!\d)")
    for match in pattern.finditer(text):
        code = match.group(1)
        before = text[max(0, match.start() - 120): match.start()]
        after = text[match.end(): match.end() + 60]
        context = collapse(f"{before} {code} {after}")
        score = 0
        if CODE_WORDS.search(before):
            score += 100
        elif CODE_WORDS.search(after):
            score += 40
        if re.search(r"\b(?:expires?|valid|minutes?|mins?)\b", before, re.IGNORECASE):
            score += 20
        if re.search(r"\b(?:date|year|price|total|amount|order|invoice)\b", context, re.IGNORECASE):
            score -= 40
        # Six-digit years and calendar dates are the common false positive.
        if digits == 6 and re.fullmatch(r"(?:19|20)\d{4}", code):
            score -= 80
        if digits == 6 and re.fullmatch(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])", code):
            score -= 80
        if digits == 6 and re.fullmatch(r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])", code):
            score -= 50
        yield code, score, context


def find_code(subject: str, text: str, html: str, digits: int = 6) -> tuple[str, str] | None:
    """Return ``(code, context)``; labelled codes win over bare numbers."""
    body = visible_text(subject, text, html)
    best: tuple[int, int, str, str] | None = None
    for index, (code, score, context) in enumerate(_code_candidates(body, digits)):
        candidate = (score, -index, code, context)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None or best[0] < 0:
        return None
    return best[2], best[3]


def _clean_url(value: str) -> str:
    url = unescape(unescape(str(value or ""))).strip()
    url = url.rstrip(").,;:!?]}\"'")
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return url


def link_candidates(text: str, html: str) -> list[str]:
    """Every absolute URL in text/HTML, de-duplicated in first-seen order."""
    raw: list[str] = []
    for match in HREF_RE.finditer(html):
        raw.append(match.group(1))
    for match in URL_RE.finditer(text):
        raw.append(match.group(0))
    for match in URL_RE.finditer(html):
        raw.append(match.group(0))
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        url = _clean_url(value)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _anchor_text(html: str) -> dict[str, str]:
    """Map href -> visible anchor text for link scoring."""
    out: dict[str, str] = {}
    for href, inner in ANCHOR_RE.findall(html or ""):
        url = _clean_url(href)
        if not url:
            continue
        visible = collapse(re.sub(r"<[^>]+>", " ", unescape(inner)))
        if visible:
            out.setdefault(url, visible)
    return out


def score_link(url: str, *, anchor: str = "", keyword: str = "", sender: str = "") -> int:
    lowered = url.lower()
    score = 0
    if keyword and keyword.lower() in lowered:
        score += 1000
    if any(word in lowered for word in GOOD_LINK_WORDS):
        score += 100
    if anchor and any(word in anchor.lower() for word in GOOD_LINK_WORDS):
        score += 40
    if any(hint in lowered for hint in TRACKING_HINTS):
        score -= 10
    if any(hint in lowered for hint in BAD_LINK_HINTS):
        score -= 200
    sender_domain = ""
    match = EMAIL_RE.search(sender or "")
    if match and "@" in match.group(0):
        sender_domain = match.group(0).rsplit("@", 1)[1].lower()
    if sender_domain and sender_domain in lowered:
        score += 25
    return score


def find_link(
    subject: str, text: str, html: str, sender: str = "", *, keyword: str = ""
) -> tuple[str, list[str]] | None:
    """Return ``(best_url, candidates)``; candidates are best-first, max 8."""
    anchors = _anchor_text(html)
    scored: list[tuple[int, int, str]] = []
    for index, url in enumerate(link_candidates(text, html)):
        score = score_link(url, anchor=anchors.get(url, ""), keyword=keyword, sender=sender)
        scored.append((score, -index, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    candidates = [item[2] for item in scored if item[0] > -200][:8]
    if not candidates:
        return None
    return candidates[0], candidates
