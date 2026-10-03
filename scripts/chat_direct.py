"""Interactive authenticated preview. Never sends messages to customer channels."""
import argparse
import os
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--channel", choices=["whatsapp", "instagram"], default="whatsapp")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        parser.error("Use HTTPS ou localhost para proteger o token administrativo.")
    token = os.environ.get("ADMIN_API_TOKEN", "").strip()
    if not token:
        parser.error("Configure ADMIN_API_TOKEN no ambiente do terminal.")
    session = None
    print("Preview direto (chama a OpenAI; não envia mensagens a clientes). /reset reinicia, /sair encerra.")
    with httpx.Client(timeout=130, follow_redirects=False,
                      headers={"Authorization": f"Bearer {token}"}) as client:
        while True:
            try:
                message = input("Você: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message == "/sair":
                break
            if message == "/reset":
                session = None
                continue
            if not message:
                continue
            try:
                response = client.post(args.url.rstrip("/") + "/api/test/direct", json={
                    "workspace_id": args.workspace, "channel": args.channel,
                    "text": message, "session": session})
                if response.status_code != 200:
                    print(f"Preview indisponível (HTTP {response.status_code}).")
                    continue
                data = response.json()
                session = data["session"]
                print("Agente:", data["reply_text"])
            except (httpx.HTTPError, ValueError, KeyError):
                print("Falha ao consultar o preview. Tente novamente ou use /reset.")


if __name__ == "__main__":
    main()
