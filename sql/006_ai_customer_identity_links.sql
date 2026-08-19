CREATE TABLE IF NOT EXISTS public.ai_customer_identity_links (
  id bigserial PRIMARY KEY,
  person_key text NOT NULL,
  identity_type text NOT NULL,
  identity_value text NOT NULL,
  channel text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_ai_customer_identity_type_value UNIQUE (identity_type, identity_value)
);

CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_person_key
ON public.ai_customer_identity_links(person_key);

CREATE INDEX IF NOT EXISTS idx_ai_customer_identity_value
ON public.ai_customer_identity_links(identity_type, identity_value);
