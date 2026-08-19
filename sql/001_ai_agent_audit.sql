CREATE TABLE IF NOT EXISTS ai_inbound_messages (
  id bigserial PRIMARY KEY,
  provider text NOT NULL DEFAULT 'brevo',
  event_type text NULL,
  message_id text NULL,
  conversation_id text NULL,
  sender_phone text NULL,
  sender_name text NULL,
  text text NOT NULL DEFAULT '',
  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_sender_phone
ON ai_inbound_messages(sender_phone);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_created_at
ON ai_inbound_messages(created_at DESC);

CREATE TABLE IF NOT EXISTS ai_agent_responses (
  id bigserial PRIMARY KEY,
  inbound_id bigint NULL REFERENCES ai_inbound_messages(id) ON DELETE SET NULL,
  sender_phone text NULL,
  reply_text text NOT NULL,
  intent text NULL,
  handoff_required boolean NOT NULL DEFAULT false,
  safety_reason text NULL,
  provider_send_ok boolean NULL,
  provider_response jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_inbound_id
ON ai_agent_responses(inbound_id);

CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_created_at
ON ai_agent_responses(created_at DESC);
