from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import customer_identity, db
from app.memory_scope import restore_state, scoped_key, stamp_state, trusted_workspace
from app.models import IncomingMessage

X = "aa774d20-509f-4d54-865b-7a5de22b6d30"
Y = "bb774d20-509f-4d54-865b-7a5de22b6d30"
PHONE = "5511900000000"


class MemoryDB:
    """Fake persistence with explicit tenant and key filtering, not model stubs."""
    def __init__(self, monkeypatch, rows=None, workspace=X):
        self.rows = rows or []
        self.calls = []
        self.last_query = ""
        self.last_params = {}
        monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(
            database_url="postgresql://test", chatbo_workspace_id=workspace,
            auto_create_tables=False,
        ))
        monkeypatch.setattr(db, "ensure_tables", lambda: None)
        monkeypatch.setattr(db, "get_conn", self.connect)

    @contextmanager
    def connect(self):
        yield self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params):
        self.last_query, self.last_params = query, params
        self.calls.append((query, params))

    def fetchone(self):
        return {"id": 101}

    def fetchall(self):
        query, params = self.last_query, self.last_params
        if "SELECT" not in query:
            return []
        assert "workspace_id = %(workspace_id)s::uuid" in query
        rows = [r for r in self.rows if r.get("workspace_id") == params["workspace_id"]]
        if "ai_customer_commerce_sessions" in query:
            return [r for r in rows if r.get("person_key") in params["keys"]]
        if "ai_customer_identity_links" in query:
            if "WITH seed" in query:
                persons = {r.get("person_key") for r in rows if r.get("identity_value") in params["values"]}
                return [r for r in rows if r.get("person_key") in persons]
            return [r for r in rows if r.get("identity_value") in params["values"]]
        return [r for r in rows if r.get("id") is not None]


@pytest.mark.parametrize("configured,supplied,expected", [
    (X, None, X), (X, X, X), (X, Y, None), ("", X, None),
    ("invalid", None, None), (X, "invalid", None),
])
def test_workspace_only_comes_from_server_config(configured, supplied, expected):
    assert trusted_workspace(SimpleNamespace(chatbo_workspace_id=configured), supplied) == expected


def test_unscoped_and_wrong_workspace_snapshots_are_rejected():
    old = {"order_id": "newstore-123", "active_product": {"name": "Tissot"}}
    assert restore_state(old, X) == {}
    assert restore_state(stamp_state(old, Y), X) == {}
    fresh = {"active_topic": "registration", "pending_followup": {"question": "CPF ou CNPJ?"}}
    assert restore_state(stamp_state(fresh, X), X) == fresh


def test_sessions_same_phone_do_not_adopt_legacy_or_other_workspace(monkeypatch):
    old = {"order_id": "newstore-123"}
    current = {"active_topic": "registration"}
    memory = MemoryDB(monkeypatch, [
        {"workspace_id": None, "person_key": f"phone:{PHONE}", "commerce_state": old},
        {"workspace_id": Y, "person_key": scoped_key(Y, f"phone:{PHONE}"), "commerce_state": stamp_state(old, Y)},
        {"workspace_id": X, "person_key": scoped_key(X, f"phone:{PHONE}"), "commerce_state": stamp_state(current, X)},
    ])
    assert db.load_customer_commerce_sessions([f"phone:{PHONE}"]) == [current]
    assert db.load_customer_commerce_sessions([scoped_key(Y, f"phone:{PHONE}")]) == []
    assert len(memory.calls) == 1


def test_session_in_correct_workspace_still_needs_current_provenance(monkeypatch):
    MemoryDB(monkeypatch, [{"workspace_id": X, "person_key": scoped_key(X, f"phone:{PHONE}"),
                            "commerce_state": {"order_id": "old-contaminated"}}])
    assert db.load_customer_commerce_sessions([f"phone:{PHONE}"]) == []


def test_session_write_namespaces_aliases_and_never_merges_legacy(monkeypatch):
    memory = MemoryDB(monkeypatch, [{"workspace_id": None, "person_key": f"phone:{PHONE}",
                                    "commerce_state": {"order_id": "old-contaminated", "resumable_score": 100}}])
    fresh = {"active_topic": "registration", "conversation_goal": "comprar com CPF"}
    db.persist_customer_commerce_session(person_keys=[f"phone:{PHONE}"], commerce_state=fresh)
    writes = [(q, p) for q, p in memory.calls if "INSERT INTO" in q]
    assert len(writes) == 1
    query, params = writes[0]
    assert params["person_key"] == scoped_key(X, f"phone:{PHONE}")
    assert params["workspace_id"] == X
    stored = params["commerce_state"].obj
    assert stored["_memory_scope"]["workspace_id"] == X
    assert not stored.get("order_id")
    assert "WHERE public.ai_customer_commerce_sessions.workspace_id = EXCLUDED.workspace_id" in query


@pytest.mark.parametrize("workspace,supplied", [("", None), (X, Y)])
def test_missing_or_conflicting_scope_never_queries_memory(monkeypatch, workspace, supplied):
    memory = MemoryDB(monkeypatch, workspace=workspace)
    lookup = dict(conversation_id="same-thread", sender_phone=PHONE, before_inbound_id=None, workspace_id=supplied)
    assert db.load_recent_conversation_turns(**lookup) == []
    assert db.load_commerce_conversation_state(**lookup) == {}
    assert db.load_customer_commerce_sessions([f"phone:{PHONE}"], workspace_id=supplied) == []
    db.persist_customer_commerce_session(person_keys=[f"phone:{PHONE}"], commerce_state={"order_id": "x"}, workspace_id=supplied)
    assert customer_identity.resolve_person_key_candidates(sender_phone=PHONE, workspace_id=supplied) == []
    assert customer_identity.resolve_linked_identity_candidates(sender_key=None, sender_phone=PHONE, workspace_id=supplied) == []
    customer_identity.upsert_customer_identity_links(IncomingMessage(text="cpf", workspace_id=supplied, sender_phone=PHONE), {})
    assert memory.calls == []


def test_history_uses_workspace_for_inbound_and_response_and_marks_legacy(monkeypatch):
    rows = [
        {"id": 1, "workspace_id": None, "text": "relógio", "reply_text": "Tissot NewStore"},
        {"id": 2, "workspace_id": Y, "text": "outro", "reply_text": "Outra empresa"},
        {"id": 3, "workspace_id": X, "text": "como compro", "reply_text": "CPF ou CNPJ?"},
        {"id": 4, "workspace_id": X, "text": "cpf", "reply_text": "Cadastro com CPF",
         "provider_response": {"_agent_context": {"commerce_state": stamp_state({"active_topic": "registration"}, X)}}},
    ]
    memory = MemoryDB(monkeypatch, rows)
    monkeypatch.setattr(customer_identity, "resolve_linked_identity_candidates", lambda **_: [])
    history = db.load_recent_conversation_turns(conversation_id="same", sender_phone=PHONE, before_inbound_id=50)
    assert [x["content"] for x in history] == ["como compro", "CPF ou CNPJ?", "cpf", "Cadastro com CPF"]
    assert history[1]["metadata"]["memory_scope_trusted"] is False
    assert history[3]["metadata"]["memory_scope_trusted"] is True
    assert all("inbound.workspace_id = %(workspace_id)s::uuid" in q and
               "response.workspace_id = %(workspace_id)s::uuid" in q for q, _ in memory.calls)


def test_response_snapshots_reject_pre_fix_contamination_even_with_tenant_column(monkeypatch):
    old = {"order_id": "newstore-order", "active_product": {"name": "Tissot"}}
    clean = {"active_topic": "registration", "conversation_goal": "comprar com CPF"}
    MemoryDB(monkeypatch, [
        {"id": 1, "workspace_id": X, "provider_response": {"_agent_context": {"commerce_state": old}}},
        {"id": 2, "workspace_id": Y, "provider_response": {"_agent_context": {"commerce_state": stamp_state(old, Y)}}},
        {"id": 3, "workspace_id": X, "provider_response": {"_agent_context": {"commerce_state": stamp_state(clean, X)}}},
    ])
    monkeypatch.setattr(customer_identity, "resolve_linked_identity_candidates", lambda **_: [])
    assert db.load_commerce_conversation_state(conversation_id="same", sender_phone=PHONE, before_inbound_id=50) == clean


def test_linked_identity_seed_and_lookup_are_both_scoped(monkeypatch):
    memory = MemoryDB(monkeypatch, [
        {"workspace_id": None, "person_key": "cpf:global", "identity_value": PHONE, "identity_type": "phone"},
        {"workspace_id": Y, "person_key": scoped_key(Y, "cpf:other"), "identity_value": scoped_key(Y, PHONE), "identity_type": "phone"},
        {"workspace_id": X, "person_key": scoped_key(X, "cpf:current"), "identity_value": scoped_key(X, PHONE), "identity_type": "phone"},
    ])
    keys = customer_identity.resolve_person_key_candidates(sender_phone=PHONE)
    assert keys == [scoped_key(X, f"phone:{PHONE}"), scoped_key(X, "cpf:current")]
    customer_identity.resolve_linked_identity_candidates(sender_key=None, sender_phone=PHONE)
    query, params = memory.calls[-1]
    assert query.count("workspace_id = %(workspace_id)s::uuid") == 2
    assert params["values"] == [scoped_key(X, PHONE)]


def test_identity_writes_do_not_reassign_existing_global_alias(monkeypatch):
    memory = MemoryDB(monkeypatch, [{"workspace_id": None, "person_key": "cpf:legacy",
                                    "identity_value": PHONE, "identity_type": "phone"}])
    customer_identity.upsert_customer_identity_links(IncomingMessage(text="cpf", sender_phone=PHONE, sender_key=f"whatsapp:{PHONE}"), {})
    writes = [(q, p) for q, p in memory.calls if "INSERT INTO" in q]
    assert len(writes) == 2
    assert all(p["workspace_id"] == X and p["identity_value"].startswith(f"workspace:{X}:v1:") for _, p in writes)
    assert all(p["person_key"] == scoped_key(X, f"phone:{PHONE}") for _, p in writes)
    assert not any("UPDATE public.ai_customer_identity_links" in q for q, _ in memory.calls)


def test_valid_cross_channel_aliases_still_resume_inside_one_workspace(monkeypatch):
    person = scoped_key(X, f"phone:{PHONE}")
    MemoryDB(monkeypatch, [
        {"workspace_id": X, "person_key": person, "identity_type": "phone", "identity_value": scoped_key(X, PHONE)},
        {"workspace_id": X, "person_key": person, "identity_type": "sender_key", "identity_value": scoped_key(X, "instagram:current")},
        {"workspace_id": Y, "person_key": person, "identity_type": "sender_key", "identity_value": scoped_key(Y, "instagram:other")},
        {"workspace_id": None, "person_key": person, "identity_type": "sender_key", "identity_value": "instagram:legacy"},
    ])
    assert customer_identity.resolve_linked_identity_candidates(sender_key=None, sender_phone=PHONE) == [
        ("instagram:current", None),
    ]


def test_alias_merge_only_updates_current_workspace(monkeypatch):
    person = scoped_key(X, f"phone:{PHONE}")
    memory = MemoryDB(monkeypatch, [{"workspace_id": X, "person_key": person,
                                    "identity_type": "phone", "identity_value": scoped_key(X, PHONE)}])
    customer_identity.upsert_customer_identity_links(
        IncomingMessage(text="cadastro", sender_phone=PHONE),
        {"checkout_draft": {"customer": {"cpf": "12345678909"}}},
    )
    updates = [(q, p) for q, p in memory.calls if "UPDATE public.ai_customer_identity_links" in q]
    assert len(updates) == 1
    query, params = updates[0]
    assert "AND workspace_id = %(workspace_id)s::uuid" in query
    assert params["preferred"] == scoped_key(X, "cpf:12345678909")
    assert all(key.startswith(f"workspace:{X}:v1:") for key in params["keys"])


def test_new_response_write_defaults_to_server_scope_and_rejects_conflict(monkeypatch):
    memory = MemoryDB(monkeypatch)
    # Only a current, internally stamped state is eligible for future recovery.
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql://test", chatbo_workspace_id=X, agent_persona_tenant_id="xnamai"))
    assert db.insert_agent_response({"reply_text": "CPF ou CNPJ?"}) == 101
    assert memory.calls[-1][1]["workspace_id"] == X
    assert db.insert_agent_response({"workspace_id": Y, "reply_text": "must not save"}) is None
    assert len(memory.calls) == 1


def test_inbound_write_derives_workspace_and_rejects_other_company(monkeypatch):
    memory = MemoryDB(monkeypatch)
    assert db.insert_inbound_message({"text": "cpf"}) == 101
    assert memory.calls[-1][1]["workspace_id"] == X
    assert db.insert_inbound_message({"text": "cpf", "workspace_id": Y}) is None
    assert db.claim_inbound_message({"text": "cpf", "workspace_id": Y, "message_id": "other"}) == (False, None)
    assert len(memory.calls) == 1


def test_later_message_from_another_workspace_does_not_suppress_this_reply(monkeypatch):
    memory = MemoryDB(monkeypatch)
    later = [{"id": 102, "workspace_id": Y}, {"id": 103, "workspace_id": None}]
    monkeypatch.setattr(memory, "fetchone", lambda: next((r for r in later if
        r["workspace_id"] == memory.last_params["workspace_id"]), None))
    assert db.is_latest_inbound_message(101, "same-thread", None, PHONE) is True
    assert "inbound.workspace_id = %(workspace_id)s::uuid" in memory.last_query
    later.append({"id": 104, "workspace_id": X})
    assert db.is_latest_inbound_message(101, "same-thread", None, PHONE) is False


def test_recent_image_stays_inside_workspace_and_supports_dict_rows(monkeypatch):
    from app import config, inbound_coalesce
    memory = MemoryDB(monkeypatch)
    monkeypatch.setattr(config, "get_settings", db.get_settings)
    monkeypatch.setattr(inbound_coalesce, "get_conn", memory.connect)
    rows = [{"id": 10, "workspace_id": Y, "text": "mesma legenda", "channel_metadata": {"image_url": "https://other.example/image.jpg"}}]
    monkeypatch.setattr(memory, "fetchone", lambda: next((r for r in rows if
        r["workspace_id"] == memory.last_params["workspace_id"]), None))
    lookup = dict(conversation_id="same-thread", sender_key=None, sender_phone=PHONE)
    assert inbound_coalesce.recent_image_inbound_for_echo(**lookup) is None
    assert "inbound.workspace_id = %(workspace_id)s::uuid" in memory.last_query
    rows.append({"id": 11, "workspace_id": X, "text": "mesma legenda", "channel_metadata": {"image_url": "https://current.example/image.jpg"}})
    assert inbound_coalesce.recent_image_inbound_for_echo(**lookup)["id"] == 11
    before = len(memory.calls)
    assert inbound_coalesce.recent_image_inbound_for_echo(**lookup, workspace_id=Y) is None
    assert len(memory.calls) == before


def test_suppression_helpers_without_workspace_do_not_query_global_history(monkeypatch):
    from app import config, inbound_coalesce
    memory = MemoryDB(monkeypatch, workspace="")
    monkeypatch.setattr(config, "get_settings", db.get_settings)
    monkeypatch.setattr(inbound_coalesce, "get_conn", memory.connect)
    assert db.is_latest_inbound_message(1, "same", None, PHONE) is True
    assert inbound_coalesce.recent_image_inbound_for_echo(conversation_id="same", sender_key=None) is None
    assert memory.calls == []
