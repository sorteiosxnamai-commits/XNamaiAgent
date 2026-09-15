"""Identidade de produto para catalogo comum, independente de fornecedor.

Por que existe. O matcher do `sales_agent` foi calibrado para um catalogo de
relogios: decide identidade a partir de `brand`, `model`, `mechanism`,
`dial_color`, `case_size`. Um catalogo generico nao preenche nada disso — tem
nome, referencia, preco e estoque. Rodar aquele matcher sobre estes produtos
produz dois erros opostos: descarta tudo (nenhum campo especializado casa) ou
aceita qualquer coisa da marca certa.

O segundo erro chegou a producao. Perguntado por "Cabo Lightning iPhone
Hmaston", o agente respondeu "Sim, encontrei" e listou fone de ouvido e caixa de
som. Marca igual, identidade completamente diferente — e o cliente nao tem como
perceber a troca, porque preco e estoque vem corretos do produto errado.

A regra deste modulo, entao, e uma so: **marca amplia e ordena candidatos; nunca
os promove a match**. Identidade se prova por referencia, EAN, id, nome exato ou
sobreposicao forte dos termos do nome. Nada de ML, embedding ou dependencia
nova: comparacao deterministica de tokens, que e auditavel e reproduzivel.

Nenhum conhecimento de fornecedor vive aqui: o modulo recebe dicionarios de
produto e texto, e devolve decisao.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: Tipos de pedido que este modulo distingue.
GENERIC_BROWSE = "generic_browse"
SPECIFIC_PRODUCT = "specific_product"

#: Quantos produtos mostrar numa pergunta generica sem numero explicito.
BROWSE_SAMPLE_LIMIT = 5

#: Teto de opcoes numa resposta de ambiguidade. Mais que isto vira lista, nao
#: pergunta.
MAX_AMBIGUOUS = 3

#: Fracao dos termos do pedido que o nome precisa cobrir para identificar.
#: 0.6 aceita "cabo lightning iphone" para "Cabo Lightning iPhone Hmaston"
#: (3 de 4) e recusa "bluetooth" para o mesmo produto (0 de 4).
STRONG_OVERLAP = 0.6

#: Palavras que nao distinguem produto nenhum. Sem esta poda, "voces tem
#: produto X" casaria com qualquer item so pelos conectivos.
_STOPWORDS = frozenset({
    "a", "as", "o", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das",
    "dos", "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem",
    "e", "ou", "que", "qual", "quais", "quanto", "quantos", "custa", "preco",
    "valor", "tem", "temos", "ter", "tenho", "voces", "voce", "vcs", "voc",
    "me", "meu", "minha", "eu", "aqui", "ai", "la", "ola", "oi", "bom", "boa",
    "dia", "tarde", "noite", "por favor", "favor", "algum", "alguma", "alguns",
    "algumas", "esse", "essa", "este", "esta", "isso", "aquele", "aquela",
    "produto", "produtos", "item", "itens", "catalogo", "loja", "estoque",
    "disponivel", "disponiveis", "vendem", "vende", "vender", "venda",
    "mostre", "mostra", "mostrar", "recomende", "recomenda", "recomendar",
    "recomendacao", "opcoes", "opcao", "sobre", "marca", "modelo", "tipo",
    "mais", "menos", "muito", "pouco", "seu", "sua", "os", "the",
})

#: Numeros por extenso que aparecem em "me recomende tres produtos".
_NUMEROS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
}

#: Expressoes que pedem uma amostra do catalogo, sem produto especifico.
_BROWSE_PATTERNS = (
    r"\bo que (voce|voces|vcs)?\s*vende",
    r"\bo que tem\b",
    r"\bo que (voce|voces|vcs)? ?te[mn]\b",
    r"\bque produtos\b",
    r"\bquais produtos\b",
    r"\bmostr[ae]r?\b.*\bprodutos?\b",
    r"\brecomend[ae]r?\b",
    r"\bsugest(ao|oes)\b",
    r"\bcatalogo\b",
)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    """Minusculas, sem acento, sem pontuacao, espacos colapsados."""
    limpo = strip_accents(str(text or "")).casefold()
    limpo = re.sub(r"[^\w\s]+", " ", limpo)
    return re.sub(r"\s+", " ", limpo).strip()


def normalize_tokens(text: str) -> tuple[str, ...]:
    """Termos que efetivamente distinguem produto."""
    return tuple(
        token
        for token in normalize_text(text).split()
        if token and token not in _STOPWORDS
    )


@dataclass(frozen=True)
class CatalogRequest:
    """O que o cliente pediu, na granularidade que o catalogo entende."""

    kind: str
    wanted: int = BROWSE_SAMPLE_LIMIT
    brand: str | None = None
    query: str | None = None


def _wanted_count(texto_normalizado: str) -> int | None:
    digito = re.search(r"\b(\d{1,2})\b", texto_normalizado)
    if digito:
        valor = int(digito.group(1))
        if 1 <= valor <= 10:
            return valor
    for palavra, valor in _NUMEROS.items():
        if re.search(rf"\b{palavra}\b", texto_normalizado):
            return valor
    return None


def _brand_only(tokens: tuple[str, ...]) -> str | None:
    """Sobrou exatamente um termo distintivo? Entao o pedido e so de marca."""
    return tokens[0] if len(tokens) == 1 else None


def classify_catalog_request(text: str) -> CatalogRequest:
    """Pergunta generica de catalogo, ou pedido de produto especifico?

    A distincao decide se o catalogo e consultado direto (amostra real) ou se o
    pedido passa pela prova de identidade. Errar para "generico" mostra produto
    demais; errar para "especifico" devolve pergunta quando havia resposta.
    """
    normalizado = normalize_text(text)
    tokens = normalize_tokens(text)
    parece_browse = any(re.search(p, normalizado) for p in _BROWSE_PATTERNS)

    if not parece_browse:
        return CatalogRequest(kind=SPECIFIC_PRODUCT, query=text or None)

    # "tem cabo lightning Hmaston?" casa `o que tem`, mas tem produto no meio:
    # dois ou mais termos distintivos significam pedido especifico.
    marca = _brand_only(tokens)
    if tokens and marca is None:
        return CatalogRequest(kind=SPECIFIC_PRODUCT, query=text or None)

    return CatalogRequest(
        kind=GENERIC_BROWSE,
        wanted=_wanted_count(normalizado) or BROWSE_SAMPLE_LIMIT,
        brand=marca,
    )


@dataclass
class MatchResult:
    """Decisao de identidade. `matched` so vem com evidencia suficiente."""

    matched: dict[str, Any] | None = None
    ambiguous: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)


def _campo(produto: dict[str, Any], nome: str) -> str:
    valor = produto.get(nome)
    return normalize_text(valor) if valor is not None else ""


def _overlap(pedido: tuple[str, ...], produto: dict[str, Any]) -> float:
    if not pedido:
        return 0.0
    do_nome = set(normalize_tokens(produto.get("name") or ""))
    if not do_nome:
        return 0.0
    return len([t for t in pedido if t in do_nome]) / len(pedido)


def match_generic_catalog_product(
    query: str, candidates: list[dict[str, Any]]
) -> MatchResult:
    """Qual candidato E o produto pedido — ou nenhum.

    Ordem de prova, da mais forte para a mais fraca: referencia exata, EAN
    exato, id exato, nome exato normalizado, sobreposicao forte dos termos.
    Marca coincidente nao aparece nesta lista de proposito: ela nunca prova
    identidade, apenas ordena.

    Varios plausiveis devolvem `ambiguous`, nunca uma escolha arbitraria: entre
    dois cabos diferentes, chutar um e afirmar preco errado com confianca.
    """
    pedido_normalizado = normalize_text(query)
    pedido_tokens = normalize_tokens(query)
    validos = [c for c in candidates if isinstance(c, dict) and c.get("name")]

    for chave in ("reference", "ean", "id"):
        for produto in validos:
            if _campo(produto, chave) and _campo(produto, chave) == pedido_normalizado:
                return MatchResult(matched=produto)

    exatos = [p for p in validos if _campo(p, "name") == pedido_normalizado]
    if len(exatos) == 1:
        return MatchResult(matched=exatos[0])

    pontuados = [
        (produto, _overlap(pedido_tokens, produto))
        for produto in validos
    ]
    fortes = [(p, s) for p, s in pontuados if s >= STRONG_OVERLAP]
    if not fortes:
        return MatchResult()

    fortes.sort(key=lambda par: par[1], reverse=True)
    melhor = fortes[0][1]
    empatados = [p for p, s in fortes if s == melhor]
    if len(empatados) == 1:
        return MatchResult(matched=empatados[0])
    return MatchResult(ambiguous=True, candidates=empatados[:MAX_AMBIGUOUS])


# === fluxo: pedido -> tools -> resposta ====================================


@dataclass
class CatalogAnswer:
    """Resultado do fast path. `kind` diz como o chamador deve responder."""

    kind: str
    products: list[dict[str, Any]] = field(default_factory=list)
    product: dict[str, Any] | None = None
    inventory: dict[str, Any] | None = None


#: Tipos de resposta que o fast path sabe produzir.
ANSWER_BROWSE = "browse"
ANSWER_PRODUCT = "product"
ANSWER_AMBIGUOUS = "ambiguous"
ANSWER_NOT_FOUND = "not_found"



def search_queries_for(text: str) -> list[str]:
    """Consultas a tentar, da mais especifica para a mais ampla.

    A leitura do indice e um `LIKE '%trecho%'` CONTIGUO, entao mandar a frase
    inteira ("quanto custa o Cabo Lightning iPhone Hmaston?") nunca casa: o nome
    do produto nao contem a pergunta. O que casa e a sequencia de termos
    distintivos — e, se ela for especifica demais, prefixos progressivamente
    menores.

    Encurtar so AMPLIA o conjunto de candidatos; quem decide identidade continua
    sendo `match_generic_catalog_product`, entao uma consulta mais larga nunca
    vira resposta errada — no maximo vira ambiguidade ou nenhum match.
    """
    tokens = normalize_tokens(text)
    if not tokens:
        return [normalize_text(text)] if normalize_text(text) else []
    tentativas = [" ".join(tokens[:corte]) for corte in range(len(tokens), 0, -1)]
    vistas: list[str] = []
    for tentativa in tentativas:
        if tentativa and tentativa not in vistas:
            vistas.append(tentativa)
    return vistas


async def resolve_catalog_request(text: str, *, execute) -> CatalogAnswer | None:
    """Responde pelo catalogo quando da para responder com certeza.

    `execute` e a mesma funcao de tool que o restante do sistema usa — o modulo
    nao conhece fornecedor, so o contrato `search_products` / `get_product` /
    `check_inventory`.

    Devolve ``None`` quando a consulta falhou de verdade (erro de tool), para
    que o chamador trate como indisponibilidade em vez de "nao temos". A
    diferenca entre "nao encontrei" e "nao consegui olhar" e a diferenca entre
    uma resposta honesta e uma negativa comercial falsa.
    """
    pedido = classify_catalog_request(text)

    if pedido.kind == GENERIC_BROWSE:
        argumentos: dict[str, Any] = {
            "limit": max(pedido.wanted, BROWSE_SAMPLE_LIMIT),
            "page": 1,
            "available": True,
        }
        if pedido.brand:
            argumentos["brand"] = pedido.brand
        resultado = await execute("search_products", argumentos)
        if not resultado.get("ok"):
            return None
        produtos = list(resultado.get("products") or [])[: pedido.wanted]
        return CatalogAnswer(kind=ANSWER_BROWSE, products=produtos)

    candidatos: list[dict[str, Any]] = []
    for consulta in search_queries_for(text):
        resultado = await execute(
            "search_products", {"query": consulta, "limit": 10, "page": 1}
        )
        if not resultado.get("ok"):
            return None
        candidatos = list(resultado.get("products") or [])
        if candidatos:
            break

    decisao = match_generic_catalog_product(text, candidatos)
    if decisao.ambiguous:
        return CatalogAnswer(kind=ANSWER_AMBIGUOUS, products=decisao.candidates)
    if decisao.matched is None:
        # Candidatos existiam, nenhum E o produto pedido. Marca coincidente nao
        # promove ninguem a resposta afirmativa.
        return CatalogAnswer(kind=ANSWER_NOT_FOUND)

    produto = dict(decisao.matched)
    identificador = produto.get("id") or produto.get("external_id")
    detalhe = await execute("get_product", {"product_id": str(identificador)})
    if detalhe.get("ok") and isinstance(detalhe.get("product"), dict):
        produto.update(detalhe["product"])
    estoque = await execute("check_inventory", {"product_id": str(identificador)})
    return CatalogAnswer(
        kind=ANSWER_PRODUCT,
        product=produto,
        inventory=estoque if estoque.get("ok") else None,
    )
