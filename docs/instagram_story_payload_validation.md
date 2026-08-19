# Instagram Story payload validation (Brevo)

Operational procedure to capture a **sanitized** real Brevo Conversations payload
before enabling canary or full rollout.

## Rules

- Keep `INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS=false` in production by default.
- Diagnostics must **never** log: full message text, username, phone, signed URLs,
  tokens, or image bytes.
- Diagnostics may log: field names, types, depth, attachment counts, presence of
  Story reference / media id / URL, URL host, irreversible path hashes.

## Procedure

1. In a controlled environment set:
   - `INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS=true`
   - `INSTAGRAM_STORY_RECOGNITION_ENABLED=false`
2. Publish a test Instagram Story from the business account linked to Brevo.
3. Reply to that Story from a test consumer account (`Qual o valor?`).
4. Capture the sanitized diagnostic structure from application logs
   (`instagram_story.payload_diagnostics` / related events).
5. Identify the real field path for:
   - story media id
   - media URL (host only in logs)
   - media type
   - reply-to vs mention
6. Create a sanitized fixture under `tests/fixtures/instagram_story/`
   (no signatures, no PII).
7. Update `app/instagram_story_parser.py` if paths differ from assumptions.
8. Disable diagnostics again (`INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS=false`).

## Acceptance gate

Canary must not be enabled until **at least one real sanitized Brevo payload**
is covered by an automated test.

## Known provider

- Primary ingress: **Brevo Conversations** webhook (not Meta Graph direct).
- Meta-shaped `reply_to.story` blobs may be forwarded inside Brevo messages.
- Direct Meta Story reply support is optional and fixture-gated.
- **Observed production blocker:** Story replies often arrive only as
  `This message cannot be viewed in Brevo…` with **no media URL**. Vision cannot
  run until the visitor resends a normal DM photo, or until Brevo/Meta expose a
  CDN URL. See `app/brevo_instagram_media.py`.

## URL handling

- Operational download uses the full signed CDN URL (`SecretStr`).
- Logs and admin responses use `SafeMediaReference` (host + path hash only).
- `strip_signed_url()` is observability-only — never for download.

## Security note

If a past delivery ZIP ever included `.env.local` with `VERCEL_OIDC_TOKEN`,
revoke/rotate that OIDC token in the Vercel dashboard. Never commit `.env.local`.
Canary/full require `INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED=true` after a real
sanitized payload is covered by tests.
