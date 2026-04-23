"""Shared utilities for LLM provider agents."""

_CREDIT_EXHAUSTION_KEYWORDS = (
    'quota',
    'credit',
    'billing',
    'insufficient',
    'payment',
    'exceeded your',
    'out of tokens',
    'balance',
)


def _is_credit_exhausted(exc: Exception) -> bool:
    """Return True if the exception signals permanent credit/quota exhaustion."""
    msg = str(exc).lower()
    if hasattr(exc, 'status_code') and getattr(exc, 'status_code', None) == 402:
        return True
    return any(kw in msg for kw in _CREDIT_EXHAUSTION_KEYWORDS)
