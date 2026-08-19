# Incidente: credencial em artifact de release (OIDC / .env.local)

## Contexto

Auditorias e ZIPs legados podem ter incluído `.env.local` com `VERCEL_OIDC_TOKEN`
(ou outros segredos). O `.gitignore` protege o Git; **não** protege ZIP da pasta
de trabalho.

## Ações obrigatórias (FASE 0)

1. No [Vercel Dashboard](https://vercel.com) → Team → Settings / Tokens:
   - **Revogar** o OIDC / token exposto.
   - Gerar novo apenas se ainda for necessário ao fluxo local.
2. Invalidar qualquer ZIP / drive / chat onde o artifact tenha sido enviado.
3. Buscar o mesmo valor em logs, backups e artifacts CI (sem colar o segredo).
4. Rotacionar outros segredos que tenham convivido no mesmo `.env.local`
   (`OPENAI_API_KEY`, `BREVO_API_KEY`, `DATABASE_URL`, etc.) se houver dúvida.
5. Confirmar que `python scripts/scan_secrets.py` e
   `python scripts/package_release.py --dry-run` passam no CI.
6. Registrar aqui a data da rotação (sem valores):

| Item | Status | Data |
|------|--------|------|
| VERCEL_OIDC_TOKEN revogado | pendente | |
| ZIP inválido / apagado | pendente | |
| scan_secrets no CI | feito no plano | |
| package_release allowlist | reforçado | |

## Prevenção

- Release só via `scripts/package_release.py` (fail-closed).
- CI bloqueia `.env*` (exceto `.env.example`) e assignments reais de segredos.
- Nunca commitar `.env.local`; nunca anexar pasta de trabalho completa em ZIP manual.

## Contato

Quem empacotar release deve rodar localmente:

```bash
python scripts/scan_secrets.py
python scripts/package_release.py --dry-run
```
