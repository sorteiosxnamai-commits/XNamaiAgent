"""O XNamai nao guarda credencial da Mercos nem fala com a Mercos.

Estes guards medem CODIGO, nao texto. Um comentario que diz "o token da Mercos
nao mora aqui" e desejavel; o sanitizador de logs PRECISA citar os nomes dos
headers secretos para poder remove-los. Medir por substring puniria os dois e
premiaria quem simplesmente nao documenta. Por isso a analise e por AST:
docstrings e comentarios ficam de fora, literais de codigo entram.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ("app", "api", "scripts")

#: Onde citar o nome do header secreto e a propria defesa: a lista de chaves a
#: remover antes de logar. Exempcao estreita e nominal, nunca por diretorio.
SANITIZER_ALLOWLIST = {"app/commerce/mercos/client.py": {"_SENSITIVE_KEYS"}}


def _runtime_files():
    for base in RUNTIME_DIRS:
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _code_string_literals(path: pathlib.Path) -> list[str]:
    """Literais de string do modulo, SEM docstrings e sem comentarios.

    Tambem ignora os literais que compoem as constantes explicitamente
    permitidas (o sanitizador), identificadas pelo nome da variavel.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    allowed_names = SANITIZER_ALLOWLIST.get(rel, set())

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

    allowed_nodes: set[int] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id in allowed_names for t in targets):
            for sub in ast.walk(node):
                allowed_nodes.add(id(sub))

    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in allowed_nodes
        ):
            found.append(node.value)
    return found


def test_scan_is_not_empty():
    """Sem isto a guarda inteira pode passar a vacuo."""
    files = list(_runtime_files())
    assert len(files) > 50
    assert any(p.name == "client.py" and "mercos" in p.parts for p in files)


def test_no_mercos_credential_lives_in_the_runtime():
    """Nenhum literal de codigo carrega credencial ou URL base da Mercos."""
    forbidden = ("MERCOS_APPLICATION_TOKEN", "MERCOS_COMPANY_TOKEN", "MERCOS_BASE_URL")
    offenders = []
    for path in _runtime_files():
        for literal in _code_string_literals(path):
            hits = [token for token in forbidden if token in literal]
            if hits:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {hits}")
    assert not offenders, f"credencial Mercos no runtime: {offenders}"


def test_no_runtime_code_points_at_mercos_itself():
    """Toda chamada sai para o adaptador; mercos.com nunca vira alvo de codigo."""
    offenders = []
    for path in _runtime_files():
        for literal in _code_string_literals(path):
            if "mercos.com" in literal.casefold():
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}: {literal[:80]}"
                )
    assert not offenders, f"alvo direto da Mercos no runtime: {offenders}"


def test_settings_declare_only_allowed_adaptor_and_gate_variables():
    from app.config import Settings

    fields = set(Settings.model_fields)
    assert {"mercos_adaptor_url", "mercos_adaptor_api_key"} <= fields
    assert not ({"mercos_application_token", "mercos_company_token", "mercos_base_url"} & fields)

    mercos_fields = {name for name in fields if name.startswith("mercos_")}
    assert mercos_fields == {
        "mercos_adaptor_url",
        "mercos_adaptor_api_key",
        "mercos_adaptor_timeout_seconds",
        # Portoes de mutacao. Entram nesta lista por serem BOOLEANOS de
        # operacao, nunca credencial: dizem se este ambiente pode criar, e o
        # default e nao. Credencial Mercos segue proibida aqui — e a assercao
        # acima continua provando isso.
        "mercos_order_mutations_enabled",
        "mercos_customer_mutations_enabled",
    }, f"campo Mercos inesperado: {sorted(mercos_fields)}"

    # O que realmente importa nesta fronteira: nenhum campo novo carrega segredo.
    for nome in ("mercos_order_mutations_enabled", "mercos_customer_mutations_enabled"):
        assert Settings.model_fields[nome].annotation is bool
        assert Settings.model_fields[nome].default is False


def test_env_example_declares_only_allowed_adaptor_and_gate_variables():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and line.strip().upper().startswith("MERCOS")
    }
    assert declared == {
        "MERCOS_ADAPTOR_URL",
        "MERCOS_ADAPTOR_API_KEY",
        "MERCOS_ADAPTOR_TIMEOUT_SECONDS",
        "MERCOS_CUSTOMER_MUTATIONS_ENABLED",
    }, f".env.example declara env Mercos inesperada: {sorted(declared)}"


def test_api_key_is_masked_by_the_secret_validator():
    """A chave interna entra na mesma validacao dos demais segredos."""
    source = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    validator = source[source.index("@field_validator(") :]
    assert '"mercos_adaptor_api_key"' in validator.split(")")[0]


def test_sanitizer_removes_every_known_secret_header():
    from app.commerce.mercos.client import sanitize

    dirty = {
        "ApplicationToken": "a",
        "CompanyToken": "b",
        "X-API-Key": "c",
        "authorization": "d",
        "nested": {"api_key": "e", "ok": 1},
        "rows": [{"token": "f", "id": 2}],
    }
    clean = sanitize(dirty)
    assert clean == {"nested": {"ok": 1}, "rows": [{"id": 2}]}
