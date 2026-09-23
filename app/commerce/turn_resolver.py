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

*Nunca escolher ao acaso.* Havendo dois carregadores plausiveis, afirmar um
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
ACTION_START_PURCHASE = "start_purchase"
ACTION_TRANSACTION = "transaction"
ACTION_ADD_TO_CART = "add_to_cart"
ACTION_REMOVE_FROM_CART = "remove_from_cart"
ACTION_SET_QUANTITY = "set_quantity"
ACTION_SHOW_CART = "show_cart"
ACTION_CLEAR_CART = "clear_cart"
ACTION_REVIEW_ORDER = "review_order"
ACTION_CONFIRM_ORDER = "confirm_order"
ACTION_CANCEL_ORDER = "cancel_order"

#: Operacoes que acontecem SO na conversa. A fonte comercial nao tem carrinho, e
#: o adaptador serializa chamadas com pausa de dois segundos — mexer no carrinho
#: nao pode custar rede.
LOCAL_CART_ACTIONS = frozenset({
    ACTION_ADD_TO_CART,
    ACTION_REMOVE_FROM_CART,
    ACTION_SET_QUANTITY,
    ACTION_SHOW_CART,
    ACTION_CLEAR_CART,
})

#: Pendencia de confirmacao do pedido.
PENDING_CONFIRM_ORDER = "confirm_order"
ACTION_CONFIRM_PENDING = "confirm_pending"
ACTION_REJECT_PENDING = "reject_pending"

#: Acoes que apenas LEEM fato de produto. Precisam decidir antes do fluxo
#: legado: "qual valor do AD-190?" caia num ramo de link de produto e voltava
#: "a fonte oficial nao informou um link" para uma pergunta de preco.
READ_ONLY_ACTIONS = frozenset({
    ACTION_BROWSE_CATALOG,
    ACTION_SEARCH_PRODUCT,
    ACTION_SELECT_PRODUCT,
    ACTION_GET_DETAILS,
    ACTION_GET_PRICE,
    ACTION_CHECK_INVENTORY,
    ACTION_SHOW_MEDIA,
    ACTION_SHOW_MORE_MEDIA,
    ACTION_COMPARE,
    ACTION_CORRECT_REFERENCE,
    ACTION_REJECT_PRODUCT,
})

#: Tipos de acao pendente aguardando sim/nao.
PENDING_BROWSE = "browse_catalog"

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
    "qual o preco", "qual preco", "qual o valor", "qual valor", "qual valor d",
    "valor desse", "preco desse", "sai quanto", "por quanto", "custa quanto",
    "quanto e", "preco", "valor",
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
    "me fala mais", "me fale mais", "fala mais", "fale mais", "me conta mais",
    "conta mais", "me explica", "explica melhor", "explica", "saber mais",
    "quais detalhes", "detalhes", "quais especificacoes", "especificacoes",
    "como ele e", "como e ele", "informacao dele", "ficha tecnica",
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

_ADD_CART = (
    "coloca no carrinho", "poe no carrinho", "adiciona no carrinho",
    "adicionar ao carrinho", "no carrinho", "adiciona", "adicionar",
    "coloca esse", "poe esse", "inclui", "incluir",
)
_REMOVE_CART = (
    "tira do carrinho", "remove do carrinho", "tira esse", "tira o",
    "remove esse", "remove o", "remover", "retira", "tirar",
)
_SHOW_CART = (
    "meu carrinho", "no carrinho", "como ficou", "como esta o pedido",
    "meu pedido", "o que tem no pedido", "resumo do pedido", "ver o carrinho",
    "o que eu escolhi", "o que escolhi", "quanto deu", "quanto ficou",
)
#: Perguntas que SO podem ser "me mostra o carrinho". Ficam separadas porque
#: "no carrinho" sozinho tambem aparece em "coloca no carrinho".
_SHOW_CART_STRONG = (
    "o que tem no carrinho", "o que tem no meu pedido", "o que ha no carrinho",
    "o que eu escolhi", "o que escolhi", "como ficou", "meu carrinho",
    "resumo do pedido", "quanto deu", "quanto ficou", "ver o carrinho",
    "me mostra meu pedido", "me mostra o carrinho",
)

_CLEAR_CART = (
    "limpa o carrinho", "limpar carrinho", "esvazia", "limpa tudo",
    "cancela tudo", "zera o carrinho",
)
#: Pedir para FECHAR e pedir para VER a revisao, nao para cria-la. A revisao
#: resolve preco, estoque e condicao — e so entao existe algo a confirmar.
_REVIEW_ORDER = (
    "pode fechar", "fecha o pedido", "fechar pedido", "quero fechar",
    "quero finalizar", "finaliza o pedido", "finalizar pedido",
    "vamos finalizar", "vamos fechar", "pode concluir", "quero concluir",
    "concluir pedido", "revisa o pedido", "revisar pedido", "revisar antes",
    "antes de fechar", "prosseguir com o pedido", "prosseguir com a compra",
    "quero prosseguir", "fechar a compra", "finalizar a compra",
)

#: Confirmacao explicita: nao pede para ver, autoriza o que ja foi visto.
_CONFIRM_ORDER = (
    "confirmo", "confirmar", "pode criar", "sim pode criar", "pode mandar",
    "confirma o pedido", "pode fazer o pedido",
    # "sim, pode fechar" e autorizacao explicita, nao pedido de ver.
    "sim pode fechar", "sim, pode fechar", "sim pode",
)
_QUANTITY_SET = ("coloca", "poe", "quero", "muda para", "deixa")
_QUANTITY_ADD = ("mais", "adiciona mais", "acrescenta", "soma")

#: Intencao TRANSACIONAL. "pedido" e "compra" sao operacoes, nunca produtos —
#: buscar por elas no catalogo produzia "nao encontrei esse produto".
_PURCHASE = (
    "fazer um pedido", "fazer pedido", "fechar um pedido", "fechar pedido",
    "quero comprar", "quero pedir", "quero levar", "como faco um pedido",
    "como faco pedido", "como compro", "como faco para comprar",
    "fazer uma compra", "realizar um pedido", "efetuar pedido",
)

# Resposta comum quando uma orientação de compra foi vaga (por exemplo,
# "qual modelo você procura?"). Isso é um pedido de esclarecimento sobre o
# atendimento, não o nome de um produto para consultar no catálogo.
_PURCHASE_GUIDANCE_FOLLOWUP = frozenset({
    "modelo de que",
    "modelo do que",
    "qual modelo",
    "que modelo",
    "tipo de que",
    "produto de que",
})

#: Transacao de verdade: tem fluxo proprio e nao pode ser engolida aqui.
_TRANSACTION = (
    "quero pagar", "forma de pagamento", "formas de pagamento", "finalizar compra",
    "finalizar pedido", "pagamento", "pagar",
    "pix", "boleto", "cartao", "frete", "entrega do pedido",
)

#: Pergunta de disponibilidade SEM produto: e uma vitrine, nao uma consulta de
#: estoque. Perguntar "qual tipo voce quer?" antes de mostrar qualquer coisa
#: custava tres turnos e falhava no meio.
_AVAILABLE_BROWSE = (
    "produto disponivel", "produtos disponiveis", "coisa disponivel",
    "pronta entrega", "em estoque", "tem disponivel", "tem em estoque",
    "voces tem disponivel", "o que tem disponivel", "que tem disponivel",
    "tem produto", "tem produtos", "produtos disponiveis", "disponiveis",
    "tem disponiveis", "disponivel agora", "o que voces tem",
)

_CONFIRM = frozenset({
    "sim", "s", "ss", "ssim", "sim!", "claro", "pode", "pode ser", "quero",
    "manda", "mostra", "mostre", "vamos", "beleza", "blz", "ok", "okay",
    "isso", "positivo", "bora", "aham",
})
_DENY = frozenset({
    "nao", "n", "nop", "nao quero", "agora nao", "deixa", "deixa pra la",
    "nao precisa", "negativo", "depois",
})

#: Normalizacoes seguras de digitacao que aparecem no WhatsApp. Nenhuma
#: biblioteca de fuzzy: so colagem de espaco em palavra curta conhecida.
_TYPOS = {
    "si m": "sim", "na o": "nao", "pre co": "preco", "va lor": "valor",
    "estoq": "estoque", "vlr": "valor", "qto": "quanto", "qnt": "quanto",
}
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
    "pedido", "pedidos", "compra", "compras", "comprar", "pedir", "levar",
    "coisa", "coisas", "agora", "hoje", "ver", "vendem", "vende",
    "tem", "temos", "ter", "tenho", "tinha", "teria",
    # Verbos de carrinho: dizem a OPERACAO, nunca o produto.
    "adiciona", "adicionar", "coloca", "colocar", "poe", "por", "inclui",
    "incluir", "tira", "tirar", "remove", "remover", "retira", "limpa",
    "limpar", "esvazia", "zera", "carrinho", "confirmo", "confirma",
    "fazer", "faco", "como", "entrega", "pronta", "sim", "ok", "claro",
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


def normalize_typos(texto: str) -> str:
    """Colagens de espaco conhecidas do WhatsApp. Nada de fuzzy generico."""
    resultado = texto
    for errado, certo in _TYPOS.items():
        resultado = re.sub(rf"\b{re.escape(errado)}\b", certo, resultado)
    return resultado


def _contem(texto: str, termos) -> bool:
    return any(termo in texto for termo in termos)


def product_query_for(text: str) -> str | None:
    """Os termos de PRODUTO da mensagem, sem o verbo do pedido.

    "quero o carregador tipo c" -> "carregador tipo c". Deixar o verbo dentro da
    consulta e o que fazia a busca lexical nao casar nada: nenhum produto se
    chama "quero carregador".
    """
    normalizado = normalize_typos(normalize_text(text))
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


def trailing_position(text: str, state) -> tuple[int, object] | None:
    """Numero colado ao fim do texto que E posicao da lista — validado.

    "me fale mais sobre o iphone kit carregador lightning7" quer o item 7. Mas
    "quero iphone7" quer o modelo 7, nao o setimo item. A diferenca nao esta no
    numero: esta em se o TEXTO tambem casa o produto daquela posicao.

    Por isso a regra exige as tres coisas juntas — sufixo numerico, posicao
    existente na lista, e forte sobreposicao dos termos com o item de la. Sem a
    terceira, qualquer "s23" ou "phone5" viraria selecao silenciosa do item
    errado, com preco e estoque de outro produto.
    """
    lista = list(getattr(state, "last_presented_products", None) or [])
    if not lista:
        return None
    normalizado = normalize_typos(normalize_text(text))
    casamento = re.search(r"([a-z]+)\s*(\d{1,2})\s*$", normalizado)
    if not casamento:
        return None
    posicao = int(casamento.group(2))
    alvo = next((p for p in lista if p.position == posicao), None)
    if alvo is None:
        return None

    sem_numero = normalizado[: casamento.start(2)]
    termos = set(_product_tokens(sem_numero))
    if not termos:
        return None
    do_nome = set(_product_tokens(normalize_text(alvo.name or "")))
    if not do_nome:
        return None
    cobertura = len([x for x in termos if x in do_nome]) / len(termos)
    if cobertura < 0.66:
        return None
    return posicao, alvo


def cart_quantity(normalizado: str) -> tuple[int, bool] | None:
    """(quantidade, incremental). "coloca 3" troca; "adiciona mais 2" soma.

    Tratar os dois como a mesma coisa faz o carrinho divergir do que o cliente
    pediu — e o erro so aparece no total do pedido.
    """
    numero = re.search(r"\b(\d{1,3})\b", normalizado)
    if not numero:
        return None
    quantidade = int(numero.group(1))
    incremental = bool(re.search(r"\bmais\b|\bacrescent|\bsoma\b", normalizado))
    return quantidade, incremental


def _tem_termo_de_produto(normalizado: str) -> bool:
    """Sobrou alguma palavra que possa nomear produto?"""
    return bool(_product_tokens(normalizado))


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
    #: (quantidade, incremental) quando o turno fala de quantidade.
    cart_quantity: tuple[int, bool] | None = None
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
    """(acao, fato) a partir do texto E do contexto da ultima acao.

    A ordem aqui e a decisao de projeto mais importante do modulo: PRIMEIRO o
    que o cliente quer fazer, so depois sobre qual produto. Invertido, "quero
    fazer um pedido" virava busca por um produto chamado "pedido".
    """
    ultima = getattr(state, "last_commerce_action", None)
    ultimo_fato = getattr(state, "last_requested_fact", None)
    pendente = getattr(state, "pending_commerce_action", None)
    enxuto = normalizado.strip(" ?!.")

    # Uma dúvida sobre a pergunta do atendente não pode virar busca literal por
    # "modelo de que". Retome a orientação de compra e explique as categorias.
    if enxuto in _PURCHASE_GUIDANCE_FOLLOWUP:
        return ACTION_START_PURCHASE, None

    # 1. resposta a uma pergunta que o proprio bot fez. So conta como resposta
    #    quando existe pergunta pendente — "sim" solto nao inventa acao.
    if pendente:
        if enxuto in _CONFIRM:
            return ACTION_CONFIRM_PENDING, None
        if enxuto in _DENY or enxuto.startswith("nao "):
            return ACTION_REJECT_PENDING, None

    # 2. fechar/finalizar/revisar pede a REVISAO. Vem antes de compra e de
    #    transacao porque as mesmas palavras aparecem nos tres vocabularios, e
    #    aqui o sentido e o de ver o pedido montado antes de autorizar.
    revisao_pendente = (
        getattr(state, "order_confirmation_status", None) == "pending"
        and bool(getattr(state, "order_review_version", None))
    )
    if _contem(normalizado, _CONFIRM_ORDER):
        return ACTION_CONFIRM_ORDER, None
    if _contem(normalizado, _REVIEW_ORDER) and (state.cart_items or []):
        # Carrinho vazio nao tem o que revisar: ali "quero fechar um pedido" e
        # intencao de COMECAR, e cai no ramo de compra logo abaixo.
        return (
            ACTION_CONFIRM_ORDER if revisao_pendente else ACTION_REVIEW_ORDER
        ), None

    # 3. transacao real tem fluxo proprio: nao pode ser engolida aqui.
    if _contem(normalizado, _TRANSACTION):
        return ACTION_TRANSACTION, None

    # 3. intencao de comprar sem produto: operacao, nunca nome de produto.
    if _contem(normalizado, _PURCHASE):
        return ACTION_START_PURCHASE, None

    # 3.5 carrinho: operacoes locais, antes de qualquer consulta de fato.
    if _contem(normalizado, _CLEAR_CART):
        return ACTION_CLEAR_CART, None
    if _contem(normalizado, _CONFIRM_ORDER):
        return ACTION_CONFIRM_ORDER, None
    if _contem(normalizado, _REMOVE_CART):
        return ACTION_REMOVE_FROM_CART, None
    if _contem(normalizado, _SHOW_CART_STRONG):
        return ACTION_SHOW_CART, None
    if _contem(normalizado, _SHOW_CART) and not _contem(normalizado, _ADD_CART):
        return ACTION_SHOW_CART, None
    quantidade = cart_quantity(normalizado)
    if quantidade is not None and (
        _contem(normalizado, _QUANTITY_ADD)
        or any(re.search(rf"\b{v}\b", normalizado) for v in _QUANTITY_SET)
    ):
        return ACTION_SET_QUANTITY, None
    if _contem(normalizado, _ADD_CART):
        return ACTION_ADD_TO_CART, None

    # 4. disponibilidade sem produto e vitrine, nao consulta de estoque. Com
    #    produto ativo a mesma frase e sobre ELE: "tem estoque?" logo depois de
    #    escolher um item pergunta daquele item, nao pede uma vitrine nova.
    if (
        _contem(normalizado, _AVAILABLE_BROWSE)
        and not _tem_termo_de_produto(normalizado)
        and getattr(state, "active_product", None) is None
    ):
        return ACTION_BROWSE_CATALOG, None

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
    normalizado = normalize_typos(normalize_text(text))
    consulta = product_query_for(text)
    referencia, ean = _referencia_explicita(text)
    acao, fato = _detectar_acao(normalizado, state)

    resolucao = CommerceTurnResolution(
        action=acao,
        product_query=consulta,
        explicit_reference=referencia,
        explicit_ean=ean,
        requested_fact=fato,
        cart_quantity=cart_quantity(normalizado),
    )

    # Resposta a pergunta pendente e transacao ja estao decididas: nao passam
    # pelo bloco de rejeicao abaixo, que interpretaria "nao" como recusa de
    # produto em vez de resposta a pergunta do bot.
    if acao in {ACTION_CONFIRM_PENDING, ACTION_REJECT_PENDING, ACTION_TRANSACTION,
                ACTION_START_PURCHASE}:
        return resolucao

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

    # --- 0. posicao colada ao texto, validada pelo contexto ----------------
    colada = trailing_position(text, state)
    if colada is not None:
        posicao_colada, alvo_colado = colada
        resolucao.reference_type = REF_LIST_POSITION
        resolucao.reference_position = posicao_colada
        resolucao.resolved_product = alvo_colado
        resolucao.confidence = 0.85
        return resolucao

    # --- 1. referencia explicita -------------------------------------------
    if referencia or ean:
        resolucao.reference_type = REF_EXPLICIT
        resolucao.requires_catalog_search = True
        resolucao.confidence = 0.95
        return resolucao

    # --- 2. posicao na lista ------------------------------------------------
    posicao = _posicao_pedida(normalizado)
    # Em turno de quantidade o numero E a quantidade: "adiciona mais 2" pede
    # duas unidades do produto atual, nao o segundo item da lista.
    if acao == ACTION_SET_QUANTITY:
        posicao = None
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
