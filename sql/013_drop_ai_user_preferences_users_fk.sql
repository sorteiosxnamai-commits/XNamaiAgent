-- Dedicated agent DB no longer hosts public.users (sorteio lives on SORTEIO_DATABASE_URL).
-- ai_user_preferences.user_id is a soft reference to the sorteio users.id.
ALTER TABLE public.ai_user_preferences
  DROP CONSTRAINT IF EXISTS ai_user_preferences_user_id_fkey;
