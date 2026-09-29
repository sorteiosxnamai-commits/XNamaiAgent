# Empacotamento seguro de release

## O que entra no ZIP (allowlist)

Somente estas entradas da raiz (`RELEASE_TOP_LEVEL_DIRS` / `RELEASE_TOP_LEVEL_FILES`
em `scripts/package_release.py`):

- pastas: `api/`, `app/`, `scripts/`, `sql/`, `docs/`, `tests/`, `.github/`
- arquivos: `README.md`, `requirements.txt`, `requirements-dev.txt`, `vercel.json`,
  `pytest.ini`, `persona_xnamai.txt`, `.env.example`, `.gitignore`

Qualquer coisa nova na raiz (notas de ferramentas como `.superpowers/` e
`.remember/`, `.cursor/`, dumps de debug, rascunhos) fica **fora** até ser
adicionada de propósito. O `--dry-run` lista os nomes deixados de fora.

## Não incluir no ZIP, mesmo dentro das pastas liberadas

- `.env`, `.env.local`, qualquer `.env.*` (exceto `.env.example`)
- `.git/`, `.vercel/`, `.pytest_cache/`, `__pycache__/`, `*.pyc`
- `.venv/`, `venv/`, `dist/`, `build/`, coverage (`.coverage`, `.coverage.*`)
- logs e debug: `*.log`, `debug-*`, pastas `logs/`, `tmp/`, `temp/`
- temporários: `*.tmp`, `*.temp`, `*.swp`, `*.bak`, `*.orig`, `*~`
- credenciais locais: `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`,
  `id_ed25519*`, `credentials*.json`, `service-account*.json`, bancos `*.sqlite*`/`*.db`
- Tokens OIDC / API keys

## Varredura de segredos é mais ampla que o pacote

`scripts/scan_secrets.py` varre **todo** o repositório (`iter_repository_files`),
inclusive notas locais e logs que nunca entram no ZIP: segredo em arquivo local
continua sendo vazamento.

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
