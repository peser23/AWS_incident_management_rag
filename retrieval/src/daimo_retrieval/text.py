"""Query text helpers: normalization, CI-like token extraction, content terms, lexical coverage."""
import re

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*[A-Za-z0-9]|[A-Za-z0-9]")
STOPWORDS = frozenset("""a an and are as at be been but by can could did do does for from had has have how i if in into is
it its me my no not of on or our out over so some than that the their them then there these they this those to up us
was we were what when where which while who why will with would you your across after before during since""".split())


def normalize_query(q: str) -> str:
    """Trim and collapse whitespace; keep case and punctuation (error codes like ORA-12541 matter)."""
    return _WS.sub(" ", q).strip()


def _tokens(q: str) -> list[str]:
    return _TOKEN.findall(q)


def ci_tokens(q: str, min_len: int = 3) -> list[str]:
    """CI-like tokens: contain a digit, hyphen or underscore, or are ALL CAPS. Returned with uppercase forms,
    deduplicated, order preserved (keyword fields match whole, case-exact values)."""
    out: list[str] = []
    for t in _tokens(q):
        if len(t) < min_len:
            continue
        if any(c.isdigit() for c in t) or "-" in t or "_" in t or (t.isalpha() and t.isupper()):
            for form in (t, t.upper()):
                if form not in out:
                    out.append(form)
    return out


def content_terms(q: str) -> list[str]:
    """Lowercased query terms with stopwords removed; error codes and CI tokens are kept whole."""
    seen: list[str] = []
    for t in _tokens(q):
        low = t.lower()
        if low in STOPWORDS or (len(low) < 2 and not low.isdigit()):
            continue
        if low not in seen:
            seen.append(low)
    return seen


def lexical_coverage(terms: list[str], text: str) -> float:
    """Share of query content terms found in the text (case-insensitive, whole-token match)."""
    if not terms:
        return 0.0
    have = {t.lower() for t in _tokens(text)}
    return sum(1 for t in terms if t in have) / len(terms)


# --- guard helpers: control-character stripping, sensitive-data redaction, injection patterns ---

_CONTROL = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f​-‏ -‮⁠﻿]")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{3} \d{2} \d{4}\b")
_SSN_LABELLED = re.compile(r"(?i)(\bssn\b\D{0,3})\d{9}\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_PLACEHOLDER = re.compile(r"<(?:SSN|CARD_NUMBER)>")
INJECTION = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget)\b.{0,20}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions?|rules|prompts?)\b",
    r"\byou are now\b",
    r"\bsystem prompt\b",
    r"(?:^|[.!?]\s+|\b(?:you|please|now)\s+(?:must\s+|should\s+|will\s+)?)(?:act|behave) as (?:a|an|if)\b",
    r"</?\s*(system|incidents?|incident|operator_query)\s*>",
    r"\bdeveloper mode\b",
    r"\bnew (rules|instructions) apply\b",
    r"\bassistant note\b",
    r"\binsufficient_evidence\b",
    r"\bpretend\b.{0,25}\b(rules?|instructions?|guidelines?)\b",
    r"\b(override|bypass)\b.{0,20}\b(your|its)\b.{0,15}\b(rules?|instructions?|guidelines?|guardrails?)\b",
    r"\bfrom now on,? you\b",
    r"\bwhat (instructions|rules|prompt)\b.{0,25}\byou\b",
)]


def strip_control(q: str) -> str:
    """Drop control and zero-width/bidi characters that hide text from a reader but not from the model."""
    return _CONTROL.sub("", q)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact_sensitive(q: str) -> tuple[str, dict[str, int]]:
    """Replace SSNs and Luhn-valid card numbers with placeholders. Returns (text, {detector: count}); the matched
    values are never returned or logged."""
    counts: dict[str, int] = {}

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    def ssn_labelled(m: re.Match) -> str:
        bump("ssn")
        return f"{m.group(1)}<SSN>"

    def ssn(_: re.Match) -> str:
        bump("ssn")
        return "<SSN>"

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            bump("card_number")
            return "<CARD_NUMBER>"
        return m.group(0)

    q = _SSN_LABELLED.sub(ssn_labelled, q)
    q = _SSN.sub(ssn, q)
    q = _CARD.sub(card, q)
    return q, counts


def only_placeholders(q: str) -> bool:
    """True when nothing but redaction placeholders, punctuation and whitespace is left."""
    return not re.search(r"[A-Za-z0-9]", _PLACEHOLDER.sub("", q))


def injection_hit(q: str) -> bool:
    return any(p.search(q) for p in INJECTION)


# --- output guard: things an answer must not contain unless the cited records already did ---

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.I)
_RISKY_PHRASE = [re.compile(p, re.I) for p in (
    r"\bdisable (?:mfa|multi-?factor(?: authentication)?)\b",
    r"\b(?:send|email|forward|upload)\b[^.\n]{0,60}\b(?:to|at)\b[^.\n]{0,30}@",
)]


def output_guard_hits(texts: list[str], context: str) -> list[str]:
    """Detector names that fired on the answer texts. SSN and card numbers always fire; emails, URLs and risky
    phrases fire only when they do not already appear in the incident records that were sent (`context`)."""
    blob, ctx = "\n".join(texts), context.lower()
    hits: list[str] = []
    cleaned, counts = redact_sensitive(blob)
    hits += sorted(counts)
    for name, rx in (("external_email", _EMAIL), ("external_url", _URL)):
        if any(m.group(0).lower().rstrip(".,;") not in ctx for m in rx.finditer(blob)):
            hits.append(name)
    if any(m.group(0).lower() not in ctx for rx in _RISKY_PHRASE for m in rx.finditer(blob)):
        hits.append("risky_phrase")
    return hits
