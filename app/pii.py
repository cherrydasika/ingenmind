"""Personal data in questions (R11): find it, and mask it before any model,
tool, web search, session or trace sees it.

Each flow's input guardrail chooses what happens when a question contains
personal data (InputGuardrailConfig.personal_data):
    mask    replace each item with a placeholder ([email], [phone]…) and
            answer the rest (the default)
    refuse  answer with the flow's message instead, without any model call
    allow   pass it through (an internal deployment that needs it); web
            searches and traces are still masked

What counts: email addresses, phone numbers, payment card numbers (Luhn
checked), UK National Insurance numbers, IBANs and IP addresses. Postcodes,
names and street addresses are not: a planning or travel question needs
them, and they are not identifying on their own.

mask_value() masks any value Langfuse is sent (tracing.py), whatever the
flow's choice.
"""
import re

PLACEHOLDERS = {"email": "[email]", "card": "[card number]", "iban": "[bank account]", "ni_number": "[NI number]",
                "phone": "[phone]", "ip": "[IP address]"}
LABELS = {"email": "an email address", "card": "a card number", "iban": "a bank account number",
          "ni_number": "a National Insurance number", "phone": "a phone number", "ip": "an IP address"}

PATTERNS = [   # most specific first: a card number must not be taken for a phone number
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("ni_number", re.compile(r"\b(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b",
                             re.IGNORECASE)),
    ("ip", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    # +44 7700 900123, 07700 900123, (020) 7946 0958, +1-202-555-0143: 10–13 digits, not a year or a price.
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,5}\)[ .-]?)?\d{2,5}(?:[ .-]?\d{2,4}){1,4}(?![\w.])")),
]


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _real(kind: str, text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    if kind == "card":
        return 13 <= len(digits) <= 19 and _luhn(digits)
    if kind == "phone":
        return 10 <= len(digits) <= 13
    return True


def find(text: str) -> list[tuple[str, int, int]]:
    """[(kind, start, end)] of personal data in text, without overlaps."""
    found, taken = [], []
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text or ""):
            if any(m.start() < e and s < m.end() for s, e in taken) or not _real(kind, m.group()):
                continue
            found.append((kind, m.start(), m.end()))
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda f: f[1])


def kinds(text: str) -> list[str]:
    """The kinds of personal data in text, each once, in order of appearance."""
    return list(dict.fromkeys(k for k, _, _ in find(text)))


def mask(text: str) -> str:
    """text with each piece of personal data replaced by its placeholder."""
    if not text:
        return text
    out, at = [], 0
    for kind, start, end in find(text):
        out += [text[at:start], PLACEHOLDERS[kind]]
        at = end
    return "".join(out) + text[at:]


def mask_value(data=None, **kwargs):
    """Mask every string in a value of any shape (Langfuse's mask hook)."""
    data = kwargs.get("data", data)
    if isinstance(data, str):
        return mask(data)
    if isinstance(data, dict):
        return {k: mask_value(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return type(data)(mask_value(v) for v in data)
    return data


def describe(found_kinds: list[str]) -> str:
    """'an email address and a phone number'"""
    words = [LABELS[k] for k in found_kinds]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""
