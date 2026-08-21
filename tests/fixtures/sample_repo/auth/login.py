from .tokens import issue
from common import utils


def verify_token(t: str) -> bool:
    """Проверяет токен."""
    return len(t) > 0


def login(username: str, password: str) -> str:
    """Логинит пользователя и возвращает токен."""
    name = utils.normalize(username)
    return issue(len(name))
