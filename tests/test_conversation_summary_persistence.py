from contextlib import contextmanager

from app import conversation_summary_repository as repository
from app.memory_models import ConversationSummaryDelta


def capture_database(monkeypatch, existing):
    calls = []
    monkeypatch.setattr(repository, "get_conversation_summary", lambda **_: existing)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return self

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return {"id": 10, "version": 2}

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(repository, "get_conn", connect)
    return calls


def test_empty_question_snapshot_is_written_as_empty_json(monkeypatch):
    calls = capture_database(monkeypatch, {"current_goal": "comparar modelos", "open_questions": ["Qual cor?"],
                                            "resolved_points": [], "last_inbound_id": 5})
    repository.apply_summary_delta(tenant_id="xnamai", conversation_key="summary:v2:scoped",
        delta=ConversationSummaryDelta(), replace_open_questions=True, inbound_id=6)
    query, params = calls[0]
    assert "WHERE tenant_id = %s" in query and "AND conversation_key = %s" in query
    assert params[3].obj == []
    assert params[-2:] == ("xnamai", "summary:v2:scoped")
    assert params[7] == 6


def test_replayed_or_older_inbound_never_overwrites_newer_summary(monkeypatch):
    previous = {"current_goal": "Bluetooth", "last_inbound_id": 9}
    calls = capture_database(monkeypatch, previous)
    for inbound_id in (8, 9):
        row = repository.apply_summary_delta(tenant_id="xnamai", conversation_key="summary:v2:scoped",
            delta=ConversationSummaryDelta(current_goal="com fio"), inbound_id=inbound_id)
        assert row == previous
    assert calls == []


def test_identical_summary_is_not_written_again(monkeypatch):
    delta = ConversationSummaryDelta(current_goal="revenda", open_questions=["Qual categoria?"])
    existing = repository.merge_summary_state(None, delta, replace_open_questions=True)
    calls = capture_database(monkeypatch, existing)
    assert repository.apply_summary_delta(tenant_id="xnamai", conversation_key="summary:v2:scoped",
        delta=delta, replace_open_questions=True) == existing
    assert calls == []


def test_new_summary_uses_scoped_key_and_correct_final_questions(monkeypatch):
    calls = capture_database(monkeypatch, None)
    repository.apply_summary_delta(tenant_id="xnamai", conversation_key="summary:v2:workspace-channel",
        delta=ConversationSummaryDelta(current_goal="abastecer loja", open_questions=["Fones ou caixas?"]),
        inbound_id=10, response_id=11, replace_open_questions=True)
    query, params = calls[0]
    assert "INSERT INTO public.ai_conversation_summaries" in query
    assert params[:2] == ("xnamai", "summary:v2:workspace-channel")
    assert params[5].obj == ["Fones ou caixas?"]
    assert params[9:11] == (10, 11)


def test_repeated_key_inside_delta_keeps_only_latest_preference():
    state = repository.merge_summary_state(None, ConversationSummaryDelta(
        resolved_points=["preference:color=azul", "preference:color=preto"]))
    assert state["resolved_points"] == ["preference:color=preto"]
