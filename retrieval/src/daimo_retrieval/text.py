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
