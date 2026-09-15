"""O contrato da conexao central do agente.

O bug que originou estes testes derrubou o full refresh em producao:

    psycopg.errors.DuplicatePreparedStatement:
        prepared statement "_pg3_0" already exists     (SQLSTATE 42P05)

`DATABASE_URL` aponta para o pooler de TRANSACAO da Supabase (6543), onde a
conexao logica nao e a fisica: entre uma transacao e outra o backend pode ser
outro, ou pode ser um que ja tem `_pg3_0` preparado por outra sessao. O psycopg3
prepara automaticamente qualquer statement repetido cinco vezes, e a colisao
vira erro.

O que tornava isso dificil de ver: a falha e INTERMITENTE (depende de qual
backend o pooler entrega) e, ate a correcao do `strict`, o writer do indice
engolia a excecao e devolvia `0`. O sync anunciava sucesso com paginas que nunca
foram gravadas.

A suite e hermetica: nenhum teste aqui abre conexao real. O que se verifica e
que o parametro sai correto da fabrica de conexao — a incompatibilidade com o
pooler ja foi validada manualmente contra o banco real.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def conexao_espiada(monkeypatch):
    """Captura os kwargs de `psycopg.connect` sem tocar em banco."""
    import app.db as db
    from app.config import get_settings

    registro: dict[str, object] = {}

    class _Conexao:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closes += 1

    conexao = _Conexao()

    def falso_connect(dsn, **kwargs):
        registro["dsn"] = dsn
        registro.update(kwargs)
        return conexao

    monkeypatch.setenv("DATABASE_URL", "postgresql://exemplo/db")
    get_settings.cache_clear()
    monkeypatch.setattr(db.psycopg, "connect", falso_connect)
    monkeypatch.setattr(db, "register_database_call", lambda: None)
    yield registro, conexao
    get_settings.cache_clear()


# === o parametro que resolve o 42P05 ======================================


def test_auto_prepare_is_disabled(conexao_espiada):
    """A linha inteira da correcao: sem isto o pooler devolve 42P05."""
    from app.db import get_conn

    registro, _ = conexao_espiada
    with get_conn():
        pass
    assert "prepare_threshold" in registro, "auto-prepare voltou ao default"
    assert registro["prepare_threshold"] is None


def test_the_rest_of_the_connection_contract_is_unchanged(conexao_espiada):
    from psycopg.rows import dict_row

    from app.db import get_conn

    registro, _ = conexao_espiada
    with get_conn():
        pass
    assert registro["row_factory"] is dict_row
    assert registro["connect_timeout"] == 10
    assert registro["dsn"] == "postgresql://exemplo/db"


def test_the_pooler_url_is_not_rewritten(conexao_espiada):
    """A correcao e no cliente: porta e pooler seguem como estao."""
    registro, _ = conexao_espiada
    from app.db import get_conn

    with get_conn():
        pass
    assert "6543" not in str(registro["dsn"]) or "5432" not in str(registro["dsn"])
    assert registro["dsn"] == "postgresql://exemplo/db"


# === o ciclo de vida nao mudou ============================================


def test_success_commits_and_closes(conexao_espiada):
    from app.db import get_conn

    _, conexao = conexao_espiada
    with get_conn() as conn:
        assert conn is conexao
    assert conexao.commits == 1
    assert conexao.rollbacks == 0
    assert conexao.closes == 1


def test_an_exception_rolls_back_and_closes(conexao_espiada):
    from app.db import get_conn

    _, conexao = conexao_espiada
    with pytest.raises(RuntimeError):
        with get_conn():
            raise RuntimeError("falha no meio da transacao")
    assert conexao.commits == 0
    assert conexao.rollbacks == 1
    assert conexao.closes == 1


def test_the_original_exception_is_not_swallowed(conexao_espiada):
    """Rollback nao pode mascarar a causa — foi assim que o bug se escondeu."""
    from app.db import get_conn

    class _Especifica(Exception):
        pass

    with pytest.raises(_Especifica):
        with get_conn():
            raise _Especifica("motivo real")


def test_without_a_database_url_it_refuses_instead_of_connecting(monkeypatch):
    import app.db as db
    from app.config import get_settings

    chamou = []
    monkeypatch.setattr(db.psycopg, "connect", lambda *a, **k: chamou.append(1))
    monkeypatch.setattr(db, "register_database_call", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            with db.get_conn():
                pass
        assert chamou == []
    finally:
        get_settings.cache_clear()


# === a conexao central continua central ===================================


def test_app_db_is_the_only_pooled_connection_factory():
    """Guarda de arquitetura: quem abrir conexao por fora escapa da correcao.

    `conversation_lock` e a excecao conhecida e deliberada: ele usa
    `pg_advisory_lock`, que precisa de conexao propria e dedicada. Executa dois
    statements distintos por conexao, nunca cinco vezes o mesmo, entao nao
    dispara o auto-prepare.
    """
    import pathlib
    import re

    raiz = pathlib.Path(__file__).resolve().parents[1]
    permitidos = {"app/db.py", "app/conversation_lock.py"}
    padrao = re.compile(r"psycopg\.connect\(|Connection\.connect\(")

    ofensores = []
    for caminho in sorted((raiz / "app").rglob("*.py")) + sorted((raiz / "api").rglob("*.py")):
        if "__pycache__" in caminho.parts:
            continue
        relativo = caminho.relative_to(raiz).as_posix()
        if relativo in permitidos:
            continue
        if padrao.search(caminho.read_text(encoding="utf-8")):
            ofensores.append(relativo)

    assert not ofensores, f"conexao aberta fora de app/db.py: {ofensores}"


def test_the_index_writer_goes_through_the_central_connection():
    """O caminho que falhou em producao usa `get_conn`, logo herda a correcao."""
    import inspect

    from app.catalog_index import upsert_canonical_items

    assert "get_conn" in inspect.getsource(upsert_canonical_items)
