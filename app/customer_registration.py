"""Deterministic Xnamai customer registration flow.

The flow never depends on an LLM: the Intent Router recognises the request,
this module collects fields (labelled or in natural language), validates them,
shows a masked review, checks the document for an existing customer and only
then mutates the commercial provider — once, after an explicit confirmation.
Provider mutations remain disabled by default.

Registration PII lives only in ``CommerceConversationState.customer_registration``
(never in Qualification Slots, memory or logs).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Awaitable, Callable

from .commerce_context import CommerceConversationState
from .models import AgentResult
from .sales.intent_router import commerce_action_from_text, is_customer_registration_request, is_greeting


PENDING_REGISTRATION_DATA = "awaiting_customer_registration_data"
PENDING_REGISTRATION_CONFIRMATION = "awaiting_customer_registration_confirmation"
REGISTRATION_PENDING_ACTIONS = frozenset({PENDING_REGISTRATION_DATA, PENDING_REGISTRATION_CONFIRMATION})

#: Creating is only offered when duplicates can be ruled out first.
REQUIRED_CAPABILITIES = frozenset({"create_customer", "lookup_customer_by_document"})

#: Terminal statuses that forbid another automatic creation in this conversation.
_BLOCKING_STATUSES = frozenset({"unknown", "lookup_failed", "ambiguous", "creation_pending", "created_pending_sync"})

_AMBIGUOUS_REPLY = (
    "Encontrei mais de um cadastro da Xnamai com esse documento. Para não vincular "
    "nem criar o cadastro errado, vou encaminhar para a equipe da Xnamai verificar."
)
_PENDING_REPLY = (
    "Já existe um pedido de cadastro com esse documento em andamento. Para evitar "
    "duplicidade, não vou enviar outro; a equipe da Xnamai confirma com você."
)
_LOOKUP_FAILED_REPLY = (
    "Não consegui verificar com segurança se já existe um cadastro com esse documento. "
    "Para não duplicar, vou encaminhar para a equipe da Xnamai concluir."
)


def _blocked_by_lookup(status: str | None) -> AgentResult:
    """Lookup that is neither FOUND nor NOT_FOUND: zero POST, handoff."""
    if status == "AMBIGUOUS":
        reply, state = _AMBIGUOUS_REPLY, "ambiguous"
    elif status == "CREATION_PENDING":
        reply, state = _PENDING_REPLY, "creation_pending"
    else:
        reply, state = _LOOKUP_FAILED_REPLY, "lookup_failed"
    return _result(
        reply, safety_reason=f"customer_registration_{state}", handoff=True,
        status=state, draft={}, pending_action=None, clear=True,
    )

_CONFIRM = frozenset({
    "sim", "confirmo", "sim confirmo", "pode cadastrar", "pode criar",
    "confirmar cadastro", "confirmo o cadastro", "dados corretos", "confirmo cadastro",
    "sim confirmo o cadastro", "esta correto", "esta tudo certo", "tudo certo",
})
_REJECT = frozenset({
    "nao", "nao confirmo", "quero corrigir", "corrigir", "esta errado", "tem erro",
})
_CANCEL = re.compile(
    r"^(?:(?:quero |pode )?cancela(?:r)?(?: o| meu)?(?: cadastro)?|desisto|desisti(?: do cadastro)?|"
    r"nao quero mais(?: me cadastrar| o cadastro| cadastro)?|deixa(?: pra| para) la|esquece)$"
)
_LABELS = {
    "tipo": "person_type",
    "pessoa": "person_type",
    "nome": "legal_name",
    "nome completo": "legal_name",
    "nome completo/razao social": "legal_name",
    "razao social": "legal_name",
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
    "cep": "cep", "rua": "rua", "numero": "numero", "número": "numero",
    "complemento": "complemento", "bairro": "bairro", "cidade": "cidade",
    "estado": "estado", "uf": "estado", "inscricao estadual": "inscricao_estadual",
    "suframa": "suframa", "observacao": "observacao",
}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_EMAIL_FIND_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER_FIND_RE = re.compile(r"(?<![\w@])\+?\d[\d.\-/() ]{8,20}\d(?![\w@])")
_DOCUMENT_HINT_RE = re.compile(r"\b(?:cpf|cnpj|documento)\b")
_PHONE_HINT_RE = re.compile(r"\b(?:telefone|celular|whatsapp|whats|fone|zap)\b")
_NAME_RE = re.compile(
    r"\b(?:(?:meu )?nome(?: completo)?(?: (?:e|é))?|me chamo|razao social(?: (?:e|é))?|razão social(?: (?:e|é))?)"
    r"\s+(?!(?:e|é|fantasia)\b)([^\W\d_][^\W\d_' .-]*(?:[ '.-]+[^\W\d_][^\W\d_'.-]*){0,7})",
    re.IGNORECASE,
)
_TRADE_NAME_RE = re.compile(
    r"\bnome fantasia(?: (?:e|é))?\s+([^\W\d_][^\W\d_' .&-]*(?:[ '.&-]+[^\W\d_][^\W\d_'.&-]*){0,7})",
    re.IGNORECASE,
)
#: Words that end a captured name ("meu nome é Maria Souza e meu cpf ...").
_NAME_STOP = frozenset({"e", "meu", "minha", "cpf", "cnpj", "email", "e-mail", "telefone", "celular", "com", "o", "a"})
_PERSON_TYPE_RE = re.compile(r"\b(pessoa fisica|pessoa juridica|pf|pj)\b")

_FIELD_LABELS = {
    "legal_name": "nome completo (ou razão social, se for empresa)",
    "document": "CPF ou CNPJ",
    "email": "e-mail",
    "phone": "telefone com DDD",
}
_FIELD_ORDER = ("document", "legal_name", "email", "phone")
_ERROR_TEXT = {
    "invalid_cpf": "o CPF informado não é válido",
    "invalid_cnpj": "o CNPJ informado não é válido",
    "invalid_email": "o e-mail informado não parece válido",
    "invalid_phone": "o telefone precisa ter DDD e número",
    "invalid_legal_name": "o nome precisa estar completo",
}


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip()


def _short(value: Any) -> str:
    return " ".join(re.sub(r"[^\w\s-]", " ", _fold(value)).split())


def is_registration_request(text: str | None) -> bool:
    """Compat alias: the Intent Router owns the detection."""
    return is_customer_registration_request(text)


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
    digits = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if not re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", digits) or digits == digits[0] * 14:
        return False

    def check(base: str, weights: tuple[int, ...]) -> str:
        remainder = sum((ord(d) - 48) * w for d, w in zip(base, weights)) % 11
        return "0" if remainder < 2 else str(11 - remainder)

    first = check(digits[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = check(digits[:12] + first, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return digits[-2:] == first + second


def _person_type(value: Any, document: str | None = None) -> str | None:
    folded = _fold(value).replace(".", "")
    length = len(re.sub(r"[^A-Z0-9]", "", str(document or "").upper()))
    if length in {11, 14}:
        return "F" if length == 11 else "J"
    if folded in {"f", "pf", "fisica", "pessoa fisica"}:
        return "F"
    if folded in {"j", "pj", "juridica", "pessoa juridica", "empresa"}:
        return "J"
    return None


def _national_phone(value: Any) -> str:
    phone = _digits(value)
    if len(phone) in {12, 13} and phone.startswith("55"):
        phone = phone[2:]
    return phone


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


def _clean_name(raw: str) -> str | None:
    words: list[str] = []
    for word in raw.split():
        if _fold(word).strip(".") in _NAME_STOP and words:
            break
        words.append(word.strip(".,;"))
    name = " ".join(word for word in words if word)
    return name if len(name) >= 3 else None


def extract_registration_fields(text: str | None) -> dict[str, str]:
    """Labelled lines first, then values stated in natural language.

    Deterministic and conservative: e-mail by pattern, CPF/CNPJ only with a
    valid check digit (or an explicit "cpf"/"cnpj" next to an 11/14-digit
    number, so a typo is reported instead of silently ignored), phone only
    when announced or formatted as one, names only after "meu nome é" /
    "me chamo" / "razão social". Nothing is inferred beyond what was written.
    """
    found = parse_registration_fields(text)
    raw = text or ""
    folded = _fold(raw)

    if "email" not in found:
        emails = _EMAIL_FIND_RE.findall(raw)
        if emails:
            found["email"] = emails[-1].strip(".")

    searchable = _EMAIL_FIND_RE.sub(" ", raw)
    document_hint = bool(_DOCUMENT_HINT_RE.search(folded))
    phone_hint = bool(_PHONE_HINT_RE.search(folded))
    for candidate in _NUMBER_FIND_RE.findall(searchable):
        digits = _digits(candidate)
        if "document" not in found and (
            (len(digits) == 11 and _valid_cpf(digits)) or (len(digits) == 14 and _valid_cnpj(digits))
        ):
            found["document"] = digits
            continue
        looks_like_phone = candidate.strip().startswith(("+", "(")) or phone_hint
        if "phone" not in found and looks_like_phone and len(_national_phone(digits)) in {10, 11}:
            found["phone"] = digits
            continue
        if "document" not in found and document_hint and len(digits) in {11, 14}:
            found["document"] = digits  # invalido: a validacao aponta o erro

    if "document" not in found:
        for candidate in re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9]{12}[0-9]{2}(?![A-Za-z0-9])", raw):
            if re.search(r"[A-Za-z]", candidate) and _valid_cnpj(candidate):
                found["document"] = candidate.upper()
                break

    if "legal_name" not in found:
        match = _NAME_RE.search(raw)
        if match:
            name = _clean_name(match.group(1))
            if name:
                found["legal_name"] = name
    if "trade_name" not in found:
        match = _TRADE_NAME_RE.search(raw)
        if match:
            trade = _clean_name(match.group(1))
            if trade:
                found["trade_name"] = trade
    if "person_type" not in found:
        match = _PERSON_TYPE_RE.search(folded)
        if match:
            found["person_type"] = "PF" if match.group(1) in {"pf", "pessoa fisica"} else "PJ"
    return found


def validate_registration_draft(draft: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}
    document = re.sub(r"[^A-Z0-9]", "", str(draft.get("document") or "").upper())
    person_type = _person_type(draft.get("person_type"), document)
    legal_name = " ".join(str(draft.get("legal_name") or "").strip().split())
    trade_name = " ".join(str(draft.get("trade_name") or "").strip().split())
    email = str(draft.get("email") or "").strip().casefold()
    phone = _national_phone(draft.get("phone"))

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
    for field, limit in {"inscricao_estadual": 30, "suframa": 20, "rua": 100,
                         "numero": 100, "complemento": 50, "bairro": 30,
                         "cidade": 50, "observacao": 500}.items():
        value = " ".join(str(draft.get(field) or "").split())
        if value:
            normalized[field] = value[:limit]
    cep = _digits(draft.get("cep"))
    if cep:
        if len(cep) == 8:
            normalized["cep"] = cep
        else:
            errors["cep"] = "invalid_cep"
    estado = str(draft.get("estado") or "").strip().upper()
    if estado:
        if re.fullmatch(r"[A-Z]{2}", estado):
            normalized["estado"] = estado
        else:
            errors["estado"] = "invalid_estado"
    return normalized, errors


def registration_template(errors: dict[str, str] | None = None) -> str:
    """Full labelled form (kept for callers that want the whole model)."""
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


def _missing_fields(draft: dict[str, Any], errors: dict[str, str]) -> tuple[list[str], list[str]]:
    """(fields never provided, fields provided but invalid) — in asking order."""
    missing: list[str] = []
    invalid: list[str] = []
    for field in _FIELD_ORDER:
        if field not in errors:
            continue
        (invalid if str(draft.get(field) or "").strip() else missing).append(field)
    return missing, invalid


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " e " + items[-1]


def registration_prompt(
    draft: dict[str, Any],
    errors: dict[str, str],
    *,
    starting: bool = False,
    channel_name: str | None = None,
) -> str:
    """Ask ONLY for what is still missing or invalid — never the whole form again."""
    missing, invalid = _missing_fields(draft, errors)
    other_invalid = [field for field in errors if field not in _FIELD_ORDER]
    lines: list[str] = []
    if invalid:
        problems = [_ERROR_TEXT.get(errors[field], f"{_FIELD_LABELS[field]} inválido") for field in invalid]
        lines.append("Quase lá: " + _join(problems) + ". Pode enviar de novo?")
    if other_invalid:
        field = other_invalid[0]
        lines.append(f"O campo {field.replace('_', ' ')} não está válido. Pode enviá-lo novamente?")
    wanted = [_FIELD_LABELS[field] for field in missing[:1]]
    if wanted:
        field = missing[0]
        if field == "document":
            lines.append("Claro. Me envie seu CPF ou CNPJ para começarmos.")
        elif field == "legal_name":
            lines.append("Certo. Agora me envie sua razão social." if _person_type(draft.get("person_type"), draft.get("document")) == "J" else "Certo. Agora me envie seu nome completo.")
        elif field == "email":
            lines.append("Qual e-mail você quer usar no cadastro?")
        else:
            lines.append("Para concluir seu cadastro, preciso também do seu telefone com DDD.")
    return "\n".join(lines)


def _masked_document(document: str) -> str:
    return f"***{document[-4:]}" if document else "—"


def _masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "—"


def registration_review(draft: dict[str, str], *, sources: dict[str, str] | None = None) -> str:
    sources = sources or {}
    kind = "Pessoa física" if draft["person_type"] == "F" else "Pessoa jurídica"
    name_note = " (do seu WhatsApp)" if sources.get("legal_name") == "channel" else ""
    phone_note = " (deste WhatsApp)" if sources.get("phone") == "channel" else ""
    return "\n".join((
        "Confira os dados do cadastro da Xnamai:",
        f"Tipo: {kind}",
        f"Nome/Razão social: {draft['legal_name']}{name_note}",
        *([f"Nome fantasia: {draft['trade_name']}"] if draft.get("trade_name") else []),
        f"CPF/CNPJ: {_masked_document(draft['document'])}",
        f"E-mail: {_masked_email(draft['email'])}",
        f"Telefone final: ***{draft['phone'][-4:]}{phone_note}",
        "",
        'Se estiver correto, responda "confirmo o cadastro". Para alterar, envie o dado corrigido.',
    ))


def customer_payload(draft: dict[str, str]) -> dict[str, Any]:
    payload = {
        "tipo": draft["person_type"],
        "razao_social": draft["legal_name"],
        "cnpj": draft["document"],
        "emails": [{"email": draft["email"]}],
        "telefones": [{"numero": draft["phone"]}],
    }
    for source, target in {"trade_name": "nome_fantasia", "inscricao_estadual": "inscricao_estadual",
                           "suframa": "suframa", "cep": "cep", "rua": "rua", "numero": "numero",
                           "complemento": "complemento", "bairro": "bairro", "cidade": "cidade",
                           "estado": "estado", "observacao": "observacao"}.items():
        if draft.get(source):
            payload[target] = draft[source]
    return payload


def registration_capability_available(capabilities: frozenset[str] | set[str]) -> bool:
    return REQUIRED_CAPABILITIES <= set(capabilities)


def _metadata(
    *,
    status: str,
    draft: dict[str, str],
    pending_action: str | None,
    customer_id: str | None = None,
    clear: bool = False,
    sources: dict[str, str] | None = None,
    required_field: str | None = None,
    required_fields: list[str] | None = None,
    trade_name_asked: bool = False,
) -> dict[str, Any]:
    state = {
        "status": status,
        "draft": draft,
        "customer_id": customer_id,
        "sources": dict(sources or {}),
        "required_field": required_field,
        "required_fields": list(required_fields or []),
        "trade_name_asked": trade_name_asked,
    }
    return {
        "domain": "commerce",
        "active_topic": "customer_registration",
        "customer_registration_state": state,
        "pending_action": pending_action,
        "clear_pending_action": clear,
        "used_commerce_provider": bool(customer_id),
    }


def _result(reply: str, *, safety_reason: str | None = None, handoff: bool = False, **metadata: Any) -> AgentResult:
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=handoff,
        safety_reason=safety_reason,
        response_metadata=_metadata(**metadata),
    )


_UNAVAILABLE_REPLY = (
    "O cadastro pelo atendimento ainda não está disponível por aqui. "
    "Vou encaminhar para a equipe da Xnamai concluir com você com segurança."
)


def _apply_channel_defaults(
    draft: dict[str, Any],
    sources: dict[str, str],
    *,
    sender_name: str | None,
    sender_phone: str | None,
) -> None:
    """Channel data only fills gaps; anything the customer typed wins."""
    phone = _national_phone(sender_phone)
    if not draft.get("phone") and sources.get("phone") != "rejected" and len(phone) in {10, 11}:
        draft["phone"] = phone
        sources["phone"] = "channel"
    name = " ".join(str(sender_name or "").split())
    is_company = _person_type(draft.get("person_type"), draft.get("document")) == "J"
    if is_company and sources.get("legal_name") == "channel":
        draft.pop("legal_name", None)  # nome de pessoa nao e razao social
        sources.pop("legal_name", None)
    # The sender's profile name is never an asserted legal name.


def _is_related_to_registration(text: str | None, updates: dict[str, str]) -> bool:
    folded = _short(text)
    return bool(
        updates
        or "cadastr" in folded
        or folded in _CONFIRM
        or folded in _REJECT
        or _CANCEL.match(folded)
    )


async def handle_customer_registration_turn(
    text: str | None,
    *,
    state: CommerceConversationState,
    execute: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    registration_enabled: bool = True,
    commit_enabled: bool = True,
    sender_name: str | None = None,
    sender_phone: str | None = None,
) -> AgentResult | None:
    """Handle one registration turn, or return ``None`` when unrelated.

    While a registration is pending, related messages (data, corrections,
    confirmation, cancel, greetings) stay here; a clear commerce question
    returns ``None`` so it is answered normally — the pending action survives.
    """
    pending = state.pending_action
    active = pending in REGISTRATION_PENDING_ACTIONS
    if not active and not is_customer_registration_request(text):
        return None

    registration = dict(getattr(state, "customer_registration", None) or {})
    draft = dict(registration.get("draft") or {})
    sources = dict(registration.get("sources") or {})
    existing_id = registration.get("customer_id") or state.mercos_customer_id
    if existing_id:
        return _result(
            "Seu cadastro da Xnamai já está vinculado a este atendimento.",
            status="created", draft={}, pending_action=None, customer_id=str(existing_id), clear=True,
        )
    if not active and registration.get("status") == "created_pending_sync" and draft.get("document"):
        try:
            lookup = await execute("lookup_customer_by_document", {"document": draft["document"]})
        except Exception:  # noqa: BLE001
            lookup = {}
        if lookup.get("ok") and lookup.get("found") and lookup.get("customer_id"):
            return _result("Seu cadastro da Xnamai já está vinculado a este atendimento.",
                           status="created", draft={}, pending_action=None,
                           customer_id=str(lookup["customer_id"]), clear=True)
        return _result(
            "Seu cadastro foi recebido pela Mercos e ainda aguarda sincronização. "
            "Não vou enviar outro cadastro.",
            status="created_pending_sync", draft={"document": draft["document"]},
            pending_action=None, clear=True,
        )
    if not active and registration.get("status") in _BLOCKING_STATUSES:
        return _result(
            "Seu pedido de cadastro anterior está em verificação com a equipe da Xnamai. "
            "Para evitar um cadastro duplicado, não vou enviar outro; a equipe retorna por aqui.",
            safety_reason="customer_registration_pending_verification", handoff=True,
            status=str(registration["status"]), draft={}, pending_action=None, clear=True,
        )
    if not registration_enabled:
        # Decide antes de pedir qualquer dado pessoal.
        return _result(
            _UNAVAILABLE_REPLY, safety_reason="customer_registration_unavailable", handoff=True,
            status="unavailable", draft={}, pending_action=None, clear=True,
        )

    folded = _short(text)
    if active and _CANCEL.match(folded):
        return _result(
            "Tudo bem, cancelei o cadastro e descartei os dados enviados. Se quiser retomar, é só me avisar.",
            status="cancelled", draft={}, pending_action=None, clear=True,
        )

    updates = extract_registration_fields(text)
    # A reply to a deterministic question may be a commerce detour. Decide
    # before treating otherwise unlabelled text as a legal or trade name.
    if active and not updates and not is_greeting(text) and (
        commerce_action_from_text(text) is not None or "?" in (text or "")
    ):
        return None
    required_field = registration.get("required_field")
    required_fields = list(registration.get("required_fields") or ([required_field] if required_field else []))
    trade_name_asked = bool(registration.get("trade_name_asked"))
    if active and trade_name_asked and not draft.get("trade_name") and _short(text) in {"nao tenho", "sem nome fantasia", "nao usamos", "pular"}:
        updates = {}
    elif active and trade_name_asked and not draft.get("trade_name") and not updates:
        raw_trade = " ".join(str(text or "").split())
        if raw_trade and not is_greeting(text):
            updates["trade_name"] = raw_trade
    if active and not updates and pending == PENDING_REGISTRATION_DATA:
        expected = required_field
        if not expected:
            _, current_errors = validate_registration_draft(draft)
            expected = next((field for field in _FIELD_ORDER if field in current_errors), None)
        raw = " ".join(str(text or "").split())
        if expected == "legal_name" and raw and not is_greeting(text):
            updates["legal_name"] = raw
        elif expected == "phone" and len(_national_phone(raw)) in {10, 11}:
            updates["phone"] = raw
        elif expected == "trade_name" and raw:
            updates["trade_name"] = raw
        elif expected in {"cep", "rua", "numero", "bairro", "cidade", "estado", "inscricao_estadual", "suframa"} and raw:
            updates[expected] = raw
    if active and not _is_related_to_registration(text, updates) and not is_greeting(text):
        if commerce_action_from_text(text) is not None or "?" in (text or ""):
            return None  # pergunta comercial: responde normalmente, cadastro segue pendente

    if pending == PENDING_REGISTRATION_CONFIRMATION and not updates:
        if folded in _CONFIRM:
            return await _confirm_and_create(draft, sources, execute=execute, commit_enabled=commit_enabled)
        if folded in _REJECT:
            return _result(
                "Sem problema. Me envie o dado que deseja corrigir (nome, CPF/CNPJ, e-mail ou telefone).",
                status="collecting", draft=draft, sources=sources, pending_action=PENDING_REGISTRATION_DATA,
            )

    for field, value in updates.items():
        draft[field] = value
        sources[field] = "customer"
    if "cep" in updates and len(_digits(updates["cep"])) == 8:
        from .checkout_data_service import lookup_address_by_zipcode

        resolved = await lookup_address_by_zipcode(updates["cep"])
        for source, target in {"address": "rua", "neighborhood": "bairro",
                               "city": "cidade", "state": "estado"}.items():
            if resolved.get(source) and not draft.get(target):
                draft[target] = resolved[source]
                sources[target] = "cep"
    _apply_channel_defaults(draft, sources, sender_name=sender_name, sender_phone=sender_phone)
    required_fields = [field for field in required_fields if not draft.get(field)]
    required_field = required_fields[0] if required_fields else None
    normalized, errors = validate_registration_draft(draft)
    errors.pop("person_type", None)  # o tipo vem do documento; pedir o documento basta
    if required_field and not draft.get(required_field):
        errors[required_field] = "required_by_mercos"
    if (not errors.get("document") and not errors.get("legal_name") and
            normalized.get("person_type") == "J" and not draft.get("trade_name") and
            not trade_name_asked and not required_field):
        return _result("Sua empresa usa nome fantasia? Se sim, me envie. Se não, responda 'não tenho'.",
                       status="collecting", draft={**draft, **normalized}, sources=sources,
                       trade_name_asked=True, pending_action=PENDING_REGISTRATION_DATA)
    if errors:
        if not required_field:
            required_field = next((field for field in errors if field not in _FIELD_ORDER), None)
        channel_name = draft.get("legal_name") if sources.get("legal_name") == "channel" else None
        return _result(
            (f"Para concluir seu cadastro, preciso também de {required_field.replace('_', ' ')}."
             if required_field and not draft.get(required_field)
             else registration_prompt(draft, errors, starting=not active, channel_name=channel_name)),
            safety_reason="customer_registration_data_needed" if active else None,
            status="collecting", draft={**draft, **normalized}, sources=sources,
            pending_action=PENDING_REGISTRATION_DATA, required_field=required_field,
            required_fields=required_fields,
            trade_name_asked=trade_name_asked,
        )
    return _result(
        registration_review(normalized, sources=sources),
        status="review", draft=normalized, sources=sources, trade_name_asked=trade_name_asked,
        pending_action=PENDING_REGISTRATION_CONFIRMATION,
    )


async def _confirm_and_create(
    draft: dict[str, Any],
    sources: dict[str, str],
    *,
    execute: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    commit_enabled: bool = True,
) -> AgentResult:
    normalized, errors = validate_registration_draft(draft)
    if errors:
        return _result(
            registration_prompt(draft, errors),
            safety_reason="customer_registration_invalid",
            status="collecting", draft={**draft, **normalized}, sources=sources,
            pending_action=PENDING_REGISTRATION_DATA,
        )

    if not commit_enabled:
        return _result(_UNAVAILABLE_REPLY, safety_reason="customer_registration_unavailable",
                       handoff=True, status="unavailable", draft={}, pending_action=None, clear=True)

    # 1) duplicidade: sem resposta clara da busca por documento, nada e criado.
    try:
        lookup = await execute("lookup_customer_by_document", {"document": normalized["document"]})
    except Exception:  # noqa: BLE001 - falha de consulta nunca libera a criacao
        lookup = {"ok": False, "error": "lookup_exception"}
    if lookup.get("ok") is True and lookup.get("found") is True and lookup.get("customer_id") is not None:
        return _result(
            "Encontrei um cadastro da Xnamai com esse documento e vinculei ao seu atendimento. "
            "Não criei outro para evitar duplicidade.",
            status="linked", draft={}, pending_action=None, customer_id=str(lookup["customer_id"]), clear=True,
        )
    if not (lookup.get("ok") is True and lookup.get("found") is False):
        return _blocked_by_lookup(lookup.get("status"))

    # 2) criacao: uma unica tentativa, sem retry automatico. O provider refaz a
    # busca sob trava por documento; so ela decide se o POST acontece.
    try:
        result = await execute("create_customer", customer_payload(normalized))
    except Exception:  # noqa: BLE001 - excecao depois do envio = resultado desconhecido
        result = {"ok": False, "code": "customer_creation_unknown"}
    if result.get("error") == "creation_blocked":
        return _blocked_by_lookup(result.get("lookup_status"))
    if result.get("ok") is True and result.get("linked_existing") and result.get("customer_id") is not None:
        return _result(
            "Encontrei um cadastro da Xnamai com esse documento e vinculei ao seu atendimento. "
            "Não criei outro para evitar duplicidade.",
            status="linked", draft={}, pending_action=None, customer_id=str(result["customer_id"]), clear=True,
        )
    if result.get("ok") is True and result.get("customer_id") is not None:
        return AgentResult(
            reply_text="Cadastro criado com sucesso na Xnamai. Já podemos continuar seu atendimento.",
            intent="commerce",
            commercial_data={"customer_registration": {"success": True}},
            response_metadata=_metadata(
                status="created", draft={}, pending_action=None,
                customer_id=str(result["customer_id"]), clear=True,
            ),
        )
    if result.get("ok") is True and result.get("status") == "CREATED_PENDING_SYNC":
        return _result(
            "Seu cadastro foi recebido pela Mercos. Estou aguardando a sincronização para vinculá-lo ao atendimento.",
            status="created_pending_sync", draft={"document": normalized["document"]},
            pending_action=None, clear=True,
        )
    code = str(result.get("code") or result.get("error") or "")
    if code == "customer_validation":
        mapping = {"razao_social": "legal_name", "tipo": "person_type", "cnpj": "document",
                   "nome_fantasia": "trade_name", "emails": "email", "telefones": "phone"}
        field_order = ("cep", "rua", "numero", "bairro", "cidade", "estado",
                       "legal_name", "document", "trade_name", "inscricao_estadual",
                       "suframa", "email", "phone")
        required_fields = [mapping.get(field, field) for field in (result.get("fields") or [])]
        required_fields = ["document" if field == "person_type" else field for field in required_fields]
        required_fields = sorted(set(required_fields), key=lambda field: field_order.index(field) if field in field_order else len(field_order))
        if required_fields:
            draft = dict(normalized)
            sources = dict(sources)
            for field in required_fields:
                draft.pop(field, None)
                sources[field] = "rejected"
            field = required_fields[0]
            label = {"email": "seu e-mail", "phone": "seu telefone com DDD",
                     "trade_name": "o nome fantasia", "legal_name": "sua razão social",
                     "document": "seu CPF ou CNPJ"}.get(field, field.replace("_", " "))
            return _result(f"Para concluir seu cadastro, preciso também de {label}.",
                           status="collecting", draft=draft, sources=sources, required_field=field,
                           required_fields=required_fields,
                           pending_action=PENDING_REGISTRATION_DATA)
        return _result("A Mercos rejeitou o cadastro por uma validação que preciso encaminhar à equipe.",
                       safety_reason="customer_registration_validation_unmapped", handoff=True,
                       status="failed", draft={}, pending_action=None, clear=True)
    if code == "duplicate_document":
        return _result("A Mercos indicou que esse documento já está cadastrado. A equipe vai verificar e vincular o cadastro.",
                       safety_reason="customer_registration_duplicate", handoff=True,
                       status="ambiguous", draft={}, pending_action=None, clear=True)
    if code in {"commerce_unavailable", "mutation_disabled"}:
        reply, status = _UNAVAILABLE_REPLY, "unavailable"
    elif code in {"missing_argument"}:
        reply = "Não consegui concluir o cadastro. Vou encaminhar para a equipe da Xnamai."
        status = "failed"
    else:
        # Inclui mutation_state_unknown, customer_id_missing e erros sem codigo:
        # nao se sabe se o cliente foi criado, entao nunca reenviamos.
        reply = (
            "Enviei o cadastro, mas não consegui confirmar o resultado. "
            "Não vou reenviar para evitar cadastro duplicado; a equipe precisa verificar."
        )
        status = "unknown"
    return _result(
        reply, safety_reason=f"customer_registration_{status}", handoff=True,
        status=status, draft={}, pending_action=None, clear=True,
    )
