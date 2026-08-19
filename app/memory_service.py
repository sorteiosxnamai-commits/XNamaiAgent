"""Process AgentTurnEnvelope memory side-channel (audit-first)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import get_settings
from .contact_memory_repository import (
    forget_contact_memory,
    get_active_contact_memories,
    upsert_contact_memory,
)
from .conversation_summary_repository import apply_summary_delta
from .instruction_extension_repository import create_extension_proposal
from .memory_models import (
    AgentTurnEnvelope,
    MemoryAction,
    MemoryProcessingResult,
    MemoryProposal,
)
from .memory_policy import (
    evaluate_instruction_extension_proposal,
    evaluate_memory_proposal,
)
from .memory_proposal_repository import (
    insert_memory_proposal,
    mark_proposal_applied,
    mark_proposal_duplicate,
    mark_proposal_pending_review,
    mark_proposal_rejected,
)
from .models import IncomingMessage


def _idempotency_key(
    *,
    tenant_id: str,
    conversation_key: str | None,
    inbound_id: int | None,
    proposal_type: str,
    normalized_key: str | None,
    normalized_value: Any,
) -> str:
    payload = "|".join(
        [
            tenant_id,
            conversation_key or "",
            str(inbound_id or ""),
            proposal_type,
            normalized_key or "",
            json.dumps(normalized_value, ensure_ascii=False, sort_keys=True, default=str),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_agent_memory_proposals(
    *,
    envelope: AgentTurnEnvelope,
    tenant_id: str,
    conversation_key: str | None,
    sender_key: str | None,
    inbound: IncomingMessage | None = None,
    inbound_id: int | None = None,
    response_id: int | None = None,
) -> MemoryProcessingResult:
    """Validate and audit proposals. Apply only when auto-apply flag allows."""
    settings = get_settings()
    result = MemoryProcessingResult()

    proposals_on = bool(getattr(settings, "agent_memory_proposals_enabled", False))
    extensions_on = bool(
        getattr(settings, "agent_instruction_extension_proposals_enabled", False)
    )
    summary_mode = str(
        getattr(settings, "agent_conversation_summary_mode", "off") or "off"
    ).strip().casefold()
    summary_on = summary_mode in {"shadow", "enforce"} or bool(
        getattr(settings, "agent_conversation_summary_enabled", False)
    )
    if not proposals_on and not extensions_on and not summary_on:
        return result

    current = []
    if proposals_on and sender_key:
        try:
            current = get_active_contact_memories(
                tenant_id=tenant_id,
                sender_key=sender_key,
                limit=int(getattr(settings, "agent_max_active_contact_memories", 20)),
            )
        except Exception as exc:
            print("[memory.service.load_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
            })

    if proposals_on:
        for proposal in envelope.memory_proposals or []:
          if not isinstance(proposal, MemoryProposal):
              continue
          if proposal.action == MemoryAction.none:
              continue
          result.proposals_seen += 1
          decision = evaluate_memory_proposal(
              proposal=proposal,
              inbound=inbound,
              current_memories=current,
              tenant_id=tenant_id,
              sender_key=sender_key,
          )
          key = _idempotency_key(
              tenant_id=tenant_id,
              conversation_key=conversation_key,
              inbound_id=inbound_id,
              proposal_type=decision.proposal_type,
              normalized_key=decision.normalized_key,
              normalized_value=decision.normalized_value,
          )
          try:
              proposal_id = insert_memory_proposal(
                  tenant_id=tenant_id,
                  conversation_key=conversation_key,
                  sender_key=sender_key,
                  inbound_id=inbound_id,
                  response_id=response_id,
                  proposal_type=decision.proposal_type,
                  target_scope=proposal.scope.value,
                  proposal_key=decision.normalized_key,
                  proposed_value={
                      "value": decision.normalized_value,
                      "kind": proposal.kind.value,
                      "action": proposal.action.value,
                      "safe_summary": proposal.safe_summary,
                      "use_in_instructions": proposal.use_in_instructions,
                  },
                  proposed_text=proposal.safe_summary,
                  importance=proposal.importance,
                  confidence=proposal.confidence,
                  reason_code=proposal.reason_code,
                  sensitive_detected=decision.sensitive_detected,
                  status="pending",
                  rejection_codes=decision.rejection_codes,
                  metadata={"source": "agent_turn_envelope"},
                  idempotency_key=key,
              )
          except Exception as exc:
              print("[memory.service.persist_error]", {
                  "error_type": type(exc).__name__,
                  "error": str(exc)[:160],
              })
              continue

          if proposal_id is None:
              continue
          result.proposal_ids.append(proposal_id)
          result.proposals_persisted += 1

          if "duplicate" in decision.rejection_codes:
              mark_proposal_duplicate(proposal_id)
              result.proposals_duplicate += 1
              continue

          if not decision.accepted:
              mark_proposal_rejected(
                  proposal_id,
                  rejection_codes=decision.rejection_codes,
              )
              result.proposals_rejected += 1
              result.rejection_codes.extend(decision.rejection_codes)
              continue

          if decision.auto_apply and sender_key and decision.normalized_key:
              try:
                  if proposal.action == MemoryAction.forget:
                      forgotten = forget_contact_memory(
                          tenant_id=tenant_id,
                          sender_key=sender_key,
                          memory_key=decision.normalized_key,
                      )
                      mark_proposal_applied(proposal_id)
                      result.proposals_applied += 1
                      print("[memory.auto_apply.forget]", {
                          "sender_key": sender_key,
                          "memory_key": decision.normalized_key,
                          "forgotten": forgotten,
                          "proposal_id": proposal_id,
                      })
                  else:
                      # Explicit preferences should enter the next prompt compile.
                      use_in_instructions = bool(proposal.use_in_instructions)
                      if proposal.reason_code in {
                          "explicit_user_preference",
                          "explicit_user_correction",
                          "explicit_user_identity",
                          "do_not_ask_again",
                      }:
                          use_in_instructions = True
                      memory = upsert_contact_memory(
                          tenant_id=tenant_id,
                          sender_key=sender_key,
                          memory_key=decision.normalized_key,
                          memory_kind=proposal.kind.value,
                          value=decision.normalized_value,
                          safe_summary=proposal.safe_summary
                          or (
                              str(decision.normalized_value.get("state"))
                              if isinstance(decision.normalized_value, dict)
                              and "state" in decision.normalized_value
                              else str(decision.normalized_value)[:120]
                          ),
                          importance=proposal.importance,
                          confidence=proposal.confidence,
                          use_in_instructions=use_in_instructions,
                          source_inbound_id=inbound_id,
                          source_response_id=response_id,
                          expires_at=decision.expires_at,
                      )
                      mark_proposal_applied(
                          proposal_id,
                          applied_memory_id=memory.id,
                      )
                      result.proposals_applied += 1
                      print("[memory.auto_apply.upsert]", {
                          "sender_key": sender_key,
                          "memory_key": decision.normalized_key,
                          "memory_id": memory.id,
                          "kind": proposal.kind.value,
                          "proposal_id": proposal_id,
                      })
                      current = get_active_contact_memories(
                          tenant_id=tenant_id,
                          sender_key=sender_key,
                          limit=int(
                              getattr(settings, "agent_max_active_contact_memories", 20)
                          ),
                      )
                      try:
                          from .memory_consolidation import consolidate_contact_memories

                          consolidate_contact_memories(
                              tenant_id=tenant_id,
                              sender_key=sender_key,
                          )
                      except Exception as cons_exc:
                          print("[memory.consolidation.error]", {
                              "error_type": type(cons_exc).__name__,
                              "error": str(cons_exc)[:120],
                          })
              except Exception as exc:
                  print("[memory.service.apply_error]", {
                      "error_type": type(exc).__name__,
                      "error": str(exc)[:160],
                  })
                  mark_proposal_pending_review(proposal_id)
                  result.proposals_pending_review += 1
          else:
              mark_proposal_pending_review(proposal_id)
              result.proposals_pending_review += 1
              if (
                  bool(getattr(settings, "agent_memory_auto_apply_enabled", False))
                  and not decision.auto_apply
              ):
                  print("[memory.auto_apply.skipped]", {
                      "sender_key": sender_key,
                      "memory_key": decision.normalized_key,
                      "proposal_id": proposal_id,
                      "requires_review": decision.requires_review,
                  })

    if extensions_on:
        for ext in envelope.instruction_extension_proposals or []:
            result.proposals_seen += 1
            decision = evaluate_instruction_extension_proposal(proposal=ext)
            key = _idempotency_key(
                tenant_id=tenant_id,
                conversation_key=conversation_key,
                inbound_id=inbound_id,
                proposal_type="instruction_extension",
                normalized_key=decision.normalized_key,
                normalized_value=decision.normalized_value,
            )
            try:
                proposal_id = insert_memory_proposal(
                    tenant_id=tenant_id,
                    conversation_key=conversation_key,
                    sender_key=sender_key,
                    inbound_id=inbound_id,
                    response_id=response_id,
                    proposal_type="instruction_extension",
                    target_scope=ext.scope,
                    proposal_key=decision.normalized_key,
                    proposed_value={
                        "instruction": decision.normalized_value,
                        "category": ext.category,
                        "scope_key": ext.scope_key,
                        "evidence_summary": ext.evidence_summary,
                    },
                    proposed_text=str(decision.normalized_value or "")[:2000],
                    importance=ext.importance,
                    confidence=ext.confidence,
                    reason_code="persona_gap_detected",
                    sensitive_detected=decision.sensitive_detected,
                    status="pending",
                    rejection_codes=decision.rejection_codes,
                    metadata={"source": "agent_turn_envelope"},
                    idempotency_key=key,
                )
            except Exception as exc:
                print("[memory.service.ext_persist_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })
                continue
            if proposal_id is None:
                continue
            result.proposal_ids.append(proposal_id)
            result.proposals_persisted += 1
            if not decision.accepted:
                mark_proposal_rejected(
                    proposal_id,
                    rejection_codes=decision.rejection_codes,
                )
                result.proposals_rejected += 1
                result.rejection_codes.extend(decision.rejection_codes)
                continue
            try:
                created = create_extension_proposal(
                    tenant_id=tenant_id,
                    extension_key=str(decision.normalized_key),
                    instruction_text=str(decision.normalized_value),
                    category=ext.category,
                    scope=ext.scope,
                    scope_key=ext.scope_key,
                    importance=ext.importance,
                    confidence=ext.confidence,
                    proposed_by_inbound_id=inbound_id,
                    proposed_by_response_id=response_id,
                    metadata={"evidence_summary": ext.evidence_summary},
                )
                mark_proposal_pending_review(proposal_id)
                # Keep audit row pending; extension row is pending_review.
                result.proposals_pending_review += 1
                result.proposal_ids.append(int(created["id"]))
            except Exception as exc:
                print("[memory.service.ext_create_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })
                mark_proposal_pending_review(proposal_id)
                result.proposals_pending_review += 1

    if (
        summary_on
        and envelope.conversation_summary_delta is not None
        and conversation_key
    ):
        from .conversation_summary_policy import evaluate_summary_delta
        from .conversation_summary_repository import get_conversation_summary

        result.proposals_seen += 1
        delta = envelope.conversation_summary_delta
        existing_summary = get_conversation_summary(
            tenant_id=tenant_id,
            conversation_key=conversation_key,
        )
        apply_ok, cleaned_delta, summary_codes = evaluate_summary_delta(
            delta,
            existing=existing_summary,
        )
        if not apply_ok or cleaned_delta is None:
            print("[memory.service.summary_skipped]", {
                "reason": (summary_codes or ["rejected"])[0],
                "codes": summary_codes[:6],
                "conversation_key_present": True,
            })
            if summary_codes and summary_codes != ["criteria_not_met"]:
                result.rejection_codes.extend(summary_codes)
                result.proposals_rejected += 1
        else:
            key = _idempotency_key(
                tenant_id=tenant_id,
                conversation_key=conversation_key,
                inbound_id=inbound_id,
                proposal_type="summary_delta",
                normalized_key="summary",
                normalized_value=cleaned_delta.model_dump(mode="json"),
            )
            try:
                proposal_id = insert_memory_proposal(
                    tenant_id=tenant_id,
                    conversation_key=conversation_key,
                    sender_key=sender_key,
                    inbound_id=inbound_id,
                    response_id=response_id,
                    proposal_type="summary_delta",
                    target_scope="conversation",
                    proposal_key="summary",
                    proposed_value=cleaned_delta.model_dump(mode="json"),
                    importance=0.5,
                    confidence=0.5,
                    reason_code="conversation_commitment",
                    status="pending",
                    rejection_codes=summary_codes,
                    idempotency_key=key,
                )
                if proposal_id is not None:
                    result.proposal_ids.append(proposal_id)
                    result.proposals_persisted += 1
                    if summary_mode == "shadow":
                        from .conversation_summary_policy import (
                            compare_summary_delta_to_facts,
                        )

                        divergences = compare_summary_delta_to_facts(
                            cleaned_delta,
                            commercial_data=None,
                        )
                        print(
                            "[memory.service.summary_shadow]",
                            {
                                "divergences": divergences[:8],
                                "applied": False,
                                "injected": False,
                            },
                        )
                        # Shadow: do not mutate conversation summary / memory / persona.
                    else:
                        apply_summary_delta(
                            tenant_id=tenant_id,
                            conversation_key=conversation_key,
                            delta=cleaned_delta,
                            inbound_id=inbound_id,
                            response_id=response_id,
                            max_chars=int(
                                getattr(
                                    settings,
                                    "agent_max_conversation_summary_chars",
                                    2500,
                                )
                            ),
                        )
                        mark_proposal_applied(proposal_id)
                        result.proposals_applied += 1
                    if summary_codes:
                        print("[memory.service.summary_scrubbed]", {
                            "codes": summary_codes[:6],
                        })
            except Exception as exc:
                print("[memory.service.summary_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                })

    return result
