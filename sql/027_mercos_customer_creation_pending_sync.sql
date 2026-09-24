-- MercosAdaptor may confirm a POST with 2xx and an empty body. The claim
-- remains blocking until the customer sync resolves the document digest.
ALTER TABLE public.ai_mercos_customer_creation_claim
    DROP CONSTRAINT IF EXISTS ai_mercos_customer_creation_claim_status_check;
ALTER TABLE public.ai_mercos_customer_creation_claim
    ADD CONSTRAINT ai_mercos_customer_creation_claim_status_check
    CHECK (status IN ('creating', 'created_pending_sync', 'created', 'unknown'));
