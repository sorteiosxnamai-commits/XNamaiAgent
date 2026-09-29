import inspect

from app import db
from app.ingress import worker


def test_inbound_inserts_persist_workspace_id():
    insert_source = inspect.getsource(db.insert_inbound_message)
    claim_source = inspect.getsource(db.claim_inbound_message)

    assert "workspace_id" in insert_source
    assert "%(workspace_id)s" in insert_source
    assert "workspace_id" in claim_source
    assert "%(workspace_id)s" in claim_source


def test_agent_response_inherits_inbound_workspace():
    response_source = inspect.getsource(db.insert_agent_response)
    worker_source = inspect.getsource(worker._process_inbox_row_locked)

    assert "%(workspace_id)s" in response_source
    assert '"workspace_id": incoming.workspace_id' in worker_source
