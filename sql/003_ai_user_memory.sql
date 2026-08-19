ALTER TABLE public.ai_user_preferences
  ADD COLUMN IF NOT EXISTS speaking_style text NULL,
  ADD COLUMN IF NOT EXISTS memory_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS recent_topics jsonb NOT NULL DEFAULT '[]'::jsonb;
