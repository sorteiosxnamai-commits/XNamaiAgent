from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .config import get_settings, resolved_sorteio_database_url
from .runtime_context import register_database_call


def to_jsonb(value: Any, default: Any = None) -> Jsonb:
    """Convert Python dict/list/value to psycopg Jsonb wrapper."""
    if value is None:
        value = {} if default is None else default
    return Jsonb(value)


def get_returning_id(row: Any) -> int | None:
    if not row:
        return None

    if isinstance(row, dict):
        return int(row["id"])

    return int(row[0])


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Agent-dedicated Postgres (ai_* tables and agent operational state)."""
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    register_database_call()
    conn = psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_sorteio_conn() -> Iterator[psycopg.Connection]:
    """Sorteio/raffle Postgres (users, draws, payments). Falls back to DATABASE_URL."""
    url = resolved_sorteio_database_url()
    if not url:
        raise RuntimeError("SORTEIO_DATABASE_URL (or DATABASE_URL fallback) is not configured")
    register_database_call()
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_tables() -> None:
    settings = get_settings()
    if not settings.database_url or not getattr(settings, "auto_create_tables", False):
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Dedicated agent DB must not require sorteio public.users.
            cur.execute(
                """
                ALTER TABLE IF EXISTS public.ai_user_preferences
                  DROP CONSTRAINT IF EXISTS ai_user_preferences_user_id_fkey
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.ai_inbound_messages (
                  id bigserial PRIMARY KEY,
                  provider text NOT NULL DEFAULT 'brevo',
                  event_type text NULL,
                  message_id text NULL,
                  conversation_id text NULL,
                  channel text NOT NULL DEFAULT 'unknown',
                  sender_key text NULL,
                  sender_external_id text NULL,
                  visitor_id text NULL,
                  sender_username text NULL,
                  source_channel_ref text NULL,
                  source_channel_link text NULL,
                  source_conversation_ref text NULL,
                  sender_phone text NULL,
                  sender_name text NULL,
                  text text NOT NULL DEFAULT '',
                  channel_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                  raw jsonb NOT NULL DEFAULT '{}'::jsonb,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                ALTER TABLE public.ai_inbound_messages
                  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
                  ADD COLUMN IF NOT EXISTS sender_key text,
                  ADD COLUMN IF NOT EXISTS sender_external_id text,
                  ADD COLUMN IF NOT EXISTS visitor_id text,
                  ADD COLUMN IF NOT EXISTS sender_username text,
                  ADD COLUMN IF NOT EXISTS source_channel_ref text,
                  ADD COLUMN IF NOT EXISTS source_channel_link text,
                  ADD COLUMN IF NOT EXISTS source_conversation_ref text,
                  ADD COLUMN IF NOT EXISTS channel_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_sender_phone
                ON public.ai_inbound_messages(sender_phone);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_messages_created_at
                ON public.ai_inbound_messages(created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_sender_key_created_at
                ON public.ai_inbound_messages(sender_key, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_channel_created_at
                ON public.ai_inbound_messages(channel, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_visitor_id
                ON public.ai_inbound_messages(visitor_id);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_source_conversation_ref
                ON public.ai_inbound_messages(channel, source_conversation_ref);

                CREATE INDEX IF NOT EXISTS idx_ai_inbound_conversation_created_at
                ON public.ai_inbound_messages(conversation_id, created_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_inbound_provider_message_id
                ON public.ai_inbound_messages(provider, message_id)
                WHERE message_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS public.ai_agent_responses (
                  id bigserial PRIMARY KEY,
                  inbound_id bigint NULL REFERENCES public.ai_inbound_messages(id) ON DELETE SET NULL,
                  channel text NOT NULL DEFAULT 'unknown',
                  sender_key text NULL,
                  sender_phone text NULL,
                  reply_text text NOT NULL,
                  intent text NULL,
                  handoff_required boolean NOT NULL DEFAULT false,
                  safety_reason text NULL,
                  provider_send_ok boolean NOT NULL DEFAULT false,
                  provider_response jsonb NULL,
                  created_at timestamptz NOT NULL DEFAULT now()
                );

                ALTER TABLE public.ai_agent_responses
                  ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
                  ADD COLUMN IF NOT EXISTS sender_key text;

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_inbound_id
                ON public.ai_agent_responses(inbound_id);

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_created_at
                ON public.ai_agent_responses(created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_sender_key_created_at
                ON public.ai_agent_responses(sender_key, created_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_customer_identity_links (
                  id bigserial PRIMARY KEY,
                  person_key text NOT NULL,
                  identity_type text NOT NULL,
                  identity_value text NOT NULL,
                  channel text NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  CONSTRAINT uq_ai_customer_identity_type_value
                    UNIQUE (identity_type, identity_value)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_person_key
                ON public.ai_customer_identity_links(person_key);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_value
                ON public.ai_customer_identity_links(identity_type, identity_value);

                CREATE TABLE IF NOT EXISTS public.ai_customer_commerce_sessions (
                  person_key text PRIMARY KEY,
                  commerce_state jsonb NOT NULL DEFAULT '{}'::jsonb,
                  channel text NULL,
                  conversation_id text NULL,
                  sender_key text NULL,
                  sender_phone text NULL,
                  resumable_score integer NOT NULL DEFAULT 0,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_updated_at
                ON public.ai_customer_commerce_sessions(updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_key
                ON public.ai_customer_commerce_sessions(sender_key);

                CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_phone
                ON public.ai_customer_commerce_sessions(sender_phone);

                CREATE TABLE IF NOT EXISTS public.ai_pix_payments (
                  id bigserial PRIMARY KEY,
                  mp_payment_id text NOT NULL,
                  status text NOT NULL DEFAULT 'pending',
                  amount_cents integer NOT NULL,
                  currency text NOT NULL DEFAULT 'BRL',
                  description text,
                  payer_email text,
                  qr_code text,
                  qr_code_base64 text,
                  external_reference text,
                  date_of_expiration timestamptz,
                  expires_at timestamptz,
                  conversation_id text,
                  sender_key text,
                  sender_phone text,
                  channel text,
                  cart_session_id text,
                  checkout_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
                  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                  paid_at timestamptz,
                  settlement_status text NOT NULL DEFAULT 'none'
                    CHECK (
                      settlement_status IN (
                        'none', 'pending', 'processing',
                        'completed', 'failed', 'skipped'
                      )
                    ),
                  tray_order_id text,
                  settled_at timestamptz,
                  settlement_error text,
                  last_webhook_at timestamptz,
                  raw_create jsonb NOT NULL DEFAULT '{}'::jsonb,
                  raw_last_status jsonb NOT NULL DEFAULT '{}'::jsonb,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  CONSTRAINT uq_ai_pix_payments_mp_payment_id UNIQUE (mp_payment_id)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_status_created
                ON public.ai_pix_payments(status, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_sender_key
                ON public.ai_pix_payments(sender_key);

                CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_settlement
                ON public.ai_pix_payments(settlement_status, status)
                WHERE status = 'approved';

                CREATE TABLE IF NOT EXISTS public.ai_agent_persona_versions (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    persona_key text NOT NULL,
                    version integer NOT NULL,
                    name text NOT NULL,
                    source text NOT NULL DEFAULT 'user'
                        CHECK (source IN ('user', 'migration', 'system')),
                    instructions text NOT NULL,
                    instructions_hash text NOT NULL,
                    status text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'active', 'archived')),
                    created_by text,
                    activated_by text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    activated_at timestamptz,
                    archived_at timestamptz,
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                    UNIQUE (tenant_id, persona_key, version)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_agent_persona_active
                ON public.ai_agent_persona_versions(tenant_id, persona_key)
                WHERE status = 'active';

                CREATE INDEX IF NOT EXISTS idx_ai_agent_persona_versions_lookup
                ON public.ai_agent_persona_versions(
                    tenant_id, persona_key, status, version DESC
                );

                CREATE TABLE IF NOT EXISTS public.ai_prompt_compilations (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    conversation_key text,
                    sender_key text,
                    inbound_id bigint,
                    response_id bigint,
                    persona_version_id bigint,
                    instruction_extension_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                    contact_memory_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                    compiled_instructions_hash text NOT NULL,
                    instructions_char_count integer NOT NULL DEFAULT 0,
                    input_char_count integer NOT NULL DEFAULT 0,
                    approximate_input_tokens integer NOT NULL DEFAULT 0,
                    channel text,
                    openai_api_mode text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE INDEX IF NOT EXISTS idx_ai_prompt_compilations_inbound
                ON public.ai_prompt_compilations(inbound_id);

                CREATE INDEX IF NOT EXISTS idx_ai_prompt_compilations_conversation
                ON public.ai_prompt_compilations(
                    tenant_id, conversation_key, created_at DESC
                );

                CREATE TABLE IF NOT EXISTS public.ai_agent_instruction_extensions (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    scope text NOT NULL
                        CHECK (scope IN ('tenant', 'channel', 'contact')),
                    scope_key text,
                    scope_key_norm text NOT NULL DEFAULT '',
                    extension_key text NOT NULL,
                    category text NOT NULL,
                    instruction_text text NOT NULL,
                    instruction_hash text NOT NULL,
                    source text NOT NULL
                        CHECK (source IN ('user', 'model_proposal', 'migration', 'system')),
                    status text NOT NULL DEFAULT 'pending_review'
                        CHECK (status IN (
                            'pending_review', 'active', 'rejected', 'superseded', 'expired'
                        )),
                    importance numeric(5,4),
                    confidence numeric(5,4),
                    evidence_count integer NOT NULL DEFAULT 1,
                    first_seen_at timestamptz NOT NULL DEFAULT now(),
                    last_seen_at timestamptz NOT NULL DEFAULT now(),
                    proposed_by_response_id bigint,
                    proposed_by_inbound_id bigint,
                    approved_by text,
                    approved_at timestamptz,
                    rejected_by text,
                    rejected_at timestamptz,
                    rejection_reason text,
                    expires_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_instruction_extension_active_key
                ON public.ai_agent_instruction_extensions(
                    tenant_id, scope, scope_key_norm, extension_key
                )
                WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS public.ai_contact_memories (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    sender_key text NOT NULL,
                    memory_key text NOT NULL,
                    memory_kind text NOT NULL,
                    value jsonb NOT NULL DEFAULT '{}'::jsonb,
                    safe_summary text,
                    source text NOT NULL DEFAULT 'model_proposal'
                        CHECK (source IN (
                            'explicit_user', 'model_proposal', 'legacy', 'admin', 'system'
                        )),
                    status text NOT NULL DEFAULT 'active'
                        CHECK (status IN (
                            'pending', 'active', 'superseded', 'forgotten', 'rejected', 'expired'
                        )),
                    importance numeric(5,4) NOT NULL DEFAULT 0,
                    confidence numeric(5,4) NOT NULL DEFAULT 0,
                    use_in_instructions boolean NOT NULL DEFAULT false,
                    sensitive boolean NOT NULL DEFAULT false,
                    source_inbound_id bigint,
                    source_response_id bigint,
                    first_seen_at timestamptz NOT NULL DEFAULT now(),
                    last_confirmed_at timestamptz,
                    expires_at timestamptz,
                    superseded_by_id bigint,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_contact_memory_active_key
                ON public.ai_contact_memories(tenant_id, sender_key, memory_key)
                WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS public.ai_conversation_summaries (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    conversation_key text NOT NULL,
                    version bigint NOT NULL DEFAULT 1,
                    current_goal text,
                    summary text,
                    resolved_points jsonb NOT NULL DEFAULT '[]'::jsonb,
                    open_questions jsonb NOT NULL DEFAULT '[]'::jsonb,
                    user_corrections jsonb NOT NULL DEFAULT '[]'::jsonb,
                    commitments jsonb NOT NULL DEFAULT '[]'::jsonb,
                    last_failure text,
                    last_inbound_id bigint,
                    last_response_id bigint,
                    approximate_token_count integer NOT NULL DEFAULT 0,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, conversation_key)
                );

                CREATE TABLE IF NOT EXISTS public.ai_memory_proposals (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    conversation_key text,
                    sender_key text,
                    inbound_id bigint,
                    response_id bigint,
                    proposal_type text NOT NULL
                        CHECK (proposal_type IN (
                            'contact_memory', 'conversation_memory',
                            'instruction_extension', 'forget_memory', 'summary_delta'
                        )),
                    target_scope text NOT NULL,
                    proposal_key text,
                    proposed_value jsonb NOT NULL DEFAULT '{}'::jsonb,
                    proposed_text text,
                    importance numeric(5,4),
                    confidence numeric(5,4),
                    reason_code text,
                    sensitive_detected boolean NOT NULL DEFAULT false,
                    status text NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'approved', 'applied',
                            'rejected', 'duplicate', 'superseded'
                        )),
                    idempotency_key text NOT NULL UNIQUE,
                    applied_memory_id bigint,
                    applied_extension_id bigint,
                    rejection_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    reviewed_at timestamptz,
                    applied_at timestamptz,
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE INDEX IF NOT EXISTS idx_ai_memory_proposals_review
                ON public.ai_memory_proposals(tenant_id, status, created_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_attendance_reviews (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    conversation_key text,
                    sender_key text,
                    inbound_id bigint,
                    response_id bigint,
                    channel text,
                    customer_text text,
                    agent_reply text,
                    outcome text NOT NULL DEFAULT 'reviewed',
                    failure_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
                    signals jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_ai_attendance_reviews_created
                ON public.ai_attendance_reviews (tenant_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_learning_insights (
                    id bigserial PRIMARY KEY,
                    tenant_id text NOT NULL,
                    insight_key text NOT NULL,
                    category text NOT NULL,
                    title text NOT NULL,
                    insight_text text NOT NULL,
                    evidence_count integer NOT NULL DEFAULT 1,
                    confidence numeric(5,4) NOT NULL DEFAULT 0.5,
                    importance numeric(5,4) NOT NULL DEFAULT 0.5,
                    status text NOT NULL DEFAULT 'pending_review',
                    applied_extension_id bigint,
                    source_review_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                    first_seen_at timestamptz NOT NULL DEFAULT now(),
                    last_seen_at timestamptz NOT NULL DEFAULT now(),
                    reviewed_at timestamptz,
                    expires_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_learning_insight_pending_key
                ON public.ai_learning_insights (tenant_id, insight_key)
                WHERE status = 'pending_review';

                CREATE TABLE IF NOT EXISTS public.ai_human_takeover_state (
                    state_key text PRIMARY KEY,
                    conversation_key text,
                    sender_key text,
                    last_human_activity_at timestamptz NOT NULL DEFAULT now(),
                    takeover_detected_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE INDEX IF NOT EXISTS idx_ai_human_takeover_activity
                ON public.ai_human_takeover_state (last_human_activity_at DESC);

                CREATE TABLE IF NOT EXISTS public.ai_catalog_cache (
                    cache_key text PRIMARY KEY,
                    products jsonb NOT NULL DEFAULT '[]'::jsonb,
                    product_count integer NOT NULL DEFAULT 0,
                    refreshed_at timestamptz NOT NULL DEFAULT now(),
                    expires_at timestamptz NOT NULL,
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_cache_expires
                ON public.ai_catalog_cache (expires_at);

                CREATE TABLE IF NOT EXISTS public.ai_catalog_index (
                    tenant_id text NOT NULL DEFAULT 'newstore',
                    catalog_item_key text NOT NULL DEFAULT '',
                    product_id text NOT NULL,
                    variant_id text NULL,
                    sku text NULL,
                    ean text NULL,
                    reference text NULL,
                    brand text NULL,
                    collection text NULL,
                    model text NULL,
                    title_normalized text NOT NULL DEFAULT '',
                    category text NULL,
                    gender text NULL,
                    mechanism text NULL,
                    case_size text NULL,
                    dial_color text NULL,
                    strap_color text NULL,
                    material text NULL,
                    strap_type text NULL,
                    colors_normalized jsonb NOT NULL DEFAULT '[]'::jsonb,
                    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
                    price numeric NULL,
                    promotional_price numeric NULL,
                    stock integer NULL,
                    available boolean NULL,
                    available_in_store boolean NULL,
                    url text NULL,
                    image_url text NULL,
                    freshness_at timestamptz NOT NULL DEFAULT now(),
                    factual_source text NOT NULL DEFAULT 'tray_search',
                    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, catalog_item_key)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_brand
                ON public.ai_catalog_index (tenant_id, brand);

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_ean
                ON public.ai_catalog_index (tenant_id, ean)
                WHERE ean IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_reference
                ON public.ai_catalog_index (tenant_id, reference)
                WHERE reference IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_title
                ON public.ai_catalog_index (tenant_id, title_normalized);

                CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_product
                ON public.ai_catalog_index (tenant_id, product_id);

                CREATE TABLE IF NOT EXISTS public.instagram_story_products (
                  id bigserial PRIMARY KEY,
                  tenant_id text NOT NULL,
                  provider text NOT NULL DEFAULT 'brevo',
                  instagram_account_id text NOT NULL,
                  story_media_id text NOT NULL,
                  story_message_id text NULL,
                  story_permalink text NULL,
                  media_type text NOT NULL DEFAULT 'unknown',
                  source_timestamp timestamptz NULL,
                  story_expires_at timestamptz NULL,
                  media_storage_path text NULL,
                  media_sha256 text NULL,
                  thumbnail_sha256 text NULL,
                  catalog_item_key text NULL,
                  product_id text NULL,
                  variant_id text NULL,
                  match_source text NOT NULL DEFAULT 'pending',
                  match_status text NOT NULL DEFAULT 'pending',
                  match_confidence numeric(6,5) NOT NULL DEFAULT 0,
                  visual_analysis jsonb NOT NULL DEFAULT '{}'::jsonb,
                  candidate_products jsonb NOT NULL DEFAULT '[]'::jsonb,
                  match_explanation jsonb NOT NULL DEFAULT '{}'::jsonb,
                  confirmed_by text NULL,
                  confirmed_at timestamptz NULL,
                  first_seen_at timestamptz NOT NULL DEFAULT now(),
                  last_seen_at timestamptz NOT NULL DEFAULT now(),
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  UNIQUE (tenant_id, provider, instagram_account_id, story_media_id)
                );

                CREATE INDEX IF NOT EXISTS idx_instagram_story_products_lookup
                ON public.instagram_story_products (
                  tenant_id, instagram_account_id, story_media_id
                );

                CREATE INDEX IF NOT EXISTS idx_instagram_story_products_status
                ON public.instagram_story_products (
                  tenant_id, match_status, updated_at DESC
                );

                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS analysis_version text NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS processing_started_at timestamptz NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS processing_owner text NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS processing_attempts integer NOT NULL DEFAULT 0;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS processing_expires_at timestamptz NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS last_failure_code text NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS media_mime text NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS media_bytes integer NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS media_deleted_at timestamptz NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS media_expires_at timestamptz NULL;
                ALTER TABLE public.instagram_story_products
                  ADD COLUMN IF NOT EXISTS media_items jsonb NOT NULL DEFAULT '[]'::jsonb;

                CREATE TABLE IF NOT EXISTS public.ai_inbound_inbox (
                  id bigserial PRIMARY KEY,
                  provider text NOT NULL,
                  channel text NOT NULL DEFAULT 'unknown',
                  message_id text NULL,
                  idempotency_key text NOT NULL,
                  conversation_key text NULL,
                  visitor_id text NULL,
                  sender_key text NULL,
                  event_name text NULL,
                  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                  status text NOT NULL DEFAULT 'pending',
                  attempts int NOT NULL DEFAULT 0,
                  max_attempts int NOT NULL DEFAULT 8,
                  lease_owner text NULL,
                  lease_expires_at timestamptz NULL,
                  last_error text NULL,
                  processed_inbound_id bigint NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  processed_at timestamptz NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_inbound_inbox_idempotency
                ON public.ai_inbound_inbox (idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_ai_inbound_inbox_status_created
                ON public.ai_inbound_inbox (status, created_at ASC);

                CREATE TABLE IF NOT EXISTS public.ai_outbound_outbox (
                  id bigserial PRIMARY KEY,
                  inbox_id bigint NULL,
                  inbound_id bigint NULL,
                  provider text NOT NULL,
                  channel text NOT NULL DEFAULT 'unknown',
                  conversation_key text NULL,
                  visitor_id text NULL,
                  sender_key text NULL,
                  recipient_external_id text NULL,
                  reply_text text NOT NULL DEFAULT '',
                  reply_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                  status text NOT NULL DEFAULT 'pending',
                  attempts int NOT NULL DEFAULT 0,
                  max_attempts int NOT NULL DEFAULT 8,
                  lease_owner text NULL,
                  lease_expires_at timestamptz NULL,
                  last_error text NULL,
                  provider_response jsonb NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  sent_at timestamptz NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_outbound_outbox_status_created
                ON public.ai_outbound_outbox (status, created_at ASC);
                """
            )


def _prepare_inbound_message(message: dict[str, Any]) -> dict[str, Any]:
    safe_message = dict(message or {})
    defaults = {
        "provider": "brevo",
        "event_type": None,
        "message_id": None,
        "conversation_id": None,
        "channel": "unknown",
        "sender_key": None,
        "sender_external_id": None,
        "visitor_id": None,
        "sender_username": None,
        "source_channel_ref": None,
        "source_channel_link": None,
        "source_conversation_ref": None,
        "sender_phone": None,
        "sender_name": None,
        "text": "",
    }
    for key, value in defaults.items():
        safe_message.setdefault(key, value)
    metadata = dict(safe_message.get("channel_metadata") or {})
    image_url = str(safe_message.get("image_url") or metadata.get("image_url") or "").strip()
    if image_url:
        metadata["image_url_present"] = True
        metadata["image_url"] = image_url
    input_modality = str(safe_message.get("input_modality") or "").strip()
    if input_modality:
        metadata["input_modality"] = input_modality
    attachment_type = str(
        safe_message.get("attachment_type") or metadata.get("attachment_type") or ""
    ).strip()
    if attachment_type:
        metadata["attachment_type"] = attachment_type
    safe_message["channel_metadata"] = to_jsonb(metadata)
    safe_message["raw"] = to_jsonb(safe_message.get("raw") or {})
    return safe_message


def resolve_context_filter(
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
    *,
    table_alias: str = "inbound",
) -> tuple[str | None, dict[str, Any]]:
    if conversation_id:
        return (
            f"{table_alias}.conversation_id = %(conversation_id)s",
            {"conversation_id": conversation_id},
        )
    if sender_key:
        return (
            f"{table_alias}.sender_key = %(sender_key)s",
            {"sender_key": sender_key},
        )
    if sender_phone:
        return (
            f"{table_alias}.sender_phone = %(sender_phone)s",
            {"sender_phone": sender_phone},
        )
    return None, {}


def insert_inbound_message(message: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_message = _prepare_inbound_message(message)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_inbound_messages
                  (
                    provider,
                    event_type,
                    message_id,
                    conversation_id,
                    channel,
                    sender_key,
                    sender_external_id,
                    visitor_id,
                    sender_username,
                    source_channel_ref,
                    source_channel_link,
                    source_conversation_ref,
                    sender_phone,
                    sender_name,
                    text,
                    channel_metadata,
                    raw
                  )
                VALUES
                  (
                    %(provider)s,
                    %(event_type)s,
                    %(message_id)s,
                    %(conversation_id)s,
                    %(channel)s,
                    %(sender_key)s,
                    %(sender_external_id)s,
                    %(visitor_id)s,
                    %(sender_username)s,
                    %(source_channel_ref)s,
                    %(source_channel_link)s,
                    %(source_conversation_ref)s,
                    %(sender_phone)s,
                    %(sender_name)s,
                    %(text)s,
                    %(channel_metadata)s,
                    %(raw)s
                  )
                RETURNING id
                """,
                safe_message,
            )

            row = cur.fetchone()
            return get_returning_id(row)


def inbound_message_exists(provider: str | None, message_id: str | None) -> bool:
    """Return whether this provider message was already recorded.

    Missing IDs are intentionally never deduplicated because two identical texts
    can be legitimate separate messages.
    """
    settings = get_settings()
    if not settings.database_url or not provider or not message_id:
        return False

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.ai_inbound_messages
                WHERE provider = %(provider)s
                  AND message_id = %(message_id)s
                LIMIT 1
                """,
                {"provider": provider, "message_id": message_id},
            )
            return cur.fetchone() is not None


def claim_inbound_message(message: dict[str, Any]) -> tuple[bool, int | None]:
    """Atomically claim an inbound message using a PostgreSQL transaction lock."""
    settings = get_settings()
    if not settings.database_url:
        return True, None

    safe_message = _prepare_inbound_message(message)

    if not safe_message.get("message_id"):
        return True, insert_inbound_message(message)

    ensure_tables()
    lock_key = f"{safe_message['provider']}:{safe_message['message_id']}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {"lock_key": lock_key},
            )
            cur.execute(
                """
                SELECT id
                FROM public.ai_inbound_messages
                WHERE provider = %(provider)s AND message_id = %(message_id)s
                LIMIT 1
                """,
                safe_message,
            )
            existing = cur.fetchone()
            if existing:
                return False, get_returning_id(existing)

            cur.execute(
                """
                INSERT INTO public.ai_inbound_messages
                  (
                    provider, event_type, message_id, conversation_id, channel,
                    sender_key, sender_external_id, visitor_id, sender_username,
                    source_channel_ref, source_channel_link, source_conversation_ref,
                    sender_phone, sender_name, text, channel_metadata, raw
                  )
                VALUES
                  (
                    %(provider)s, %(event_type)s, %(message_id)s, %(conversation_id)s,
                    %(channel)s, %(sender_key)s, %(sender_external_id)s, %(visitor_id)s,
                    %(sender_username)s, %(source_channel_ref)s, %(source_channel_link)s,
                    %(source_conversation_ref)s, %(sender_phone)s, %(sender_name)s,
                    %(text)s, %(channel_metadata)s, %(raw)s
                  )
                RETURNING id
                """,
                safe_message,
            )
            return True, get_returning_id(cur.fetchone())


def is_latest_inbound_message(
    inbound_id: int | None,
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
) -> bool:
    """Check whether no later inbound row exists for this conversation/contact."""
    settings = get_settings()
    if not settings.database_url or not inbound_id:
        return True
    conversation_filter, params = resolve_context_filter(
        conversation_id,
        sender_key,
        sender_phone,
    )
    if not conversation_filter:
        return True

    ensure_tables()
    params["inbound_id"] = inbound_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1
                FROM public.ai_inbound_messages AS inbound
                WHERE inbound.id > %(inbound_id)s
                  AND {conversation_filter}
                LIMIT 1
                """,
                params,
            )
            return cur.fetchone() is None


def _history_identity_candidates(
    conversation_id: str | None,
    sender_key: str | None,
    sender_phone: str | None,
) -> list[tuple[str | None, str | None, str | None]]:
    candidates: list[tuple[str | None, str | None, str | None]] = [
        (conversation_id, sender_key, sender_phone),
    ]
    if conversation_id and (sender_key or sender_phone):
        candidates.append((None, sender_key, sender_phone))
    if sender_key:
        candidates.append((None, sender_key, None))
    if sender_phone:
        candidates.append((None, None, sender_phone))
    try:
        from .customer_identity import resolve_linked_identity_candidates

        for linked_key, linked_phone in resolve_linked_identity_candidates(
            sender_key=sender_key,
            sender_phone=sender_phone,
        ):
            candidates.append((None, linked_key, linked_phone))
    except Exception as exc:
        print("[sales.context] identity_resolve_failed", {
            "error_type": type(exc).__name__,
        })
    return candidates


def load_recent_conversation_turns(
    *,
    conversation_id: str | None,
    sender_phone: str | None,
    before_inbound_id: int | None,
    limit: int = 8,
    sender_key: str | None = None,
    hard_cap: int = 40,
) -> list[dict[str, Any]]:
    """Load a chronological transcript containing only delivered replies.

    ``limit`` controls how many turns are returned. ``hard_cap`` bounds the
    SQL row fetch (inbound messages). Use a higher limit for commerce handle
    recovery so payment links from earlier in the thread still surface.
    """
    settings = get_settings()
    if not settings.database_url:
        return []

    safe_hard_cap = max(1, min(int(hard_cap), 200))
    safe_limit = max(1, min(int(limit), safe_hard_cap))
    before_filter = (
        "AND inbound.id < %(before_inbound_id)s"
        if before_inbound_id is not None
        else ""
    )
    seen_filters: set[str] = set()
    rows_by_id: dict[int, dict[str, Any]] = {}
    try:
        for conv, key, phone in _history_identity_candidates(
            conversation_id,
            sender_key,
            sender_phone,
        ):
            conversation_filter, identity_params = resolve_context_filter(
                conv,
                key,
                phone,
            )
            if not conversation_filter:
                continue
            filter_token = f"{conversation_filter}|{sorted(identity_params.items())}"
            if filter_token in seen_filters:
                continue
            seen_filters.add(filter_token)
            params: dict[str, Any] = {
                "before_inbound_id": before_inbound_id,
                "limit": safe_limit,
            }
            params.update(identity_params)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT inbound.id, inbound.text, delivered.reply_text, delivered.safety_reason
                        FROM public.ai_inbound_messages AS inbound
                        LEFT JOIN LATERAL (
                            SELECT response.reply_text, response.safety_reason
                            FROM public.ai_agent_responses AS response
                            WHERE response.inbound_id = inbound.id
                              AND response.provider_send_ok = true
                            ORDER BY response.id DESC
                            LIMIT 1
                        ) AS delivered ON true
                        WHERE {conversation_filter}
                          {before_filter}
                        ORDER BY inbound.id DESC
                        LIMIT %(limit)s
                        """,
                        params,
                    )
                    for row in cur.fetchall() or []:
                        inbound_id = row.get("id")
                        if inbound_id is None:
                            continue
                        rows_by_id[int(inbound_id)] = row
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.context] load_failed", {"error_type": type(exc).__name__})
        return []

    rows = [rows_by_id[key] for key in sorted(rows_by_id)][-safe_limit:]
    turns: list[dict[str, Any]] = []
    for row in rows:
        inbound_text = str(row.get("text") or "").strip()
        reply_text = str(row.get("reply_text") or "").strip()
        if inbound_text:
            turns.append({"role": "user", "content": inbound_text})
        if reply_text:
            assistant_turn: dict[str, Any] = {"role": "assistant", "content": reply_text}
            if row.get("safety_reason"):
                assistant_turn["metadata"] = {"safety_reason": str(row["safety_reason"])}
            turns.append(assistant_turn)
    return turns[-safe_limit:]


def _commerce_state_from_provider_response(provider_response: Any) -> dict[str, Any]:
    agent_context = (
        provider_response.get("_agent_context")
        if isinstance(provider_response, dict)
        else None
    )
    state = agent_context.get("commerce_state") if isinstance(agent_context, dict) else None
    return state if isinstance(state, dict) else {}


def _load_commerce_states_for_filter(
    *,
    conversation_filter: str,
    identity_params: dict[str, Any],
    before_inbound_id: int | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "before_inbound_id": before_inbound_id,
        "limit": max(1, min(int(limit), 20)),
    }
    params.update(identity_params)
    before_filter = (
        "AND inbound.id < %(before_inbound_id)s"
        if before_inbound_id is not None
        else ""
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT response.provider_response
                FROM public.ai_agent_responses AS response
                JOIN public.ai_inbound_messages AS inbound
                  ON inbound.id = response.inbound_id
                WHERE {conversation_filter}
                  {before_filter}
                  AND response.provider_send_ok = true
                  AND response.provider_response ? '_agent_context'
                ORDER BY response.id DESC
                LIMIT %(limit)s
                """,
                params,
            )
            rows = cur.fetchall() or []
    states: list[dict[str, Any]] = []
    for row in rows:
        provider_response = row.get("provider_response") if isinstance(row, dict) else None
        state = _commerce_state_from_provider_response(provider_response)
        if state:
            states.append(state)
    return states


def load_customer_commerce_sessions(
    person_keys: list[str],
) -> list[dict[str, Any]]:
    """Load durable commerce sessions for one or more person_key aliases."""
    settings = get_settings()
    keys = [str(key).strip() for key in person_keys if str(key or "").strip()]
    if not settings.database_url or not keys:
        return []
    ensure_tables()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT person_key, commerce_state, resumable_score, updated_at
                    FROM public.ai_customer_commerce_sessions
                    WHERE person_key = ANY(%(keys)s)
                    ORDER BY resumable_score DESC, updated_at DESC
                    """,
                    {"keys": keys},
                )
                rows = cur.fetchall() or []
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.session] load_failed", {"error_type": type(exc).__name__})
        return []

    sessions: list[dict[str, Any]] = []
    for row in rows:
        state = row.get("commerce_state") if isinstance(row, dict) else None
        if isinstance(state, dict) and state:
            sessions.append(state)
    return sessions


def persist_customer_commerce_session(
    *,
    person_keys: list[str],
    commerce_state: dict[str, Any],
    channel: str | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
) -> None:
    """Persist durable working memory under all known person_key aliases."""
    from .context_resume import (
        commerce_state_resumable_score,
        merge_commerce_states,
    )

    settings = get_settings()
    keys = [str(key).strip() for key in person_keys if str(key or "").strip()]
    if not settings.database_url or not keys or not isinstance(commerce_state, dict):
        return

    ensure_tables()
    try:
        existing_sessions = load_customer_commerce_sessions(keys)
        donor = (
            max(existing_sessions, key=commerce_state_resumable_score)
            if existing_sessions
            else {}
        )
        merged = merge_commerce_states(commerce_state, donor)
        score = commerce_state_resumable_score(merged)
        if score <= 0:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
                for key in keys:
                    cur.execute(
                        """
                        INSERT INTO public.ai_customer_commerce_sessions (
                          person_key,
                          commerce_state,
                          channel,
                          conversation_id,
                          sender_key,
                          sender_phone,
                          resumable_score,
                          updated_at
                        )
                        VALUES (
                          %(person_key)s,
                          %(commerce_state)s,
                          %(channel)s,
                          %(conversation_id)s,
                          %(sender_key)s,
                          %(sender_phone)s,
                          %(resumable_score)s,
                          now()
                        )
                        ON CONFLICT (person_key) DO UPDATE
                        SET
                          commerce_state = EXCLUDED.commerce_state,
                          channel = COALESCE(
                            EXCLUDED.channel,
                            public.ai_customer_commerce_sessions.channel
                          ),
                          conversation_id = COALESCE(
                            EXCLUDED.conversation_id,
                            public.ai_customer_commerce_sessions.conversation_id
                          ),
                          sender_key = COALESCE(
                            EXCLUDED.sender_key,
                            public.ai_customer_commerce_sessions.sender_key
                          ),
                          sender_phone = COALESCE(
                            EXCLUDED.sender_phone,
                            public.ai_customer_commerce_sessions.sender_phone
                          ),
                          resumable_score = EXCLUDED.resumable_score,
                          updated_at = now()
                        """,
                        {
                            "person_key": key,
                            "commerce_state": to_jsonb(merged),
                            "channel": channel,
                            "conversation_id": conversation_id,
                            "sender_key": sender_key,
                            "sender_phone": sender_phone,
                            "resumable_score": score,
                        },
                    )
        print("[sales.session] upserted", {
            "aliases": len(keys),
            "person_key_prefix": keys[0].split(":", 1)[0],
            "resumable_score": score,
            "has_order": bool(merged.get("order_id")),
        })
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.session] upsert_failed", {"error_type": type(exc).__name__})


def load_commerce_conversation_state(
    *,
    conversation_id: str | None,
    sender_phone: str | None,
    before_inbound_id: int | None,
    sender_key: str | None = None,
) -> dict[str, Any]:
    """Load delivered commerce state, recovering order context across identities."""
    from .context_resume import (
        commerce_state_resumable_score,
        merge_commerce_states,
    )
    from .customer_identity import resolve_person_key_candidates

    settings = get_settings()
    if not settings.database_url:
        return {}

    identity_candidates: list[tuple[str | None, str | None, str | None]] = [
        (conversation_id, sender_key, sender_phone),
    ]
    # Fallback across channel identities when conversation_id is sparse/stale.
    if conversation_id and (sender_key or sender_phone):
        identity_candidates.append((None, sender_key, sender_phone))
    if sender_key:
        identity_candidates.append((None, sender_key, None))
    if sender_phone:
        identity_candidates.append((None, None, sender_phone))
    try:
        from .customer_identity import resolve_linked_identity_candidates

        for linked_key, linked_phone in resolve_linked_identity_candidates(
            sender_key=sender_key,
            sender_phone=sender_phone,
        ):
            identity_candidates.append((None, linked_key, linked_phone))
    except Exception as exc:
        print("[sales.context.state] identity_resolve_failed", {
            "error_type": type(exc).__name__,
        })

    seen_filters: set[str] = set()
    collected: list[dict[str, Any]] = []
    try:
        for conv, key, phone in identity_candidates:
            conversation_filter, identity_params = resolve_context_filter(
                conv,
                key,
                phone,
            )
            if not conversation_filter:
                continue
            filter_token = f"{conversation_filter}|{sorted(identity_params.items())}"
            if filter_token in seen_filters:
                continue
            seen_filters.add(filter_token)
            collected.extend(
                _load_commerce_states_for_filter(
                    conversation_filter=conversation_filter,
                    identity_params=identity_params,
                    before_inbound_id=before_inbound_id,
                )
            )
    except (psycopg.Error, RuntimeError) as exc:
        print("[sales.context.state] load_failed", {"error_type": type(exc).__name__})
        collected = []

    merged: dict[str, Any] = {}
    if collected:
        latest = collected[0]
        richest = max(collected, key=commerce_state_resumable_score)
        merged = merge_commerce_states(latest, richest)

    person_keys = resolve_person_key_candidates(
        sender_key=sender_key,
        sender_phone=sender_phone,
        state=merged or None,
    )
    durable_sessions = load_customer_commerce_sessions(person_keys)
    durable = (
        max(durable_sessions, key=commerce_state_resumable_score)
        if durable_sessions
        else {}
    )
    if durable:
        merged = merge_commerce_states(merged, durable)

    print("[sales.context.state] loaded", {
        "candidates": len(collected),
        "durable_sessions": len(durable_sessions),
        "merged_has_order": bool(merged.get("order_id")),
        "merged_pending_action": merged.get("pending_action"),
        "merged_score": commerce_state_resumable_score(merged),
    })
    return merged


def has_successful_agent_response(inbound_id: int | None) -> bool:
    """Return True when a successful outbound reply already exists for inbound_id."""
    settings = get_settings()
    if not settings.database_url or inbound_id is None:
        return False

    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.ai_agent_responses
                WHERE inbound_id = %(inbound_id)s
                  AND provider_send_ok = true
                LIMIT 1
                """,
                {"inbound_id": inbound_id},
            )
            return cur.fetchone() is not None


def insert_agent_response(data: dict[str, Any]) -> int | None:
    settings = get_settings()

    if not settings.database_url:
        return None

    ensure_tables()

    safe_data = dict(data or {})

    safe_data.setdefault("inbound_id", None)
    safe_data.setdefault("channel", "unknown")
    safe_data.setdefault("sender_key", None)
    safe_data.setdefault("sender_phone", None)
    safe_data.setdefault("reply_text", "")
    safe_data.setdefault("intent", None)
    safe_data.setdefault("handoff_required", False)
    safe_data.setdefault("safety_reason", None)
    safe_data.setdefault("provider_send_ok", False)

    safe_data["provider_response"] = to_jsonb(safe_data.get("provider_response") or {})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_agent_responses
                  (
                    inbound_id,
                    channel,
                    sender_key,
                    sender_phone,
                    reply_text,
                    intent,
                    handoff_required,
                    safety_reason,
                    provider_send_ok,
                    provider_response
                  )
                VALUES
                  (
                    %(inbound_id)s,
                    %(channel)s,
                    %(sender_key)s,
                    %(sender_phone)s,
                    %(reply_text)s,
                    %(intent)s,
                    %(handoff_required)s,
                    %(safety_reason)s,
                    %(provider_send_ok)s,
                    %(provider_response)s
                  )
                RETURNING id
                """,
                safe_data,
            )

            row = cur.fetchone()
            return get_returning_id(row)
