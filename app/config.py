from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_secret(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="production", alias="ENVIRONMENT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    app_name: str = Field(default="NewStoreAgent", alias="APP_NAME")
    dry_run: bool = Field(default=True, alias="DRY_RUN")

    brevo_webhook_secret: str = Field(default="", alias="BREVO_WEBHOOK_SECRET")
    admin_api_token: str = Field(default="", alias="ADMIN_API_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    # Role-specific models (fall back to OPENAI_MODEL when empty).
    openai_main_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MAIN_MODEL")
    openai_fast_model: str = Field(default="gpt-4.1-nano", alias="OPENAI_FAST_MODEL")
    openai_agent_name: str = Field(default="NewStoreAgent", alias="OPENAI_AGENT_NAME")
    openai_transcribe_model: str = Field(default="whisper-1", alias="OPENAI_TRANSCRIBE_MODEL")
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="nova", alias="OPENAI_TTS_VOICE")
    openai_tts_format: str = Field(default="opus", alias="OPENAI_TTS_FORMAT")
    # Responses is the primary path; Chat Completions remains emergency fallback.
    openai_api_mode: Literal[
        "chat_completions",
        "responses",
        "shadow",
        "canary",
    ] = Field(default="responses", alias="OPENAI_API_MODE")
    openai_store_responses: bool = Field(
        default=False,
        alias="OPENAI_STORE_RESPONSES",
    )
    # Never use OpenAI-stored conversation state for commerce; keep false.
    openai_use_previous_response_id: bool = Field(
        default=False,
        alias="OPENAI_USE_PREVIOUS_RESPONSE_ID",
    )
    openai_use_conversations_api: bool = Field(
        default=False,
        alias="OPENAI_USE_CONVERSATIONS_API",
    )
    openai_responses_structured_enabled: bool = Field(
        default=True,
        alias="OPENAI_RESPONSES_STRUCTURED_ENABLED",
    )
    openai_responses_tool_loop_enabled: bool = Field(
        default=True,
        alias="OPENAI_RESPONSES_TOOL_LOOP_ENABLED",
    )
    openai_shadow_sample_rate: float = Field(
        default=0.10,
        alias="OPENAI_SHADOW_SAMPLE_RATE",
        ge=0.0,
        le=1.0,
    )
    # Canary share on Responses (0.0–1.0). Mode=responses ignores this (always 100%).
    openai_responses_traffic_percent: float = Field(
        default=1.0,
        alias="OPENAI_RESPONSES_TRAFFIC_PERCENT",
        ge=0.0,
        le=1.0,
    )
    openai_responses_fallback_to_chat: bool = Field(
        default=True,
        alias="OPENAI_RESPONSES_FALLBACK_TO_CHAT",
    )
    openai_canary_sticky_routing: bool = Field(
        default=True,
        alias="OPENAI_CANARY_STICKY_ROUTING",
    )
    # Etapa 12: progressive canary profile (overrides mode/traffic when canary_*|emergency).
    agent_rollout_profile: Literal[
        "shadow",
        "full",
        "canary_5",
        "canary_25",
        "canary_50",
        "canary_100",
        "emergency",
    ] = Field(default="full", alias="AGENT_ROLLOUT_PROFILE")
    agent_emergency_rollback: bool = Field(
        default=False,
        alias="AGENT_EMERGENCY_ROLLBACK",
    )
    agent_rollout_alert_enabled: bool = Field(
        default=True,
        alias="AGENT_ROLLOUT_ALERT_ENABLED",
    )
    agent_rollout_alert_window: int = Field(
        default=40,
        alias="AGENT_ROLLOUT_ALERT_WINDOW",
        ge=5,
        le=500,
    )
    agent_rollout_alert_min_samples: int = Field(
        default=20,
        alias="AGENT_ROLLOUT_ALERT_MIN_SAMPLES",
        ge=5,
        le=500,
    )
    agent_rollout_fallback_alert_rate: float = Field(
        default=0.25,
        alias="AGENT_ROLLOUT_FALLBACK_ALERT_RATE",
        ge=0.0,
        le=1.0,
    )
    agent_rollout_factual_alert_rate: float = Field(
        default=0.10,
        alias="AGENT_ROLLOUT_FACTUAL_ALERT_RATE",
        ge=0.0,
        le=1.0,
    )
    agent_rollout_handoff_alert_rate: float = Field(
        default=0.40,
        alias="AGENT_ROLLOUT_HANDOFF_ALERT_RATE",
        ge=0.0,
        le=1.0,
    )
    # When false, OPENAI_API_MODE=chat_completions redirects to Responses (+ fallback).
    openai_chat_completions_primary_allowed: bool = Field(
        default=False,
        alias="OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED",
    )
    openai_reasoning_effort: Literal["", "minimal", "low", "medium", "high"] = Field(
        default="medium",
        alias="OPENAI_REASONING_EFFORT",
    )
    openai_text_verbosity: Literal["", "low", "medium", "high"] = Field(
        default="medium",
        alias="OPENAI_TEXT_VERBOSITY",
    )
    openai_max_output_tokens: int | None = Field(
        default=None,
        alias="OPENAI_MAX_OUTPUT_TOKENS",
        ge=1,
    )
    openai_timeout_seconds: float = Field(
        default=45.0,
        alias="OPENAI_TIMEOUT_SECONDS",
        gt=0,
        le=300,
    )
    openai_max_retries: int = Field(
        default=2,
        alias="OPENAI_MAX_RETRIES",
        ge=0,
        le=8,
    )
    # Phase 5: DB persona for tone/identity; falls back to in-code contract.
    agent_db_persona_enabled: bool = Field(
        default=True,
        alias="AGENT_DB_PERSONA_ENABLED",
    )
    agent_persona_tenant_id: str = Field(
        default="newstore",
        alias="AGENT_PERSONA_TENANT_ID",
    )
    agent_persona_key: str = Field(
        default="newstore_commercial",
        alias="AGENT_PERSONA_KEY",
    )
    agent_max_recent_turns: int = Field(
        default=8,
        alias="AGENT_MAX_RECENT_TURNS",
        ge=1,
        le=40,
    )
    agent_max_active_contact_memories: int = Field(
        default=20,
        alias="AGENT_MAX_ACTIVE_CONTACT_MEMORIES",
        ge=1,
        le=100,
    )
    agent_max_contact_memory_chars: int = Field(
        default=3000,
        alias="AGENT_MAX_CONTACT_MEMORY_CHARS",
        ge=200,
        le=20000,
    )
    agent_max_instruction_extensions: int = Field(
        default=20,
        alias="AGENT_MAX_INSTRUCTION_EXTENSIONS",
        ge=1,
        le=100,
    )
    agent_max_instruction_extension_chars: int = Field(
        default=4000,
        alias="AGENT_MAX_INSTRUCTION_EXTENSION_CHARS",
        ge=200,
        le=20000,
    )
    agent_max_conversation_summary_chars: int = Field(
        default=2500,
        alias="AGENT_MAX_CONVERSATION_SUMMARY_CHARS",
        ge=200,
        le=20000,
    )
    # Phase 7: audit proposals on; never auto-apply unless allowlist is set.
    agent_memory_proposals_enabled: bool = Field(
        default=True,
        alias="AGENT_MEMORY_PROPOSALS_ENABLED",
    )
    agent_memory_auto_apply_enabled: bool = Field(
        default=False,
        alias="AGENT_MEMORY_AUTO_APPLY_ENABLED",
    )
    # Comma-separated sender_key allowlist. Empty = nobody. "*" = all senders.
    agent_memory_auto_apply_sender_allowlist: str = Field(
        default="",
        alias="AGENT_MEMORY_AUTO_APPLY_SENDER_ALLOWLIST",
    )
    agent_memory_auto_apply_min_confidence: float = Field(
        default=0.85,
        alias="AGENT_MEMORY_AUTO_APPLY_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    agent_memory_auto_apply_min_importance: float = Field(
        default=0.70,
        alias="AGENT_MEMORY_AUTO_APPLY_MIN_IMPORTANCE",
        ge=0.0,
        le=1.0,
    )
    # Inject active contact memories into system instructions (even without DB persona).
    agent_contact_memory_in_prompt_enabled: bool = Field(
        default=True,
        alias="AGENT_CONTACT_MEMORY_IN_PROMPT_ENABLED",
    )
    agent_instruction_extension_proposals_enabled: bool = Field(
        default=False,
        alias="AGENT_INSTRUCTION_EXTENSION_PROPOSALS_ENABLED",
    )
    agent_conversation_summary_enabled: bool = Field(
        default=False,
        alias="AGENT_CONVERSATION_SUMMARY_ENABLED",
    )
    # Inject compacted conversation summary into compiled system instructions.
    agent_conversation_summary_in_prompt_enabled: bool = Field(
        default=False,
        alias="AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED",
    )
    # off | shadow | enforce — shadow generates but does not inject into reply prompt.
    agent_conversation_summary_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        alias="AGENT_CONVERSATION_SUMMARY_MODE",
    )
    agent_prompt_compilation_audit_enabled: bool = Field(
        default=True,
        alias="AGENT_PROMPT_COMPILATION_AUDIT_ENABLED",
    )
    agent_debug_store_compiled_prompt: bool = Field(
        default=False,
        alias="AGENT_DEBUG_STORE_COMPILED_PROMPT",
    )
    # Temporary rollback: re-embed fallback contract inside <legacy_*> tags
    # (duplicates main instructions). Keep false — Phase 2 default.
    agent_legacy_prompt_compat_enabled: bool = Field(
        default=False,
        alias="AGENT_LEGACY_PROMPT_COMPAT_ENABLED",
    )
    agent_runtime_enabled: bool = Field(default=True, alias="AGENT_RUNTIME_ENABLED")
    # Phase 8: per-turn OpenAI budget. Critique enforce needs headroom for
    # interpret/respond + judge + regenerate + re-judge (1 retry).
    agent_llm_budget_enabled: bool = Field(
        default=True,
        alias="AGENT_LLM_BUDGET_ENABLED",
    )
    # Logical LLM ops: interpret + grounded reply (max 2); complex up to 4.
    agent_max_llm_calls_per_turn: int = Field(
        default=2,
        alias="AGENT_MAX_LLM_CALLS_PER_TURN",
        ge=0,
    )
    agent_max_llm_calls_per_turn_complex: int = Field(
        default=4,
        alias="AGENT_MAX_LLM_CALLS_PER_TURN_COMPLEX",
        ge=0,
    )
    # Etapa 3: unified TurnUnderstanding as interpreter schema (adapted to SalesInterpretation).
    agent_turn_understanding_enabled: bool = Field(
        default=True,
        alias="AGENT_TURN_UNDERSTANDING_ENABLED",
    )
    agent_policy_mode: Literal["off", "shadow", "enforce"] = Field(
        default="shadow",
        alias="AGENT_POLICY_MODE",
    )
    agent_factual_validation_mode: Literal[
        "off",
        "shadow",
        "enforce",
    ] = Field(
        default="enforce",
        alias="AGENT_FACTUAL_VALIDATION_MODE",
    )
    agent_trusted_fact_domains: str = Field(
        default="sorteionewstore.com.br,newstoresorteios.com.br",
        alias="AGENT_TRUSTED_FACT_DOMAINS",
    )
    agent_conversation_lock_enabled: bool = Field(
        default=True,
        alias="AGENT_CONVERSATION_LOCK_ENABLED",
    )
    agent_conversation_lock_timeout_seconds: float = Field(
        default=15.0,
        alias="AGENT_CONVERSATION_LOCK_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    # Etapa 7: thin = minimal presenter; full = legacy regex; shadow = outbound full + thin diff.
    agent_presenter_mode: Literal["full", "thin", "shadow"] = Field(
        default="thin",
        alias="AGENT_PRESENTER_MODE",
    )
    # After human assumes a ChatBô thread, resume the bot if the attendant
    # stays idle for this many minutes (next customer message).
    human_takeover_idle_minutes: int = Field(
        default=15,
        alias="HUMAN_TAKEOVER_IDLE_MINUTES",
        ge=1,
        le=1440,
    )
    agent_quality_judge_mode: Literal["off", "shadow", "enforce"] = Field(
        default="off",
        alias="AGENT_QUALITY_JUDGE_MODE",
    )
    agent_quality_judge_risk_threshold: int = Field(
        default=70,
        alias="AGENT_QUALITY_JUDGE_RISK_THRESHOLD",
        ge=0,
        le=100,
    )
    agent_quality_judge_sample_rate: float = Field(
        default=0.0,
        alias="AGENT_QUALITY_JUDGE_SAMPLE_RATE",
        ge=0.0,
        le=1.0,
    )
    # Dual-agent critique: shadow by default (Etapa 6); LLM only on risk/sample.
    agent_critique_mode: Literal["off", "shadow", "enforce"] = Field(
        default="shadow",
        alias="AGENT_CRITIQUE_MODE",
    )
    agent_critique_max_retries: int = Field(
        default=1,
        alias="AGENT_CRITIQUE_MAX_RETRIES",
        ge=0,
        le=10,
    )
    agent_critique_llm_on_risk_only: bool = Field(
        default=True,
        alias="AGENT_CRITIQUE_LLM_ON_RISK_ONLY",
    )
    agent_critique_shadow_sample_rate: float = Field(
        default=0.0,
        alias="AGENT_CRITIQUE_SHADOW_SAMPLE_RATE",
        ge=0.0,
        le=1.0,
    )
    # Hourly self-learning over recent attendances.
    agent_learning_lookback_hours: int = Field(
        default=2,
        alias="AGENT_LEARNING_LOOKBACK_HOURS",
        ge=1,
        le=48,
    )
    agent_learning_batch_limit: int = Field(
        default=120,
        alias="AGENT_LEARNING_BATCH_LIMIT",
        ge=10,
        le=500,
    )
    # Etapa 9: learning writes pending proposals only; never auto-activate.
    agent_learning_auto_promote: bool = Field(
        default=False,
        alias="AGENT_LEARNING_AUTO_PROMOTE",
    )
    agent_learning_auto_activate: bool = Field(
        default=False,
        alias="AGENT_LEARNING_AUTO_ACTIVATE",
    )
    # Model prompt window (interpreter/responder/critique). Operational recovery
    # still loads up to AGENT_HISTORY_HARD_CAP from the database.
    # Accept legacy Vercel values up to 200, then normalize in model_validator.
    agent_history_limit: int = Field(
        default=12,
        alias="AGENT_HISTORY_LIMIT",
        ge=4,
        le=200,
    )
    agent_history_hard_cap: int = Field(
        default=80,
        alias="AGENT_HISTORY_HARD_CAP",
        ge=8,
        le=200,
    )
    agent_send_idempotency_enabled: bool = Field(
        default=True,
        alias="AGENT_SEND_IDEMPOTENCY_ENABLED",
    )
    # Phase 13: short TTL Tray product cache (off disables all kinds).
    agent_product_cache_enabled: bool = Field(
        default=True,
        alias="AGENT_PRODUCT_CACHE_ENABLED",
    )
    agent_product_cache_ttl_seconds: float = Field(
        default=180.0,
        alias="AGENT_PRODUCT_CACHE_TTL_SECONDS",
        ge=0,
        le=3600,
    )
    agent_price_cache_ttl_seconds: float = Field(
        default=45.0,
        alias="AGENT_PRICE_CACHE_TTL_SECONDS",
        ge=0,
        le=600,
    )
    agent_stock_cache_ttl_seconds: float = Field(
        default=20.0,
        alias="AGENT_STOCK_CACHE_TTL_SECONDS",
        ge=0,
        le=300,
    )
    agent_search_cache_ttl_seconds: float = Field(
        default=45.0,
        alias="AGENT_SEARCH_CACHE_TTL_SECONDS",
        ge=0,
        le=600,
    )
    # Brand/category catalog pools used for preference filtering (azul/blue…).
    agent_catalog_cache_ttl_seconds: int = Field(
        default=3600,
        alias="AGENT_CATALOG_CACHE_TTL_SECONDS",
        ge=60,
        le=86400,
    )
    # Etapa 4: hybrid retrieval / LLM rerank / revalidation budgets.
    agent_candidate_pool_limit: int = Field(
        default=20,
        alias="AGENT_CANDIDATE_POOL_LIMIT",
        ge=5,
        le=80,
    )
    agent_rerank_selection_limit: int = Field(
        default=15,
        alias="AGENT_RERANK_SELECTION_LIMIT",
        ge=5,
        le=20,
    )
    agent_revalidate_top_n: int = Field(
        default=3,
        alias="AGENT_REVALIDATE_TOP_N",
        ge=1,
        le=10,
    )
    agent_catalog_index_write_enabled: bool = Field(
        default=True,
        alias="AGENT_CATALOG_INDEX_WRITE_ENABLED",
    )
    agent_catalog_index_read_enabled: bool = Field(
        default=True,
        alias="AGENT_CATALOG_INDEX_READ_ENABLED",
    )
    agent_catalog_index_fallback_to_tray: bool = Field(
        default=True,
        alias="AGENT_CATALOG_INDEX_FALLBACK_TO_TRAY",
    )
    agent_catalog_index_max_age_seconds: int = Field(
        default=86_400,
        alias="AGENT_CATALOG_INDEX_MAX_AGE_SECONDS",
        ge=60,
        le=2_592_000,
    )
    agent_catalog_index_candidate_limit: int = Field(
        default=30,
        alias="AGENT_CATALOG_INDEX_CANDIDATE_LIMIT",
        ge=1,
        le=100,
    )
    agent_image_cache_ttl_seconds: float = Field(
        default=300.0,
        alias="AGENT_IMAGE_CACHE_TTL_SECONDS",
        ge=0,
        le=3600,
    )
    # Phase 14: salt for irreversible observability ids (empty → process-local).
    agent_obs_hash_secret: str = Field(
        default="",
        alias="AGENT_OBS_HASH_SECRET",
    )
    # Etapa 10: full/HTTP obs opt-in only (PII-safe defaults).
    agent_full_obs_logs: bool = Field(
        default=False,
        alias="AGENT_FULL_OBS_LOGS",
    )
    # Attach turn runtime context to every HTTP request (not only webhooks).
    agent_http_obs_logs: bool = Field(
        default=False,
        alias="AGENT_HTTP_OBS_LOGS",
    )

    tray_adapter_url: str = Field(default="", alias="TRAY_ADAPTER_URL")
    tray_adapter_token: str = Field(default="", alias="TRAY_ADAPTER_TOKEN")

    audio_inbound_enabled: bool = Field(default=True, alias="AUDIO_INBOUND_ENABLED")
    audio_outbound_enabled: bool = Field(default=True, alias="AUDIO_OUTBOUND_ENABLED")
    brevo_send_audio_as_attachment: bool = Field(default=True, alias="BREVO_SEND_AUDIO_AS_ATTACHMENT")
    audio_public_base_url: str = Field(default="", alias="AUDIO_PUBLIC_BASE_URL")
    # Vision → catalog search when customer sends a product photo.
    agent_image_search_enabled: bool = Field(
        default=True,
        alias="AGENT_IMAGE_SEARCH_ENABLED",
    )
    agent_image_search_model: str = Field(
        default="",
        alias="AGENT_IMAGE_SEARCH_MODEL",
    )
    agent_image_search_min_confidence: float = Field(
        default=0.55,
        alias="AGENT_IMAGE_SEARCH_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    agent_image_download_max_bytes: int = Field(
        default=8_000_000,
        alias="AGENT_IMAGE_DOWNLOAD_MAX_BYTES",
        ge=100_000,
        le=20_000_000,
    )
    # Phase 2: visual nearest-neighbor fallback over catalog image fingerprints.
    agent_visual_search_enabled: bool = Field(
        default=True,
        alias="AGENT_VISUAL_SEARCH_ENABLED",
    )
    agent_product_image_index_enabled: bool = Field(
        default=True,
        alias="AGENT_PRODUCT_IMAGE_INDEX_ENABLED",
    )
    agent_visual_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="AGENT_VISUAL_EMBEDDING_MODEL",
    )
    agent_visual_top_k: int = Field(
        default=3,
        alias="AGENT_VISUAL_TOP_K",
        ge=1,
        le=10,
    )
    agent_visual_max_distance: float = Field(
        default=0.45,
        alias="AGENT_VISUAL_MAX_DISTANCE",
        ge=0.0,
        le=2.0,
    )
    agent_product_image_index_batch_size: int = Field(
        default=40,
        alias="AGENT_PRODUCT_IMAGE_INDEX_BATCH_SIZE",
        ge=1,
        le=200,
    )
    checkout_cep_lookup_enabled: bool = Field(
        default=True,
        alias="CHECKOUT_CEP_LOOKUP_ENABLED",
    )
    checkout_cep_lookup_url: str = Field(
        default="https://viacep.com.br/ws",
        alias="CHECKOUT_CEP_LOOKUP_URL",
    )

    # Mercado Pago PIX direto no chat (fase 1+: create; webhook depois).
    # Default off até o fluxo de venda ligar o canal.
    pix_direct_enabled: bool = Field(default=False, alias="PIX_DIRECT_ENABLED")
    mp_access_token: str = Field(default="", alias="MP_ACCESS_TOKEN")
    mercadopago_access_token: str = Field(
        default="",
        alias="MERCADOPAGO_ACCESS_TOKEN",
    )
    mp_base_url: str = Field(
        default="https://api.mercadopago.com",
        alias="MP_BASE_URL",
    )
    pix_exp_min: int = Field(default=30, alias="PIX_EXP_MIN", ge=1, le=1440)
    public_url: str = Field(default="", alias="PUBLIC_URL")

    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")
    supabase_audio_bucket: str = Field(default="agent-audio", alias="SUPABASE_AUDIO_BUCKET")

    # Agent-owned Postgres (ai_* tables, sessions, memory, image index).
    database_url: str = Field(default="", alias="DATABASE_URL")
    # Sorteio/raffle domain Postgres (users, draw/draws, payments, app_config_new).
    # Falls back to DATABASE_URL when empty (legacy shared-DB setups).
    sorteio_database_url: str = Field(default="", alias="SORTEIO_DATABASE_URL")
    auto_create_tables: bool = Field(default=False, alias="AUTO_CREATE_TABLES")
    remarketing_enabled: bool = Field(default=False, alias="REMARKETING_ENABLED")
    remarketing_cron_secret: str = Field(default="", alias="CRON_SECRET")
    remarketing_touch_hours: str = Field(default="1,12,23", alias="REMARKETING_TOUCH_HOURS")
    remarketing_meta_window_hours: int = Field(default=24, alias="REMARKETING_META_WINDOW_HOURS")
    remarketing_batch_size: int = Field(default=25, alias="REMARKETING_BATCH_SIZE")

    brevo_api_key: str = Field(default="", alias="BREVO_API_KEY")
    brevo_send_url: str = Field(default="", alias="BREVO_SEND_URL")
    brevo_sender_number: str = Field(default="", alias="BREVO_SENDER_NUMBER")
    brevo_reply_mode: str = Field(default="auto", alias="BREVO_REPLY_MODE")
    brevo_agent_id: str = Field(default="", alias="BREVO_AGENT_ID")
    brevo_agent_email: str = Field(default="", alias="BREVO_AGENT_EMAIL")
    brevo_agent_name: str = Field(default="NewStoreAgent", alias="BREVO_AGENT_NAME")
    brevo_received_from: str = Field(default="NewStoreAgent", alias="BREVO_RECEIVED_FROM")
    brevo_allowed_channels: str = Field(
        # Instagram temporarily off — re-add "instagram" when social DM is stable.
        default="whatsapp,facebook",
        alias="BREVO_ALLOWED_CHANNELS",
    )
    brevo_social_channels_enabled: bool = Field(
        default=True,
        alias="BREVO_SOCIAL_CHANNELS_ENABLED",
    )
    # FASE 2: validate → enqueue → 200; worker processes turns. Default off until canary.
    agent_async_ingress_enabled: bool = Field(
        default=False,
        alias="AGENT_ASYNC_INGRESS_ENABLED",
    )
    agent_inbox_batch_size: int = Field(
        default=5,
        alias="AGENT_INBOX_BATCH_SIZE",
        ge=1,
        le=25,
    )
    agent_inbox_lease_seconds: int = Field(
        default=120,
        alias="AGENT_INBOX_LEASE_SECONDS",
        ge=30,
        le=600,
    )

    # Meta Instagram Messaging (FASE 3) — direct media, not Brevo CDN.
    meta_webhook_enabled: bool = Field(
        default=False,
        alias="META_WEBHOOK_ENABLED",
    )
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_ig_app_secret: str = Field(default="", alias="META_IG_APP_SECRET")
    meta_verify_token: str = Field(default="", alias="META_VERIFY_TOKEN")
    meta_page_access_token: str = Field(default="", alias="META_PAGE_ACCESS_TOKEN")
    meta_ig_business_account_id: str = Field(
        default="",
        alias="META_IG_BUSINESS_ACCOUNT_ID",
    )
    instagram_ingress_provider: Literal["meta", "brevo", "dual"] = Field(
        default="meta",
        alias="INSTAGRAM_INGRESS_PROVIDER",
    )

    # Instagram Story ↔ product recognition (default off until real payload validated).
    instagram_story_recognition_enabled: bool = Field(
        default=False,
        alias="INSTAGRAM_STORY_RECOGNITION_ENABLED",
    )
    instagram_story_payload_diagnostics: bool = Field(
        default=False,
        alias="INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS",
    )
    instagram_story_media_storage_enabled: bool = Field(
        default=True,
        alias="INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED",
    )
    instagram_story_media_max_bytes: int = Field(
        default=12_582_912,
        alias="INSTAGRAM_STORY_MEDIA_MAX_BYTES",
        ge=1024,
        le=52_428_800,
    )
    instagram_story_media_timeout_seconds: float = Field(
        default=10.0,
        alias="INSTAGRAM_STORY_MEDIA_TIMEOUT_SECONDS",
        ge=1.0,
        le=60.0,
    )
    instagram_story_media_retention_days: int = Field(
        default=7,
        alias="INSTAGRAM_STORY_MEDIA_RETENTION_DAYS",
        ge=1,
        le=90,
    )
    instagram_story_allowed_hosts: str = Field(
        default="",
        alias="INSTAGRAM_STORY_ALLOWED_HOSTS",
    )
    instagram_story_video_frame_analysis_enabled: bool = Field(
        default=False,
        alias="INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED",
    )
    instagram_story_video_max_frames: int = Field(
        default=3,
        alias="INSTAGRAM_STORY_VIDEO_MAX_FRAMES",
        ge=1,
        le=5,
    )
    instagram_story_vision_model: str = Field(
        default="",
        alias="INSTAGRAM_STORY_VISION_MODEL",
    )
    instagram_story_analysis_detail: Literal["low", "high", "auto"] = Field(
        default="high",
        alias="INSTAGRAM_STORY_ANALYSIS_DETAIL",
    )
    instagram_story_analysis_version: str = Field(
        default="v2",
        alias="INSTAGRAM_STORY_ANALYSIS_VERSION",
    )
    instagram_story_visual_cache_enabled: bool = Field(
        default=True,
        alias="INSTAGRAM_STORY_VISUAL_CACHE_ENABLED",
    )
    instagram_story_visual_cache_ttl_days: int = Field(
        default=30,
        alias="INSTAGRAM_STORY_VISUAL_CACHE_TTL_DAYS",
        ge=1,
        le=365,
    )
    instagram_story_auto_match_min_confidence: float = Field(
        default=0.95,
        alias="INSTAGRAM_STORY_AUTO_MATCH_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    instagram_story_exact_match_min_confidence: float = Field(
        default=0.95,
        alias="INSTAGRAM_STORY_EXACT_MATCH_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    instagram_story_visual_match_min_confidence: float = Field(
        default=0.96,
        alias="INSTAGRAM_STORY_VISUAL_MATCH_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    instagram_story_ambiguous_min_confidence: float = Field(
        default=0.65,
        alias="INSTAGRAM_STORY_AMBIGUOUS_MIN_CONFIDENCE",
        ge=0.0,
        le=1.0,
    )
    instagram_story_match_margin: float = Field(
        default=0.12,
        alias="INSTAGRAM_STORY_MATCH_MARGIN",
        ge=0.0,
        le=1.0,
    )
    instagram_story_max_candidates: int = Field(
        default=10,
        alias="INSTAGRAM_STORY_MAX_CANDIDATES",
        ge=1,
        le=20,
    )
    instagram_story_admin_api_enabled: bool = Field(
        default=True,
        alias="INSTAGRAM_STORY_ADMIN_API_ENABLED",
    )
    instagram_story_account_tenant_map: str = Field(
        default="",
        alias="INSTAGRAM_STORY_ACCOUNT_TENANT_MAP",
    )
    instagram_story_storage_bucket: str = Field(
        default="",
        alias="INSTAGRAM_STORY_STORAGE_BUCKET",
    )
    instagram_story_rollout_mode: Literal[
        "off", "diagnostics", "shadow", "canary", "full"
    ] = Field(
        default="off",
        alias="INSTAGRAM_STORY_ROLLOUT_MODE",
    )
    instagram_story_canary_percent: float = Field(
        default=5.0,
        alias="INSTAGRAM_STORY_CANARY_PERCENT",
        ge=0.0,
        le=100.0,
    )
    instagram_story_real_payload_validated: bool = Field(
        default=False,
        alias="INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED",
    )

    max_reply_chars: int = Field(default=900, alias="MAX_REPLY_CHARS")

    @field_validator(
        "openai_api_key", "admin_api_token", "brevo_webhook_secret", "brevo_api_key",
        "tray_adapter_token", "remarketing_cron_secret",
        "mp_access_token", "mercadopago_access_token",
        "meta_app_secret", "meta_ig_app_secret", "meta_verify_token",
        "meta_page_access_token",
        mode="before",
    )
    @classmethod
    def normalize_secret(cls, value: object) -> object:
        if isinstance(value, str):
            return _strip_secret(value)
        return value

    @field_validator("supabase_service_key", mode="before")
    @classmethod
    def normalize_supabase_key(cls, value: object) -> object:
        if isinstance(value, str):
            return _strip_secret(value)
        return value

    @field_validator(
        "openai_model",
        "openai_main_model",
        "openai_fast_model",
        mode="before",
    )
    @classmethod
    def normalize_model(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("openai_reasoning_effort", "openai_text_verbosity", mode="before")
    @classmethod
    def normalize_optional_literal(cls, value: object) -> object:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip().casefold()
        return value

    @field_validator("openai_max_output_tokens", mode="before")
    @classmethod
    def normalize_max_output_tokens(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("public_url", "mp_base_url", mode="before")
    @classmethod
    def normalize_public_base_url(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().rstrip("/")
        return value

    @field_validator("agent_critique_mode", mode="before")
    @classmethod
    def normalize_critique_mode(cls, value: object) -> object:
        """Accept legacy Vercel aliases like on/true → enforce so boot never 500s."""
        if value is None:
            return value
        text = str(value).strip().casefold()
        aliases = {
            "on": "enforce",
            "true": "enforce",
            "1": "enforce",
            "yes": "enforce",
            "enabled": "enforce",
            "false": "off",
            "0": "off",
            "no": "off",
            "disabled": "off",
        }
        normalized = aliases.get(text, text)
        if normalized != text:
            print(
                "[config.critique]",
                {
                    "event": "coerce_legacy_critique_mode",
                    "from": str(value),
                    "to": normalized,
                },
            )
        return normalized

    @model_validator(mode="after")
    def normalize_history_windows(self) -> "Settings":
        """Keep boot resilient when Vercel still has legacy HISTORY_LIMIT=200.

        - hard_cap remains the DB recovery bound
        - values above 40 are treated as legacy "load size" and coerced to the
          intended model window (12)
        """
        hard_cap = int(self.agent_history_hard_cap)
        limit = int(self.agent_history_limit)
        if limit > hard_cap:
            print(
                "[config.history]",
                {"event": "clamp_limit_to_hard_cap", "from": limit, "to": hard_cap},
            )
            limit = hard_cap
        if limit > 40:
            print(
                "[config.history]",
                {
                    "event": "coerce_legacy_history_limit_to_model_window",
                    "from": limit,
                    "to": 12,
                },
            )
            limit = 12
        object.__setattr__(self, "agent_history_limit", limit)
        return self

    def resolved_mp_access_token(self) -> str:
        return self.mp_access_token or self.mercadopago_access_token or ""

    def pix_notification_url(self) -> str | None:
        base = (self.public_url or "").strip().rstrip("/")
        if not base:
            return None
        return f"{base}/api/payments/webhook"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def resolved_sorteio_database_url(settings: Settings | None = None) -> str:
    """Raffle DB URL; falls back to agent DATABASE_URL for legacy shared setups."""
    cfg = settings or get_settings()
    dedicated = str(getattr(cfg, "sorteio_database_url", "") or "").strip()
    if dedicated:
        return dedicated
    return str(getattr(cfg, "database_url", "") or "").strip()


def get_allowed_channels(settings: Settings) -> set[str]:
    return {
        channel.strip().lower()
        for channel in (
            getattr(settings, "brevo_allowed_channels", "whatsapp,facebook") or ""
        ).split(",")
        if channel.strip()
    }


def get_remarketing_touch_hours(settings: Settings) -> list[int]:
    hours: set[int] = set()
    for value in (settings.remarketing_touch_hours or "").split(","):
        try:
            hour = int(value.strip())
        except ValueError:
            continue
        if 0 < hour < settings.remarketing_meta_window_hours:
            hours.add(hour)
    return sorted(hours)
