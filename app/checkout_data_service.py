from __future__ import annotations

import re
import json
import unicodedata
from typing import Any

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import ValidationError

from .commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceConversationState,
    checkout_fields_view,
    checkout_missing_fields,
)
from .config import get_settings
from .models import AgentResult, CheckoutDataInput
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CUSTOMER_FIELDS = {"name", "cpf", "email", "phone", "rg", "gender"}
_ADDRESS_FIELDS = {
    "address", "zipcode", "zip_code", "number", "complement",
    "neighborhood", "city", "state",
}
_BRAZILIAN_STATE_CODES = {
    "ac": "AC", "acre": "AC",
    "al": "AL", "alagoas": "AL",
    "ap": "AP", "amapa": "AP",
    "am": "AM", "amazonas": "AM",
    "ba": "BA", "bahia": "BA",
    "ce": "CE", "ceara": "CE",
    "df": "DF", "distrito federal": "DF",
    "es": "ES", "espirito santo": "ES",
    "go": "GO", "goias": "GO",
    "ma": "MA", "maranhao": "MA",
    "mt": "MT", "mato grosso": "MT",
    "ms": "MS", "mato grosso do sul": "MS",
    "mg": "MG", "minas gerais": "MG",
    "pa": "PA", "para": "PA",
    "pb": "PB", "paraiba": "PB",
    "pr": "PR", "parana": "PR",
    "pe": "PE", "pernambuco": "PE",
    "pi": "PI", "piaui": "PI",
    "rj": "RJ", "rio de janeiro": "RJ",
    "rn": "RN", "rio grande do norte": "RN",
    "rs": "RS", "rio grande do sul": "RS",
    "ro": "RO", "rondonia": "RO",
    "rr": "RR", "roraima": "RR",
    "sc": "SC", "santa catarina": "SC",
    "sp": "SP", "sao paulo": "SP",
    "se": "SE", "sergipe": "SE",
    "to": "TO", "tocantins": "TO",
}
_FIELD_LABELS = {
    "name": ("Nome completo", "<seu nome completo>"),
    "cpf": ("CPF", "<11 dígitos>"),
    "email": ("E-mail", "<seu e-mail>"),
    "phone": ("Telefone com DDD", "<seu telefone>"),
    "address": ("Rua/Avenida", "<nome da rua ou avenida>"),
    "zipcode": ("CEP", "<8 dígitos>"),
    "number": ("Número", "<número do imóvel>"),
    "neighborhood": ("Bairro", "<seu bairro>"),
    "city": ("Cidade", "<sua cidade>"),
    "state": ("Estado/UF", "<nome do estado ou sigla>"),
}
CHECKOUT_RESOLUTION_CAPABILITIES = (
    "normalize_brazilian_state",
    "lookup_address_by_cep",
    "repair_explicit_fields_with_openai",
)


def normalize_zipcode(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 8 else None


def _normalize_cpf(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11 or digits == digits[0] * 11:
        return None
    for size in (9, 10):
        total = sum(int(digit) * weight for digit, weight in zip(
            digits[:size], range(size + 1, 1, -1)
        ))
        check = (total * 10 % 11) % 10
        if check != int(digits[size]):
            return None
    return digits


def _normalize_phone(value: Any) -> str | None:
    """Canoniza para o contrato do TrayAdapter: 10 ou 11 dígitos, sem DDI."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return None
    return digits


def _clean_text(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    return text or None


def _fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(
        "".join(
            char for char in normalized
            if not unicodedata.combining(char)
        ).split()
    )


def normalize_brazilian_state(value: Any) -> str | None:
    return _BRAZILIAN_STATE_CODES.get(_fold_text(value))


def checkout_data_template(
    missing_fields: list[str],
    field_errors: dict[str, str] | None = None,
) -> str:
    errors = field_errors or {}
    requested = [
        field
        for field in CHECKOUT_REQUIRED_FIELDS
        if field in set(missing_fields) | set(errors)
    ]
    if not requested:
        return ""
    if errors:
        intro = "Não consegui validar alguns dados. Corrija e envie neste modelo:"
    else:
        intro = "Para continuar, copie, preencha e envie neste modelo:"
    lines = [
        f"{_FIELD_LABELS[field][0]}: {_FIELD_LABELS[field][1]}"
        for field in requested
        if field in _FIELD_LABELS
    ]
    return "\n".join([
        intro,
        "",
        *lines,
        "",
        "Não precisa repetir os dados que já foram validados.",
    ])


def should_repair_checkout_data(
    message_text: str,
    updates: dict[str, Any],
    missing_fields: list[str],
    field_errors: dict[str, str],
) -> bool:
    if field_errors:
        return True
    populated_lines = [
        line for line in (message_text or "").splitlines()
        if line.strip()
    ]
    return bool(missing_fields and (len(updates) >= 4 or len(populated_lines) >= 4))


async def lookup_address_by_zipcode(zipcode: Any) -> dict[str, str]:
    settings = get_settings()
    normalized = normalize_zipcode(zipcode)
    if not normalized or not settings.checkout_cep_lookup_enabled:
        return {}
    base_url = settings.checkout_cep_lookup_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(f"{base_url}/{normalized}/json/")
            response.raise_for_status()
            payload = response.json()
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
    ) as exc:
        print("[sales.checkout.cep_lookup] failed", {
            "error_type": type(exc).__name__,
            "zipcode_prefix": normalized[:3],
        })
        return {}
    if not isinstance(payload, dict) or payload.get("erro") is True:
        return {}
    state = normalize_brazilian_state(payload.get("uf") or payload.get("estado"))
    resolved = {
        field: value
        for field, value in {
            "address": _clean_text(payload.get("logradouro")),
            "neighborhood": _clean_text(payload.get("bairro")),
            "city": _clean_text(payload.get("localidade")),
            "state": state,
            "zipcode": normalized,
        }.items()
        if value
    }
    print("[sales.checkout.cep_lookup]", {
        "success": bool(resolved),
        "resolved_fields": sorted(resolved),
        "zipcode_prefix": normalized[:3],
    })
    return resolved


async def enrich_checkout_data_from_cep(
    updates: dict[str, Any],
    *,
    known_zipcode: Any = None,
    missing_fields: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    address_fields = {"address", "zipcode", "zip_code", "neighborhood", "city", "state"}
    unresolved = set(missing_fields or []) | set(field_errors or {})
    if not address_fields.intersection(unresolved):
        return dict(updates)
    zipcode = updates.get("zipcode") or updates.get("zip_code") or known_zipcode
    resolved = await lookup_address_by_zipcode(zipcode)
    if not resolved:
        return dict(updates)
    # A CEP exato é a fonte factual para localidade/UF. Rua e bairro são usados
    # somente quando o serviço os retorna; número e complemento sempre vêm do cliente.
    return {**updates, **resolved}


async def repair_checkout_data_with_openai(
    *,
    message_text: str,
    updates: dict[str, Any],
    missing_fields: list[str],
    field_errors: dict[str, str],
) -> dict[str, Any]:
    """Re-extract only unresolved fields; deterministic validators remain authoritative."""
    settings = get_settings()
    repairable = set(missing_fields) | set(field_errors)
    if not settings.openai_api_key or not repairable:
        return dict(updates)
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=CheckoutDataInput,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extraia e normalize dados de checkout explicitamente presentes "
                        "na mensagem. Nunca invente dados ausentes. Converta nomes de "
                        "estados brasileiros para a UF de duas letras, remova apenas a "
                        "formatação de CPF, telefone e CEP, e preserve nomes e endereços. "
                        "Retorne null para qualquer campo não informado."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message_text,
                            "first_extraction": updates,
                            "unresolved_fields": sorted(repairable),
                            "validation_errors": field_errors,
                            "available_resolution_capabilities": list(
                                CHECKOUT_RESOLUTION_CAPABILITIES
                            ),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            call_type="checkout_repair",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, CheckoutDataInput):
            raise ValueError("checkout_repair_schema_missing")
        repaired = parsed.model_dump(mode="json", exclude_none=True)
        accepted_repairs = {
            field: value
            for field, value in repaired.items()
            if field in repairable
        }
        print("[sales.checkout.repair]", {
            "attempted": True,
            "repairable_fields": sorted(repairable),
            "repaired_fields": sorted(accepted_repairs),
        })
        return {**updates, **accepted_repairs}
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        print("[sales.checkout.repair] failed", {
            "error_type": type(exc).__name__,
            "repairable_fields": sorted(repairable),
        })
        return dict(updates)


def update_checkout_data(
    state: CommerceConversationState,
    updates: dict[str, Any],
) -> AgentResult:
    allowed = _CUSTOMER_FIELDS | _ADDRESS_FIELDS
    unknown = sorted(set(updates) - allowed)
    errors: dict[str, str] = {
        field: "field_not_allowed" for field in unknown
    }
    normalized: dict[str, str | None] = {}
    for field, value in updates.items():
        if field not in allowed or value is None:
            continue
        canonical = "zipcode" if field == "zip_code" else field
        if canonical == "cpf":
            parsed = _normalize_cpf(value)
            if parsed is None:
                errors[canonical] = "invalid_cpf"
            else:
                normalized[canonical] = parsed
        elif canonical == "email":
            parsed = _clean_text(value)
            if parsed is None or not _EMAIL_RE.fullmatch(parsed):
                errors[canonical] = "invalid_email"
            else:
                normalized[canonical] = parsed.casefold()
        elif canonical == "phone":
            parsed = _normalize_phone(value)
            if parsed is None:
                errors[canonical] = "invalid_phone"
            else:
                normalized[canonical] = parsed
        elif canonical == "zipcode":
            parsed = normalize_zipcode(value)
            if parsed is None:
                errors[canonical] = "invalid_zipcode"
            else:
                normalized[canonical] = parsed
        elif canonical == "state":
            parsed = normalize_brazilian_state(value)
            if parsed is None:
                errors[canonical] = "invalid_state"
            else:
                normalized[canonical] = parsed
        else:
            parsed = _clean_text(value)
            if parsed is None and canonical != "complement":
                errors[canonical] = "empty_value"
            else:
                normalized[canonical] = parsed or ""

    draft = state.checkout_draft.model_copy(deep=True)
    for field, value in normalized.items():
        if field in _CUSTOMER_FIELDS:
            setattr(draft.customer, field, value)
        else:
            setattr(draft.address, "zip_code" if field == "zipcode" else field, value)
    missing = checkout_missing_fields(draft)
    shipping_zipcode_changed = bool(
        normalized.get("zipcode")
        and state.shipping_quote_zipcode
        and normalized["zipcode"] != state.shipping_quote_zipcode
    )
    print("[sales.checkout.data.updated]", {
        "updated_fields": sorted(normalized),
        "cpf_present": bool(draft.customer.cpf),
        "email_present": bool(draft.customer.email),
        "phone_present": bool(draft.customer.phone),
        "address_complete": not any(
            field in missing
            for field in ("address", "zipcode", "number", "neighborhood", "city", "state")
        ),
    })
    print("[sales.checkout.missing_fields]", {
        "missing_count": len(missing),
        "missing_fields": missing,
    })
    template = checkout_data_template(missing, errors)
    return AgentResult(
        reply_text=template or "Dados de checkout atualizados.",
        intent="commerce",
        commercial_data={
            "success": not errors,
            "checkout_fields": checkout_fields_view(draft),
            "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
            "missing_fields": missing,
            "field_errors": errors,
            "input_template": template or None,
            "shipping_quote_required": shipping_zipcode_changed,
        },
        response_metadata={
            "domain": "commerce",
            "checkout_state": {"checkout_draft": draft.model_dump(mode="json")},
            **(
                {
                    "shipping_state": {
                        "shipping_quote_zipcode": None,
                        "shipping_quotes": [],
                        "selected_shipping": None,
                    },
                    "purchase_stage": "shipping_quote",
                }
                if shipping_zipcode_changed
                else {"purchase_stage": "checkout_data"}
            ),
            **(
                {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if missing and not shipping_zipcode_changed
                else {"clear_pending_action": True}
            ),
            "used_tray": False,
        },
    )
