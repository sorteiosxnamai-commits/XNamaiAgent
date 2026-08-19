# Empacotamento seguro de release

## Não incluir no ZIP

- `.env`, `.env.local`, qualquer `.env.*` (exceto `.env.example`)
- `.git/`, `.vercel/`, `.pytest_cache/`, `__pycache__/`, `*.pyc`
- `.venv/`, `venv/`, `dist/`, `build/`, coverage
- Tokens OIDC / API keys

## Comando

```bash
python scripts/package_release.py
# ou
python scripts/package_release.py --dry-run
```

O script **falha** se encontrar atribuições como `OPENAI_API_KEY=`, `VERCEL_OIDC_TOKEN=`,
`DATABASE_URL=`, etc. Informa só **caminho + nome da variável** — nunca o valor.

## Ação externa obrigatória

Se um ZIP antigo ou pasta `.vercel/` chegou a conter `VERCEL_OIDC_TOKEN` (ou outras
credenciais), **revogue/rotacione o token na Vercel** e não redistribua o artefato.

Não apagamos `.env.local` locais automaticamente — apenas garantimos que não entram
no Git nem no pacote.
