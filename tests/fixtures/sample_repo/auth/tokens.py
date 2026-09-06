from common.utils import hash_value, CACHE


def issue(user_id: int) -> str:
    """Issues a token for the user."""
    CACHE[user_id] = 1
    return hash_value(str(user_id))


def refresh(token: str) -> str:
    return hash_value(token)
