"""Replay anonymized conversations through the admin endpoint, without sending.

Set XNAMAI_EVAL_URL and ADMIN_API_TOKEN in the process environment. The key stays
server-side; this script never needs OPENAI_API_KEY. Results contain synthetic
messages only. Exit nonzero on routing, delivery or factual-validation failures.
"""
import json
import os
import secrets
import sys
from pathlib import Path

import httpx

CASES = [
    {"name": "restock", "text": "Queria abastecer minha loja. Por onde começo?", "history": [], "advice": True},
    {"name": "catalog_yes", "text": "sim", "history": [
        {"role": "user", "content": "Queria abastecer minha loja"},
        {"role": "assistant", "content": "Você quer receber o link do catálogo?"}],
     "advice": True, "required": ["xnamai.meuspedidos.com.br"]},
    {"name": "purchase", "text": "Como faço para comprar com vocês?", "history": [],
     "advice": True, "required": ["CPF", "CNPJ", "800"]},
    {"name": "general_comparison", "text": "Qual a diferença entre fone com fio e Bluetooth para revender? Não preciso de modelos agora.",
     "history": [], "advice": True, "required": ["fio", "Bluetooth"]},
    {"name": "correction", "text": "Não foi isso que perguntei, quero entender como comprar", "history": [
        {"role": "user", "content": "Como funciona para comprar?"},
        {"role": "assistant", "content": "Não encontrei esse produto no catálogo agora."}], "advice": True,
     "required": ["cadastro"]},
    {"name": "mixed_advice", "text": "Quero abastecer minha loja com fones e mouses. Como compro, aceitam CPF e qual o pedido mínimo?",
     "history": [], "advice": True, "required": ["CPF", "800"]},
    {"name": "checkout_explanation", "text": "Como faço para finalizar lá no catálogo?", "history": [
        {"role": "user", "content": "Já adicionei os produtos no carrinho do catálogo"},
        {"role": "assistant", "content": "Os produtos ainda estão no carrinho ou você já recebeu um número de pedido?"}],
     "advice": True, "required": ["pedido"]},
    {"name": "wholesale", "text": "Liste as caixas de som 120w", "history": [], "advice": False, "required": ["120"]},
    {"name": "order_status", "text": "Consulte o pedido 95805", "history": [], "advice": False, "required": ["pedido"]},
    {"name": "order_explanation", "text": "O que significa não faturado?", "history": [
        {"role": "user", "content": "Consulte o pedido 95805"},
        {"role": "assistant", "content": "Seu pedido está com status Pedido gerado — não faturado."}],
     "advice": True, "required": ["fatur"]},
    {"name": "mixed_catalog", "text": "Liste caixas de som 120w. Também posso comprar com CPF e qual o pedido mínimo?",
     "history": [], "advice": False, "required": ["120", "CPF", "800"]},
    {"name": "order_next_step", "text": "Okay e o que faço agora?", "history": [
        {"role": "user", "content": "Consulte o pedido 95805"},
        {"role": "assistant", "content": "Seu pedido está com status Pedido gerado — não faturado."}],
     "advice": False, "required": ["pagamento"]},
    {"name": "cpf_choice", "text": "cpf", "history": [
        {"role": "user", "content": "gostaria de comprar com vocês"},
        {"role": "assistant", "content": "Você quer comprar com CPF ou CNPJ? 😊"}],
     "advice": False, "required": ["CPF"], "source": "contextual_registration_choice"},
    {"name": "cnpj_choice", "text": "cnpj", "history": [
        {"role": "assistant", "content": "Você quer comprar com CPF ou CNPJ?"}],
     "advice": False, "required": ["CNPJ"], "source": "contextual_choice"},
    {"name": "payment_choice", "text": "pix", "history": [
        {"role": "assistant", "content": "Você prefere Pix ou cartão?"}],
     "advice": False, "required": ["Pix"], "source": "contextual_choice"},
    {"name": "delivery_choice", "text": "retirada", "history": [
        {"role": "assistant", "content": "Você prefere entrega ou retirada?"}],
     "advice": False, "required": ["retirada"], "source": "contextual_choice"},
]


def check(case, data):
    reply = data.get("reply_text") or ""
    errors = []
    if data.get("handoff_required") or data.get("safety_reason"):
        errors.append("fallback_or_handoff")
    evaluation = data.get("evaluation") or {}
    if case.get("source") and evaluation.get("response_source") != case["source"]:
        errors.append("unexpected_choice_handler")
    if case["advice"] and (evaluation.get("response_source") != "consultative_openai"
                           or evaluation.get("used_commerce_provider")):
        errors.append("advice_routed_to_operational_flow")
    for term in case.get("required", []):
        if term.casefold() not in reply.casefold():
            errors.append("missing:" + term)
    if case["advice"] and any(term in reply.casefold() for term in (
            "não encontrei esse produto", "criação do pedido está bloqueada", "confirme qual produto")):
        errors.append("legacy_unrelated_reply")
    if case["name"] in {"wholesale", "mixed_catalog"} and "não encontrei correspondências" in reply.casefold():
        errors.append("known_catalog_filter_returned_no_products")
    if evaluation.get("factual_validation", {}).get("valid") is False:
        errors.append("factual_validation_failed")
    return errors


def main():
    url, token = os.environ["XNAMAI_EVAL_URL"], os.environ["ADMIN_API_TOKEN"]
    results = []
    run_identity = secrets.randbelow(1000000)
    with httpx.Client(timeout=120, headers={"Authorization": "Bearer " + token}) as client:
        for index, case in enumerate(CASES):
            phone = f"550000{run_identity:06}{index:02}"
            if case["name"] == "order_next_step":
                setup = client.post(url.rstrip("/") + "/api/test/agent", json={
                    "text": "Consulte o pedido 95805", "history": [], "phone": phone, "name": "Avaliação sintética"})
                setup.raise_for_status()
            response = client.post(url.rstrip("/") + "/api/test/agent", json={
                "text": case["text"], "history": case["history"],
                "phone": phone, "name": "Avaliação sintética"})
            response.raise_for_status()
            data = response.json()
            row = {"name": case["name"], "reply": data.get("reply_text"),
                   "evaluation": data.get("evaluation"), "errors": check(case, data)}
            results.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(row["errors"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
