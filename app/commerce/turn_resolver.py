"""De qual produto o cliente esta falando — e o que ele quer saber.

O sintoma que originou este modulo e uma conversa que nao se sustenta::

    BOT     "1. Ring Light  2. FKT-110C Tipo C Kit Carregador  3. FKT-1108 ..."
    CLIENTE "quero o carregador tipo c"
    BOT     "Nao encontrei esse produto"       <- estava na tela
    CLIENTE "tem foto?"
    BOT     "Nao encontrei esse produto"       <- acabou de encontrar

Cada turno virava uma busca nova. O que faltava nao era catalogo nem persona: era
resolver a REFERENCIA contra o que ja esta em jogo na conversa.

Tres decisoes moldam este modulo:

*Puro.* Entram texto e estado, sai uma decisao. Nenhuma tool, nenhum fornecedor,
nenhuma persona. Assim da para testar toda a ambiguidade conversacional sem rede,
sem banco e sem modelo — e o mesmo raciocinio vale se a fonte comercial mudar.

*Lexico gera sinal, estado decide.* "tem?" sozinho nao significa nada; depois de
uma foto significa outra foto, depois de detalhes significa disponibilidade. Por
isso a resolucao le texto E ultima acao E produto ativo E lista apresentada.

*Nunca escolher por sorteio.* Havendo dois carregadores plausiveis, afirmar um
deles produz preco e estoque do produto errado — e o cliente nao tem como
perceber a troca. Ambiguidade vira pergunta, sempre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .generic_catalog import normalize_text, normalize_tokens

# --- acoes ------------------------------------------------------------------
ACTION_BROWSE_CATALOG = "browse_catalog"
ACTION_SEARCH_PRODUCT = "search_product"
ACTION_SELECT_PRODUCT = "select_product"
ACTION_GET_DETAILS = "get_product_details"
ACTION_GET_PRICE = "get_price"
ACTION_CHECK_INVENTORY = "check_inventory"
ACTION_SHOW_MEDIA = "show_media"
ACTION_SHOW_MORE_MEDIA = "show_more_media"
ACTION_COMPARE = "compare_products"
ACTION_REJECT_PRODUCT = "reject_product"
ACTION_CORRECT_REFERENCE = "correct_reference"

# --- tipos de referencia ----------------------------------------------------
REF_EXPLICIT = "explicit"
REF_LIST_POSITION = "list_position"
REF_CURRENT = "current_product"
REF_SEMANTIC = "semantic_previous_list"

# --- fatos ------------------------------------------------------------------
FACT_PRICE = "price"
FACT_INVENTORY = "inventory"
FACT_MEDIA = "media"
FACT_DETAILS = "details"

#: Verbos de pedido. Dizem o que o cliente QUER, nunca qual produto e — buscar
#: "quero carregador" no catalogo nao casa nada.
_CONVERSATIONAL = (
    "estou procurando", "voces tem", "voce tem", "vcs tem", "me mostre",
    "me mostra", "me manda", "me ve", "me vê", "gostaria de ver",
    "gostaria de", "queria ver", "queria", "quero ver", "quero", "preciso de",
    "preciso", "procuro", "busco", "gostei de", "gostei", "fico com",
    "consegue ver", "consegue", "tem como ver", "tem como", "pode ser",
    "vou querer", "tem ai", "tem", "mostra", "mostre", "manda", "ver",
)

_POSICOES = {
    "primeiro": 1, "primeira": 1, "segundo": 2, "segunda": 2, "terceiro": 3,
    "terceira": 3, "quarto": 4, "quarta": 4, "quinto": 5, "quinta": 5,
}

_CURRENT_WORDS = frozenset({
    "esse", "essa", "este", "esta", "isso", "ele", "ela", "dele", "dela",
    "mesmo", "desse", "dessa", "deste", "nesse",
})

_PRICE = (
    "quanto custa", "quanto fica", "quanto sai", "sai por quanto", "e quanto",
    "qual o preco", "qual preco", "qual o valor", "qual valor", "preco", "valor",
)
_INVENTORY = (
    "tem estoque", "ainda tem", "esta disponivel", "tem disponivel",
    "pronta entrega", "quantos tem", "quantas tem", "acabou", "estoque",
    "disponivel", "disponibilidade",
)
_MEDIA = (
    "tem foto", "tem fotos", "tem imagem", "tem imagens", "manda foto",
    "manda a foto", "manda imagem", "mostra foto", "mostra imagem",
    "quero ver", "posso ver", "tem como ver", "me mostra esse", "mostra ele",
    "foto", "fotos", "imagem", "imagens",
)
_DETAILS = (
    "me fala mais", "fala mais", "me explica", "explica", "quais detalhes",
    "detalhes", "quais especificacoes", "especificacoes", "como ele e",
    "informacao dele", "ficha tecnica",
)
_COMPARE = (
    "qual e melhor", "qual o melhor", "qual desses", "qual deles",
    "vale mais a pena", "ou o segundo", "ou aquele", "compara", "comparar",
)
_BROWSE = (
    "o que voces vendem", "o que voce vende", "o que vendem", "o que tem",
    "quais produtos", "que produtos", "ver o catalogo", "catalogo",
    "alguns produtos", "uns produtos", "outros produtos", "me mostre outros",
    "mostre outros", "mostra outros", "tem mais", "mais produtos",
    "recomend", "sugest",
)
_MORE = ("outra", "outro", "mais", "proxima", "proximo", "seguinte")
_REJECT = ("nao esse", "esse nao", "nao quero esse", "nao e esse", "nao")
_CORRECTION = ("quis dizer", "na verdade", "nao,", "nao e", "corrig")

#: Palavras que nomeiam o FATO pedido, nunca o produto. Sem esta poda, "tem
#: foto?" produzia a consulta "foto" — e o turno virava busca de um produto
#: chamado foto, em vez de pedido de midia sobre o produto ativo.
_NON_PRODUCT = frozenset({
    "foto", "fotos", "imagem", "imagens", "midia", "video",
    "preco", "valor", "custa", "custo", "fica", "sai", "quanto", "quanta",
    "estoque", "disponivel", "disponiveis", "disponibilidade", "acabou",
    "ainda", "pronta", "entrega", "quantos", "quantas",
    "detalhes", "detalhe", "especificacoes", "especificacao", "explica",
    "explicar", "fala", "falar", "informacao", "informacoes", "ficha",
    "tecnica", "melhor", "pena", "vale", "catalogo", "opcao", "opcoes",
    "primeiro", "primeira", "segundo", "segunda", "terceiro", "terceira",
    "quarto", "quarta", "quinto", "quinta", "ultimo", "ultima", "anterior",
    "outra", "outro", "outros", "outras", "mais", "proxima", "proximo",
    "seguinte", "parecido", "parecida", "similar", "nao", "sim",
    "posso", "consigo", "da", "pra", "poderia",
})

#: Conectivos que nao distinguem produto. Lista propria, e nao a do
#: `generic_catalog`, porque aquela descarta "tipo" e "modelo" — palavras que
#: ali sao ruido de busca mas aqui SAO o produto ("carregador tipo c").
_CONNECTIVES = frozenset({
    "a", "as", "o", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das",
    "dos", "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem",
    "e", "ou", "que", "qual", "quais", "me", "meu", "minha", "eu", "ai",
    "algum", "alguma", "alguns", "algumas", "produto", "produtos", "item",
    "itens", "loja", "aqui", "voces", "voce", "vcs",
})


def _product_tokens(texto_normalizado: str) -> list[str]:
    """Termos que podem ser parte do NOME de um produto."""
    return [
        token
        for token in texto_normalizado.split()
        if token
        and token not in _CONNECTIVES
        and token not in _CURRENT_WORDS
        and token not in _NON_PRODUCT
    ]

#: Expressoes curtas que significam produto apesar do tamanho.
_SHORT_MEANINGFUL = ("tipo c", "usb c", "type c", "p2", "p3", "3a", "5g")


def _contem(texto: str, termos) -> bool:
    return any(termo in texto for termo in termos)


def product_query_for(text: str) -> str | None:
    """Os termos de PRODUTO da mensagem, sem o verbo do pedido.

    "quero o carregador tipo c" -> "carregador tipo c". Deixar o verbo dentro da
    consulta e o que fazia a busca lexical nao casar nada: nenhum produto se
    chama "quero carregador".
    """
    normalizado = normalize_text(text)
    for verbo in _CONVERSATIONAL:
        normalizado = re.sub(rf"\b{re.escape(verbo)}\b", " ", normalizado)
    normalizado = re.sub(r"\s+", " ", normalizado).strip()

    tokens = _product_tokens(normalizado)
    if not tokens:
        return None
    consulta = " ".join(tokens)
    if len(consulta) <= 2 and not _contem(consulta, _SHORT_MEANINGFUL):
        # Token isolado curto nao identifica produto; "tipo c" identifica.
        return None
    return consulta


def _posicao_pedida(normalizado: str) -> int | None:
    for palavra, posicao in _POSICOES.items():
        if re.search(rf"\b{palavra}\b", normalizado):
            return posicao
    if re.search(r"\bultim[oa]\b", normalizado):
        return -1
    digito = re.search(r"\b(\d{1,2})\b", normalizado)
    if digito:
        valor = int(digito.group(1))
        if 1 <= valor <= 20:
            return valor
    return None


def _referencia_explicita(text: str) -> tuple[str | None, str | None]:
    """(reference, ean) quando o cliente cola um codigo."""
    ean = re.search(r"\b(\d{8}|\d{12,14})\b", text or "")
    referencia = re.search(r"\b([A-Z]{2,}[A-Z0-9]*[-\s]?\d{2,}[A-Z0-9]*)\b", text or "")
    return (referencia.group(1).strip() if referencia else None,
            ean.group(1) if ean else None)


@dataclass
class CommerceTurnResolution:
    """O que o cliente quis dizer, e sobre qual produto."""

    action: str
    reference_type: str | None = None
    reference_position: int | None = None
    product_query: str | None = None
    explicit_reference: str | None = None
    explicit_ean: str | None = None
    explicit_product_id: str | None = None
    resolved_product: Any | None = None
    rejected_product_id: str | None = None
    requested_fact: str | None = None
    requires_catalog_search: bool = False
    requires_clarification: bool = False
    candidates: list[Any] = field(default_factory=list)
    confidence: float = 0.0


def _por_posicao(state, posicao: int):
    lista = list(getattr(state, "last_presented_products", None) or [])
    if not lista:
        return None
    if posicao == -1:
        return lista[-1]
    return next((p for p in lista if p.position == posicao), None)


def _por_semantica(state, consulta: str | None):
    """Casa os termos do pedido contra a lista JA apresentada.

    Resolver aqui antes do catalogo global nao e so economia: o cliente esta
    falando do que acabou de ver, e o catalogo inteiro traria homonimos que nunca
    estiveram na conversa.
    """
    if not consulta:
        return [], None
    lista = list(getattr(state, "last_presented_products", None) or [])
    termos = set(_product_tokens(normalize_text(consulta)))
    if not termos or not lista:
        return [], None
    pontuados = []
    for item in lista:
        do_nome = set(_product_tokens(normalize_text(item.name or "")))
        cobertos = len([t for t in termos if t in do_nome])
        if cobertos:
            pontuados.append((item, cobertos / len(termos)))
    if not pontuados:
        return [], None
    melhor = max(s for _, s in pontuados)
    # 0.66 exige que a maior parte dos termos apareca: com 0.5, "cabo lightning"
    # casava "Cabo Usb" so pela palavra "cabo".
    if melhor < 0.66:
        return [], None
    empatados = [p for p, s in pontuados if s == melhor]
    if len(empatados) == 1:
        return empatados, empatados[0]
    return empatados, None


def _ativo(state):
    return getattr(state, "active_product", None)


def _lista_unica(state):
    lista = list(getattr(state, "last_presented_products", None) or [])
    return lista[0] if len(lista) == 1 else None


def _detectar_acao(normalizado: str, state) -> tuple[str, str | None]:
    """(acao, fato) a partir do texto E do contexto da ultima acao."""
    ultima = getattr(state, "last_commerce_action", None)
    ultimo_fato = getattr(state, "last_requested_fact", None)

    if _contem(normalizado, _COMPARE):
        return ACTION_COMPARE, None
    # Browse antes de midia: "quero ver o catalogo" contem "quero ver", que
    # tambem e pedido de imagem — mas aqui o objeto e o catalogo, nao o produto.
    if _contem(normalizado, _BROWSE):
        return ACTION_BROWSE_CATALOG, None
    if _contem(normalizado, _MEDIA):
        if _contem(normalizado, _MORE) and ultimo_fato == FACT_MEDIA:
            return ACTION_SHOW_MORE_MEDIA, FACT_MEDIA
        return ACTION_SHOW_MEDIA, FACT_MEDIA
    # "manda outra" / "tem outra?" so pedem outra IMAGEM dentro de contexto de
    # midia; fora dele, pedem outro produto.
    if _contem(normalizado, _MORE) and (
        ultima in {ACTION_SHOW_MEDIA, ACTION_SHOW_MORE_MEDIA} or ultimo_fato == FACT_MEDIA
    ):
        return ACTION_SHOW_MORE_MEDIA, FACT_MEDIA
    # "quanto ELE custa" tem palavra no meio: casar o par de termos, e nao so a
    # expressao contigua, evita depender da ordem exata da frase.
    palavras = set(normalizado.split())
    if _contem(normalizado, _PRICE) or (
        "quanto" in palavras and palavras & {"custa", "fica", "sai", "custam"}
    ):
        return ACTION_GET_PRICE, FACT_PRICE
    if _contem(normalizado, _INVENTORY):
        return ACTION_CHECK_INVENTORY, FACT_INVENTORY
    if _contem(normalizado, _DETAILS):
        return ACTION_GET_DETAILS, FACT_DETAILS
    # "tem?" pelado: o contexto diz se e disponibilidade.
    if normalizado.strip(" ?") in {"tem", "ainda tem"}:
        return ACTION_CHECK_INVENTORY, FACT_INVENTORY
    return ACTION_SELECT_PRODUCT, None


def resolve_commerce_turn(text: str, *, state) -> CommerceTurnResolution:
    """Interpreta o turno contra o estado da conversa.

    Ordem de resolucao da referencia, da evidencia mais forte para a mais fraca:
    codigo explicito, posicao na lista, referencia contextual ("esse"), termos
    que casam a lista apresentada, e so entao busca no catalogo global.
    """
    normalizado = normalize_text(text)
    consulta = product_query_for(text)
    referencia, ean = _referencia_explicita(text)
    acao, fato = _detectar_acao(normalizado, state)

    resolucao = CommerceTurnResolution(
        action=acao,
        product_query=consulta,
        explicit_reference=referencia,
        explicit_ean=ean,
        requested_fact=fato,
    )

    # --- rejeicao / correcao ------------------------------------------------
    negativo = bool(re.match(r"^\s*n[aã]o\b", normalizado)) or _contem(
        normalizado, ("nao esse", "esse nao", "nao quero esse")
    )
    if negativo or _contem(normalizado, _CORRECTION):
        ativo = _ativo(state)
        if ativo is not None:
            resolucao.rejected_product_id = ativo.product_id
        posicao = _posicao_pedida(normalizado)
        if posicao is not None:
            alvo = _por_posicao(state, posicao)
            if alvo is not None:
                resolucao.action = ACTION_CORRECT_REFERENCE
                resolucao.reference_type = REF_LIST_POSITION
                resolucao.reference_position = posicao
                resolucao.resolved_product = alvo
                resolucao.confidence = 0.9
                return resolucao
        resolucao.action = ACTION_REJECT_PRODUCT
        resolucao.requires_catalog_search = bool(consulta)
        return resolucao

    # --- 1. referencia explicita -------------------------------------------
    if referencia or ean:
        resolucao.reference_type = REF_EXPLICIT
        resolucao.requires_catalog_search = True
        resolucao.confidence = 0.95
        return resolucao

    # --- 2. posicao na lista ------------------------------------------------
    posicao = _posicao_pedida(normalizado)
    if posicao is not None and not consulta:
        alvo = _por_posicao(state, posicao)
        resolucao.reference_type = REF_LIST_POSITION
        resolucao.reference_position = posicao
        if alvo is not None:
            resolucao.resolved_product = alvo
            resolucao.confidence = 0.9
            if resolucao.action == ACTION_SELECT_PRODUCT:
                ultimo_fato = getattr(state, "last_requested_fact", None)
                if ultimo_fato == FACT_INVENTORY:
                    resolucao.action = ACTION_CHECK_INVENTORY
                    resolucao.requested_fact = FACT_INVENTORY
                elif ultimo_fato == FACT_PRICE:
                    resolucao.action = ACTION_GET_PRICE
                    resolucao.requested_fact = FACT_PRICE
        else:
            resolucao.requires_clarification = True
        return resolucao

    # --- 3. termos que casam a lista apresentada ---------------------------
    candidatos, unico = _por_semantica(state, consulta)
    if unico is not None:
        resolucao.reference_type = REF_SEMANTIC
        resolucao.resolved_product = unico
        resolucao.confidence = 0.8
        return resolucao
    if len(candidatos) > 1:
        resolucao.reference_type = REF_SEMANTIC
        resolucao.candidates = candidatos
        resolucao.requires_clarification = True
        return resolucao

    # --- 4. referencia contextual ------------------------------------------
    tokens = set(normalize_text(text).split())
    aponta_atual = bool(tokens & _CURRENT_WORDS)
    if aponta_atual or (fato is not None and not consulta):
        ativo = _ativo(state) or _lista_unica(state)
        if ativo is not None:
            resolucao.reference_type = REF_CURRENT
            resolucao.resolved_product = ativo
            resolucao.confidence = 0.85
            return resolucao
        resolucao.reference_type = REF_CURRENT
        resolucao.requires_clarification = True
        return resolucao

    # --- 5. catalogo global -------------------------------------------------
    if consulta:
        resolucao.action = (
            ACTION_SEARCH_PRODUCT
            if resolucao.action == ACTION_SELECT_PRODUCT
            else resolucao.action
        )
        resolucao.requires_catalog_search = True
        resolucao.confidence = 0.6
        return resolucao

    resolucao.requires_clarification = True
    return resolucao
