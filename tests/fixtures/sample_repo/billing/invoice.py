from common.utils import hash_value, CACHE


class Invoice:
    """Счёт на оплату."""

    def total(self) -> int:
        """Возвращает сумму счёта."""
        return 0

    def signature(self) -> str:
        CACHE["x"] = 2
        return hash_value("invoice")

    def render(self) -> str:
        """Печатает счёт."""
        return str(self.total())


class TaxInvoice(Invoice):
    """Счёт с налогом."""

    def total(self) -> int:
        return super().total() + 1


def make_invoice() -> Invoice:
    """Фабрика счёта — упоминает Invoice, но не вызывает его напрямую."""
    cls = Invoice
    return cls()
