import re

# Regular expressions for detecting sensitive tokens and credentials
SENSITIVE_PATTERNS = [
    # AWS access key
    r"\bAKIA[0-9A-Z]{16}\b",
    # GitHub Tokens
    r"\bghp_[0-9a-zA-Z]{36}\b",
    r"\bgithub_pat_[0-9a-zA-Z_]{40,}\b",
    # OpenAI API Keys
    r"\bsk-[a-zA-Z0-9_-]{20,}\b",
    # Google API Key
    r"\bAIza[0-9A-Za-z\-_]{35}\b",
    # Slack Tokens
    r"\bxox[baprs]-[0-9a-zA-Z]{10,48}\b",
    # JWT tokens
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    # Bearer tokens
    r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{16,}",
    # Password / secret assignments (e.g. password = "xyz", secret: 12345, db_password: foo)
    r"(?i)\b\w*(?:password|passwd|pwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token)\w*\s*[:=]\s*['\"]?([^\s'\"]{4,})['\"]?",
    # Wi-Fi passwords / credentials
    r"(?i)\bwifi[_-]?(?:password|key|pass)\s*[:=]\s*['\"]?([^\s'\"]+)['\"]?",
    # Private keys
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # Generic high-entropy hex/base64 keys with prefix
    r"(?i)\b(?:key|token|secret)\s*[:=]\s*[0-9a-fA-F]{32,}\b",
]

COMPILED_PATTERNS = [re.compile(p) for p in SENSITIVE_PATTERNS]


def is_sensitive(text: str, types: list = None) -> bool:
    """
    Check if a snippet contains sensitive credentials, passwords, or tokens.
    Uses classified types and pattern matching.
    """
    if not text:
        return False

    # Check classified types if provided
    if types:
        for t in types:
            if t and str(t).lower() in ("password", "credentials"):
                return True

    # Check pattern matches
    for pattern in COMPILED_PATTERNS:
        if pattern.search(text):
            return True

    return False


def redact_for_llm(text: str) -> str:
    """
    Sanitize text before passing it to any LLM prompt.
    Replaces passwords, API keys, and sensitive tokens with [REDACTED_CREDENTIAL].
    """
    if not text:
        return ""

    sanitized = text
    for pattern in COMPILED_PATTERNS:
        sanitized = pattern.sub("[REDACTED_CREDENTIAL]", sanitized)

    return sanitized


def mask_snippet(text: str) -> str:
    """
    Mask a sensitive snippet for safe GUI display by default.
    Keeps general context while masking sensitive credentials.
    """
    if not text:
        return ""

    # If the text is short and consists purely of a password or secret assignment, mask it completely
    if len(text.strip()) <= 60 and is_sensitive(text):
        return "•" * 12 + " (Sensitive Credential Hidden)"

    # Otherwise redact the specific sensitive patterns with bullet masks
    masked = text
    for pattern in COMPILED_PATTERNS:
        def _mask_match(match):
            val = match.group(0)
            if len(val) > 8:
                return val[:2] + "•" * 8 + val[-2:]
            return "•" * len(val)
        masked = pattern.sub(_mask_match, masked)

    return masked
