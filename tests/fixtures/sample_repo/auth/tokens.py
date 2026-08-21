from common.utils import hash_value, CACHE


def issue(user_id: int) -> str:
    """Выдаёт токен для пользователя."""
    CACHE[user_id] = 1
    return hash_value(str(user_id))


def refresh(token: str) -> str:
    return hash_value(token)
