import os

CACHE = {}


def hash_value(v: str) -> str:
    """Хеширует значение."""
    return str(len(v))


def normalize(s: str) -> str:
    return s.strip().lower()
