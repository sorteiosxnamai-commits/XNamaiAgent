-- Allow settlement_status=processing for atomic claim during Tray create.

ALTER TABLE public.ai_pix_payments
    DROP CONSTRAINT IF EXISTS ai_pix_payments_settlement_status_check;

ALTER TABLE public.ai_pix_payments
    ADD CONSTRAINT ai_pix_payments_settlement_status_check
    CHECK (
        settlement_status IN (
            'none',
            'pending',
            'processing',
            'completed',
            'failed',
            'skipped'
        )
    );
