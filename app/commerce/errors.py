from __future__ import annotations

COMMERCE_UNAVAILABLE_CODE = "commerce_provider_unavailable"


class CommerceUnavailableError(RuntimeError):
    """Nenhum provider comercial está configurado.

    Falha explícita e controlada: nunca inventar produto, preço, estoque
    ou pedido, e nunca cair de volta em um provider legado.
    """

    code = COMMERCE_UNAVAILABLE_CODE

    def __init__(self, capability: str = "") -> None:
        self.capability = capability
        suffix = f": {capability}" if capability else ""
        super().__init__(f"{COMMERCE_UNAVAILABLE_CODE}{suffix}")
