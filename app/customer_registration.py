"""Deterministic Xnamai customer registration flow.

The flow never depends on an LLM: it collects labelled fields, validates them,
shows a review, and mutates the commercial provider only after an explicit
confirmation. Provider mutations remain disabled by default.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Awaitable, Callable

from .commerce_context import CommerceConversationState
from .models import AgentResult


PENDING_REGISTRATION_DATA = "awaiting_customer_registration_data"
PENDING_REGISTRATION_CONFIRMATION = "awaiting_customer_registration_confirmation"

_START_PATTERNS = (
    "quero me cadastrar",
    "quero fazer cadastro",
    "fazer meu cadastro",
    "fazer cadastro",
    "criar meu cadastro",
    "cadastro de cliente",
    "cadastrar cliente",
    "abrir cadastro",
)
_CONFIRM = frozenset({
    "sim", "confirmo", "sim confirmo", "pode cadastrar", "pode criar",
    "confirmar cadastro", "confirmo o cadastro", "dados corretos",
})
_REJECT = frozenset({
    "nao", "não", "cancelar", "cancela", "nao confirmo", "não confirmo",
    "quero corrigir", "corrigir",
})
_LABELS = {
    "tipo": "person_type",
    "pessoa": "person_type",
    "nome": "legal_name",
    "nome completo": "legal_name",
    "nome completo/razao social": "legal_name",
    "razao social": "legal_name",
    "razão social": "legal_name",
    "nome fantasia": "trade_name",
    "fantasia": "trade_name",
    "cpf": "document",
    "cnpj": "document",
    "cpf cnpj": "document",
    "cpf/cnpj": "document",
    "documento": "document",
    "email": "email",
    "e-mail": "email",
    "telefone": "phone",
    "celular": "phone",
    "whatsapp": "phone",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip()


def is_registration_request(text: str | None) -> bool:
    folded = _fold(text)
    return any(pattern in folded for pattern in _START_PATTERNS)


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _valid_cpf(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(
            int(digit) * weight
            for digit, weight in zip(digits[:size], range(size + 1, 1, -1))
        )
        if (total * 10 % 11) % 10 != int(digits[size]):
            return False
    return True


def _valid_cnpj(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False

    def check(base: str, weights: tuple[int, ...]) -> str:
        remainder = sum(int(d) * w for d, w in zip(base, weights)) % 11
        return "0" if remainder < 2 else str(11 - remainder)

    first = check(digits[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = check(digits[:12] + first, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return digits[-2:] == first + second


def _person_type(value: Any, document: str | None = None) -> str | None:
    folded = _fold(value).replace(".", "")
    if folded in {"f", "pf", "fisica", "pessoa fisica"}:
        return "F"
    if folded in {"j", "pj", "juridica", "pessoa juridica", "empresa"}:
        return "J"
    length = len(_digits(document))
    return "F" if length == 11 else "J" if length == 14 else None


def parse_registration_fields(text: str | None) -> dict[str, str]:
    """Parse only explicit labelled values; absent fields are never invented."""
    found: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:=]{2,30})\s*[:=]\s*(.+)$", line)
        if not match:
            continue
        label = _fold(match.group(1))
        key = _LABELS.get(label)
        value = " ".join(match.group(2).strip().split())
        if key and value and not (value.startswith("<") and value.endswith(">")):
            found[key] = value
    return found


def validate_registration_draft(draft: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}
    document = _digits(draft.get("document"))
    person_type = _person_type(draft.get("person_type"), document)
    legal_name = " ".join(str(draft.get("legal_name") or "").strip().split())
    trade_name = " ".join(str(draft.get("trade_name") or "").strip().split())
    email = str(draft.get("email") or "").strip().casefold()
    phone = _digits(draft.get("phone"))
    if len(phone) in {12, 13} and phone.startswith("55"):
        phone = phone[2:]

    if person_type not in {"F", "J"}:
        errors["person_type"] = "invalid_person_type"
    else:
        normalized["person_type"] = person_type
    if len(legal_name) < 3:
        errors["legal_name"] = "invalid_legal_name"
    else:
        normalized["legal_name"] = legal_name[:100]
    if trade_name:
        normalized["trade_name"] = trade_name[:100]
    elif legal_name:
        normalized["trade_name"] = legal_name[:100]
    if person_type == "F" and not _valid_cpf(document):
        errors["document"] = "invalid_cpf"
    elif person_type == "J" and not _valid_cnpj(document):
        errors["document"] = "invalid_cnpj"
    elif document:
        normalized["document"] = document
    else:
        errors["document"] = "missing_document"
    if not _EMAIL_RE.fullmatch(email):
        errors["email"] = "invalid_email"
    else:
        normalized["email"] = email
    if len(phone) not in {10, 11}:
        errors["phone"] = "invalid_phone"
    else:
        normalized["phone"] = phone
    return normalized, errors


def registration_template(errors: dict[str, str] | None = None) -> str:
    intro = (
        "Não consegui validar alguns dados. Corrija e envie neste modelo:"
        if errors
        else "Para criar seu cadastro na Xnamai, copie, preencha e envie este modelo:"
    )
    return "\n".join((
        intro,
        "",
        "Tipo: PF ou PJ",
        "Nome completo/Razão social: <nome>",
        "Nome fantasia: <preencha se for empresa>",
        "CPF/CNPJ: <somente números ou formatado>",
        "E-mail: <seu e-mail>",
        "Telefone: <DDD e número>",
        "",
        "Seus dados serão enviados somente após você conferir e confirmar.",
    ))


def _masked_document(document: str) -> str:
    return f"***{document[-4:]}" if document else "—"


def _masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "—"


def registration_review(draft: dict[str, str]) -> str:
    kind = "Pessoa física" if draft["person_type"] == "F" else "Pessoa jurídica"
    return "\n".join((
        "Confira os dados do cadastro da Xnamai:",
        f"Tipo: {kind}",
        f"Nome/Razão social: {draft['legal_name']}",
        f"Nome fantasia: {draft['trade_name']}",
        f"CPF/CNPJ: {_masked_document(draft['document'])}",
        f"E-mail: {_masked_email(draft['email'])}",
        f"Telefone final: ***{draft['phone'][-4:]}",
        "",
        'Se estiver correto, responda "confirmo o cadastro". Para alterar, envie os campos corrigidos.',
    ))


def customer_payload(draft: dict[str, str]) -> dict[str, Any]:
    return {
        "tipo": draft["person_type"],
        "razao_social": draft["legal_name"],
        "nome_fantasia": draft["trade_name"],
        "cnpj": draft["document"],
        "email": draft["email"],
        "telefones": [{"numero": draft["phone"]}],
        "ativo": True,
        "observacao": "Cadastro solicitado pelo cliente no atendimento Xnamai",
    }


def _metadata(
    *,
    status: str,
    draft: dict[str, str],
    pending_action: str | None,
    customer_id: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    state = {
        "status": status,
        "draft": draft,
        "customer_id": customer_id,
    }
    return {
        "domain": "commerce",
        "active_topic": "customer_registration",
        "customer_registration_state": state,
        "pending_action": pending_action,
        "clear_pending_action": clear,
        "used_commerce_provider": bool(customer_id),
    }


async def handle_customer_registration_turn(
    text: str | None,
    *,
    state: CommerceConversationState,
    execute: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    registration_enabled: bool = True,
) -> AgentResult | None:
    """Handle one registration turn, or return ``None`` when unrelated."""
    pending = state.pending_action
    active = pending in {PENDING_REGISTRATION_DATA, PENDING_REGISTRATION_CONFIRMATION}
    if not active and not is_registration_request(text):
        return None

    if not registration_enabled:
        return AgentResult(
            reply_text=(
                "O cadastro pelo atendimento ainda não está habilitado. "
                "Vou encaminhar para a equipe da Xnamai concluir com segurança."
            ),
            intent="commerce",
            handoff_required=True,
            safety_reason="customer_registration_unavailable",
            response_metadata=_metadata(
                status="unavailable", draft={}, pending_action=None, clear=True,
            ),
        )

    registration = dict(getattr(state, "customer_registration", None) or {})
    draft = dict(registration.get("draft") or {})
    existing_id = registration.get("customer_id") or state.mercos_customer_id
    if existing_id:
        return AgentResult(
            reply_text="Seu cadastro da Xnamai já está vinculado a este atendimento.",
            intent="commerce",
            response_metadata=_metadata(
                status="created", draft=draft, pending_action=None,
                customer_id=str(existing_id), clear=True,
            ),
        )

    folded = _fold(text).strip(" ?!.")
    updates = parse_registration_fields(text)
    if pending == PENDING_REGISTRATION_CONFIRMATION and folded in _REJECT:
        return AgentResult(
            reply_text=registration_template(),
            intent="commerce",
            response_metadata=_metadata(
                status="collecting", draft=draft,
                pending_action=PENDING_REGISTRATION_DATA,
            ),
        )

    if pending == PENDING_REGISTRATION_CONFIRMATION and folded in _CONFIRM:
        normalized, errors = validate_registration_draft(draft)
        if errors:
            return AgentResult(
                reply_text=registration_template(errors),
                intent="commerce",
                safety_reason="customer_registration_invalid",
                response_metadata=_metadata(
                    status="collecting", draft={**draft, **normalized},
                    pending_action=PENDING_REGISTRATION_DATA,
                ),
            )
        result = await execute("create_customer", customer_payload(normalized))
        if result.get("ok") is True and result.get("customer_id") is not None:
            customer_id = str(result["customer_id"])
            return AgentResult(
                reply_text="Cadastro criado com sucesso na Xnamai. Já podemos continuar seu atendimento.",
                intent="commerce",
                commercial_data={"customer_registration": {"success": True}},
                response_metadata=_metadata(
                    status="created", draft={}, pending_action=None,
                    customer_id=customer_id, clear=True,
                ),
            )
        code = str(result.get("code") or result.get("error") or "")
        if code in {"mutation_state_unknown", "customer_creation_unknown"}:
            reply = (
                "Enviei o cadastro, mas não consegui confirmar o resultado. "
                "Não vou reenviar para evitar cadastro duplicado; a equipe precisa verificar."
            )
            status = "unknown"
        elif code in {"commerce_unavailable", "mutation_disabled"}:
            reply = (
                "O cadastro pelo atendimento ainda não está habilitado. "
                "Vou encaminhar para a equipe da Xnamai concluir com segurança."
            )
            status = "unavailable"
        else:
            reply = "Não consegui concluir o cadastro. Vou encaminhar para a equipe da Xnamai."
            status = "failed"
        return AgentResult(
            reply_text=reply,
            intent="commerce",
            handoff_required=True,
            safety_reason=f"customer_registration_{status}",
            response_metadata=_metadata(
                status=status, draft=normalized, pending_action=None, clear=True,
            ),
        )

    if updates:
        draft.update(updates)
        normalized, errors = validate_registration_draft(draft)
        if errors:
            return AgentResult(
                reply_text=registration_template(errors),
                intent="commerce",
                safety_reason="customer_registration_data_needed",
                response_metadata=_metadata(
                    status="collecting", draft={**draft, **normalized},
                    pending_action=PENDING_REGISTRATION_DATA,
                ),
            )
        return AgentResult(
            reply_text=registration_review(normalized),
            intent="commerce",
            response_metadata=_metadata(
                status="review", draft=normalized,
                pending_action=PENDING_REGISTRATION_CONFIRMATION,
            ),
        )

    return AgentResult(
        reply_text=registration_template(),
        intent="commerce",
        response_metadata=_metadata(
            status="collecting", draft=draft,
            pending_action=PENDING_REGISTRATION_DATA,
        ),
    )
