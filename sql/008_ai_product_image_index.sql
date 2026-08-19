-- Visual product index for image-search fallback (pgvector).
-- Apply in Supabase/Postgres after enabling the vector extension.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.ai_product_image_index (
  product_id text PRIMARY KEY,
  image_url text NOT NULL,
  brand text NULL,
  model text NULL,
  reference text NULL,
  name text NULL,
  visual_caption text NOT NULL,
  embedding vector(1536) NOT NULL,
  source_hash text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_product_image_index_updated_at
ON public.ai_product_image_index(updated_at DESC);

-- Cosine distance operator <=> ; HNSW needs pgvector >= 0.5.0
CREATE INDEX IF NOT EXISTS idx_ai_product_image_index_embedding_hnsw
ON public.ai_product_image_index
USING hnsw (embedding vector_cosine_ops);
