from common.utils import hash_value, CACHE


class Invoice:
    """A payment invoice."""

    def total(self) -> int:
        """Returns the invoice total."""
        return 0

    def signature(self) -> str:
        CACHE["x"] = 2
        return hash_value("invoice")

    def render(self) -> str:
        """Prints the invoice."""
        return str(self.total())


class TaxInvoice(Invoice):
    """An invoice with tax."""

    def total(self) -> int:
        return super().total() + 1


def make_invoice() -> Invoice:
    """Invoice factory — mentions Invoice, but does not call it directly."""
    cls = Invoice
    return cls()
