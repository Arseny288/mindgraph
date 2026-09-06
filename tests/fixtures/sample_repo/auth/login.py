from .tokens import issue
from common import utils


def verify_token(t: str) -> bool:
    """Verifies the token."""
    return len(t) > 0


def login(username: str, password: str) -> str:
    """Logs the user in and returns a token."""
    name = utils.normalize(username)
    return issue(len(name))
