from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from tfda_context_gate.access_control import (
    ActorRole,
    AuthorizationStatus,
    FrontendPersona,
    InformationSource,
    PermissionScope,
)
from tfda_context_gate.clinical_safety import RiskSignalPolicy
from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session import ProductSession, ProductSessionRepository
from tfda_context_gate.product_session import ProductSessionConflict
from tfda_context_gate.product_session import WebhookEventIdentityMismatch
from tfda_context_gate.workflow import run_workflow
from tfda_context_gate.workflow.schemas import WorkflowResult
from tfda_context_gate.workflow.fallbacks import fallback_response

from .schemas import OrchestratorResult


WorkflowRunner = Callable[..., WorkflowResult]


class ConversationOrchestrator:
    """LINE 產品狀態編排；醫療決策仍完全交給固定 workflow。"""

    SELF_COMMANDS = {"為自己整理", "自己", "本人"}
    PROXY_COMMANDS = {"代家人整理", "幫家人整理", "家人"}
    PROXY_CONSENT_COMMANDS = {"已取得同意", "家人已同意", "同意"}
    CONFIRM_COMMANDS = {"確認", "確認完成", "提交"}
    START_INTAKE_COMMANDS = {"我要準備看診", "準備看診", "開始看診整理"}
    SHARE_COMMANDS = {"分享給醫護", "分享摘要"}
    SUMMARY_COMMANDS = {"查看看診摘要", "看診摘要", "查看摘要"}
    MODIFY_COMMANDS = {"修改看診資料", "修改資料"}
    PROXY_SUBJECT_SOURCE_COMMANDS = {"家人本人描述", "病患本人描述", "本人描述"}
    PROXY_OBSERVED_SOURCE_COMMANDS = {"我的觀察", "照護者觀察", "家屬觀察"}
    PAUSE_COMMANDS = {"暫停整理", "先不要填"}
    CANCEL_COMMANDS = {"不填了", "取消整理"}
    RESUME_COMMANDS = {"繼續整理", "繼續填寫", "回到看診整理"}
    INTAKE_FIELD_ORDER = (
        "known_medications", "allergies", "chronic_conditions", "family_history",
        "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor",
    )

    def __init__(
        self,
        repository: ProductSessionRepository,
        *,
        identity_hash_key: str,
        workflow_runner: WorkflowRunner = run_workflow,
        session_ttl: timedelta = timedelta(days=7),
        context_manager: ConversationContextManager | None = None,
    ) -> None:
        if len(identity_hash_key) < 16:
            raise ValueError("identity_hash_key must contain at least 16 characters")
        self.repository = repository
        self._hash_key = identity_hash_key.encode("utf-8")
        self.workflow_runner = workflow_runner
        self.session_ttl = session_ttl
        self.context_manager = context_manager or ConversationContextManager()
        self.risk_policy = RiskSignalPolicy()

    def handle_text(
        self,
        *,
        event_id: str,
        line_user_id: str,
        text: str,
    ) -> OrchestratorResult:
        principal_hash = self._hash(line_user_id)
        existing_event = self.repository.get_webhook_event(event_id)
        if existing_event is not None and existing_event.status == "COMPLETED" and existing_event.result:
            if existing_event.principal_id_hash != principal_hash:
                raise WebhookEventIdentityMismatch("webhook event belongs to another principal")
            return OrchestratorResult.model_validate({**existing_event.result, "replayed": True})
        claim_token = self.repository.claim_webhook_event(event_id, principal_hash)
        if claim_token is None:
            return OrchestratorResult(
                event_id=event_id,
                session_id=self._session_id(line_user_id),
                reply="此訊息正在處理中，請稍候。",
                status="PROCESSING",
                replayed=True,
            )

        try:
            session = self._load_or_create(line_user_id)
            try:
                result = self._process_text(session, text.strip())
            except ProductSessionConflict:
                latest = self.repository.get(session.session_id)
                if latest is None:
                    raise
                result = self._process_text(latest, text.strip())
            result = result.model_copy(update={"event_id": event_id})
            self.repository.complete_webhook_event(
                event_id, result.model_dump(mode="json"), claim_token=claim_token
            )
            return result
        except Exception:
            self.repository.fail_webhook_event(event_id, claim_token=claim_token)
            raise

    def handle_image(
        self,
        *,
        event_id: str,
        line_user_id: str,
        image_bytes: bytes,
        ocr_service: Any | None = None,
    ) -> OrchestratorResult:
        principal_hash = self._hash(line_user_id)
        existing_event = self.repository.get_webhook_event(event_id)
        if existing_event is not None and existing_event.status == "COMPLETED" and existing_event.result:
            if existing_event.principal_id_hash != principal_hash:
                raise WebhookEventIdentityMismatch("webhook event belongs to another principal")
            return OrchestratorResult.model_validate({**existing_event.result, "replayed": True})
        claim_token = self.repository.claim_webhook_event(event_id, principal_hash)
        if claim_token is None:
            return OrchestratorResult(event_id=event_id, session_id=self._session_id(line_user_id), reply="此圖片正在處理中，請稍候。", status="PROCESSING", replayed=True)
        try:
            session = self._load_or_create(line_user_id)
            previous_version = session.version
            if not self._is_intake_active(session):
                result = OrchestratorResult(event_id=event_id, session_id=session.session_id, reply="請先選擇「為自己整理」或「代家人整理」，再上傳藥袋。", status="NEEDS_AUTHORIZATION", intake_stage=session.intake_stage)
            else:
                workflow = self.workflow_runner(
                    {"request_id": f"{session.session_id}-img-v{previous_version + 1}", "schema_version": "a.v0.1", "user_raw_input": "我上傳藥袋供看診前整理", "declared_role": self._declared_role(session.actor_role), "language": "zh-TW"},
                    task_type="pre_visit_intake",
                    intake=session.intake_snapshot,
                    image_bytes=image_bytes,
                    ocr_service=ocr_service,
                )
                updates: dict[str, Any] = {"pending_question": workflow.question}
                if workflow.intake_snapshot is not None:
                    updates["intake_snapshot"] = PreVisitIntake.model_validate(workflow.intake_snapshot)
                if workflow.intake_stage is not None:
                    updates["intake_stage"] = workflow.intake_stage
                next_intake = PreVisitIntake.model_validate(
                    workflow.intake_snapshot or session.intake_snapshot
                )
                updates["pending_field"] = self._next_pending_field(next_intake)
                if workflow.status == "NEEDS_CONFIRMATION":
                    updates["status"] = "AWAITING_CONFIRMATION"
                new_stage = updates.get("intake_stage", session.intake_stage)
                stage_completed = session.intake_stage != new_stage
                reply_text = workflow.final_response
                if stage_completed and session.intake_stage in {"stage1", "stage2"}:
                    try:
                        checkpoint = self._stage_checkpoint(next_intake, session.intake_stage)
                        if checkpoint:
                            reply_text = f"{checkpoint}\n\n{reply_text}"
                    except Exception:
                        pass
                context = self.context_manager.append_turn(session.conversation_context, role="user", content="［藥袋圖片］")
                context = self.context_manager.append_turn(context, role="assistant", content=reply_text)
                if stage_completed and session.intake_stage in {"stage1", "stage2", "stage3"}:
                    context = self.context_manager.mark_stage_completed(
                        context, session.intake_stage, next_stage=new_stage
                    )
                context, _ = self.context_manager.compact(context, stage_completed=stage_completed)
                updates["conversation_context"] = context
                session = session.model_copy(update=updates, deep=True)
                session = self._sync_clinical_context(session)
                saved = self.repository.save(session, expected_version=previous_version)
                result = OrchestratorResult(event_id=event_id, session_id=saved.session_id, reply=reply_text, status=workflow.status, intake_stage=workflow.intake_stage)
            self.repository.complete_webhook_event(
                event_id, result.model_dump(mode="json"), claim_token=claim_token
            )
            return result
        except Exception:
            self.repository.fail_webhook_event(event_id, claim_token=claim_token)
            raise

    def _process_text(self, session: ProductSession, text: str) -> OrchestratorResult:
        previous_version = session.version
        context = self.context_manager.append_turn(
            session.conversation_context,
            role="user",
            content=text or "（空白訊息）",
        )
        session = session.model_copy(update={"conversation_context": context}, deep=True)

        risk = self.risk_policy.classify(text)
        cumulative_risk = self._merge_risk(session.system_risk_classification, risk.model_dump(mode="json"))
        context = self.context_manager.apply_structured_updates(
            session.conversation_context,
            {
                "system_risk_classification": cumulative_risk,
                "risk_flags": ["POSSIBLE_EMERGENCY"] if risk.level == "RED_FLAG" else [],
            },
        )
        session = session.model_copy(
            update={"conversation_context": context, "system_risk_classification": cumulative_risk},
            deep=True,
        )

        # 一旦同一 subject 出現明確紅旗，後續產品命令也不得把它洗回一般狀態。
        if cumulative_risk.get("level") == "RED_FLAG":
            reply = fallback_response("A_EMERGENCY")
            session = self._sync_clinical_context(session)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=False)
            saved = self.repository.save(
                session.model_copy(update={"conversation_context": context}, deep=True),
                expected_version=previous_version,
            )
            return OrchestratorResult(
                event_id="pending",
                session_id=saved.session_id,
                reply=reply,
                status="FALLBACK",
                intake_stage=saved.intake_stage,
            )

        # 資料來源是臨床摘要的一部分，必須寫進結構化 state，不能只留在自由文字。
        if session.actor_role is ActorRole.RELATED_PERSON:
            if any(value in text for value in self.PROXY_SUBJECT_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.SUBJECT_REPORTED_VIA_PROXY})
            elif any(value in text for value in self.PROXY_OBSERVED_SOURCE_COMMANDS):
                session = session.model_copy(update={"information_source": InformationSource.PROXY_OBSERVED})

        command_result = self._handle_product_command(session, text)
        if command_result is not None:
            session, reply, status = command_result
            intake_stage = session.intake_stage
        else:
            if self._is_intake_active(session, text) and self._looks_like_side_question(session, text):
                workflow = self.workflow_runner({
                    "request_id": f"{session.session_id}-side-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": text,
                    "declared_role": self._declared_role(session.actor_role),
                    "language": "zh-TW",
                })
                reply = self._without_intake_invitation(workflow.final_response)
                pending_question = session.pending_question or self._question_for_field(
                    session.pending_field or self._next_pending_field(session.intake_snapshot)
                )
                if pending_question:
                    reply = (
                        f"{reply}\n\n資料已保留，想繼續可點「繼續整理」：\n{pending_question}"
                    )
                context = self.context_manager.append_turn(
                    session.conversation_context, role="assistant", content=reply
                )
                context, _ = self.context_manager.compact(context, stage_completed=False)
                saved = self.repository.save(
                    session.model_copy(update={"conversation_context": context}, deep=True),
                    expected_version=previous_version,
                )
                return OrchestratorResult(
                    event_id="pending", session_id=saved.session_id, reply=reply,
                    status="SIDE_ANSWER", intake_stage=saved.intake_stage,
                )

            old_stage = session.intake_stage
            intake_note: str | None = None
            workflow_text = text
            if self._is_intake_active(session, text) and session.status == "ACTIVE":
                session, intake_note = self._normalize_intake_answer(session, text)
                # 「不知道／沒有／跳過」已先寫入結構化 intake；不要再把這類短句
                # 丟給醫療 intent router，否則容易被誤判為無法回答的請求。
                if intake_note:
                    workflow_text = "我要繼續整理看診前資料"
            workflow = self.workflow_runner(
                {
                    "request_id": f"{session.session_id}-v{previous_version + 1}",
                    "schema_version": "a.v0.1",
                    "user_raw_input": workflow_text,
                    "declared_role": self._declared_role(session.actor_role),
                    "language": "zh-TW",
                },
                task_type="pre_visit_intake" if session.status in {"ACTIVE", "AWAITING_CONFIRMATION"} and self._is_intake_active(session, text) else None,
                intake=session.intake_snapshot if self._is_intake_active(session, text) else None,
            )
            updates: dict[str, Any] = {
                "pending_question": workflow.question,
                "system_risk_classification": self._merge_risk(
                    session.system_risk_classification,
                    workflow.system_risk_classification or risk.model_dump(mode="json"),
                ),
            }
            if workflow.intake_snapshot is not None:
                updates["intake_snapshot"] = PreVisitIntake.model_validate(workflow.intake_snapshot)
            if workflow.intake_stage is not None:
                updates["intake_stage"] = workflow.intake_stage
            resulting_intake = PreVisitIntake.model_validate(
                workflow.intake_snapshot or session.intake_snapshot
            )
            updates["pending_field"] = self._next_pending_field(resulting_intake)
            if workflow.status == "NEEDS_CONFIRMATION":
                updates["status"] = "AWAITING_CONFIRMATION"
            session = session.model_copy(update=updates, deep=True)
            reply, status, intake_stage = workflow.final_response, workflow.status, workflow.intake_stage
            stage_completed = old_stage != session.intake_stage
            checkpoint: str | None = None
            if stage_completed and old_stage in {"stage1", "stage2"}:
                checkpoint = self._stage_checkpoint(resulting_intake, old_stage)
            if intake_note and checkpoint:
                if intake_note.strip() == reply.strip():
                    reply = intake_note
                else:
                    reply = f"{intake_note}\n\n{checkpoint}\n\n{reply}"
            elif intake_note:
                if intake_note.strip() == reply.strip():
                    reply = intake_note
                elif intake_note.strip() in reply:
                    reply = reply
                else:
                    reply = f"{intake_note}\n\n{reply}"
            elif checkpoint:
                reply = f"{checkpoint}\n\n{reply}"
            session = self._sync_clinical_context(session)
            if stage_completed and old_stage in {"stage1", "stage2", "stage3"}:
                session = session.model_copy(update={
                    "conversation_context": self.context_manager.mark_stage_completed(
                        session.conversation_context,
                        old_stage,
                        next_stage=session.intake_stage,
                    )
                }, deep=True)
            context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
            context, _ = self.context_manager.compact(context, stage_completed=stage_completed)
            session = session.model_copy(update={"conversation_context": context}, deep=True)
            saved = self.repository.save(session, expected_version=previous_version)
            return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status=status, intake_stage=intake_stage)

        session = self._sync_clinical_context(session)
        context = self.context_manager.append_turn(session.conversation_context, role="assistant", content=reply)
        context, _ = self.context_manager.compact(context, stage_completed=False)
        session = session.model_copy(update={"conversation_context": context}, deep=True)
        saved = self.repository.save(session, expected_version=previous_version)
        return OrchestratorResult(event_id="pending", session_id=saved.session_id, reply=reply, status=status, intake_stage=intake_stage)

    _PROXY_FUZZY_RE = re.compile(r"幫.{0,10}問|代.{0,10}整理|幫.{0,10}整理|替.{0,10}問", re.IGNORECASE)
    _UNCERTAIN_RE = re.compile(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚|不太知道", re.IGNORECASE)

    @classmethod
    def _is_proxy_intent(cls, text: str) -> bool:
        if cls._PROXY_FUZZY_RE.search(text):
            return True
        if "幫" in text and "問" in text:
            return True
        if ("代" in text or "幫" in text) and "整理" in text:
            return True
        return False

    def _handle_product_command(self, session: ProductSession, text: str) -> tuple[ProductSession, str, str] | None:
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and (text in self.START_INTAKE_COMMANDS or any(token in text for token in ("準備看診", "回診", "看醫生")))
        ):
            return session, "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。", "NEEDS_ROLE_SELECTION"
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and self._is_proxy_intent(text)
        ):
            if self.risk_policy.classify(text).level == "RED_FLAG":
                return None
            subject_hash = self._hash(f"{session.session_id}:proxy-subject")
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.RELATED_PERSON
                or session.subject_id_hash not in {None, subject_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.RELATED_PERSON,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": subject_hash,
                "information_source": InformationSource.PROXY_OBSERVED,
                "pending_field": None,
                "pending_question": "請先確認：是否已取得家人同意，由您代為整理這份看診資料？",
            }, deep=True)
            return session, session.pending_question or "請確認家人同意。", "NEEDS_AUTHORIZATION"
        if (
            (session.authorization_status is AuthorizationStatus.UNVERIFIED or session.status == "CLOSED")
            and not self._is_intake_active(session)
            and self._UNCERTAIN_RE.search(text)
            and self.risk_policy.classify(text).level != "RED_FLAG"
        ):
            return session, "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。", "NEEDS_ROLE_SELECTION"
        if text in self.SHARE_COMMANDS:
            if session.status != "SUBMITTED":
                return session, "請先完成看診摘要的 Review & Confirm，才能分享給醫護。", "NEEDS_CONFIRMATION"
            return session, "摘要已可分享。請開啟「分享給醫護」頁面建立一次性短效連結。", "READY_TO_SHARE"
        if text == "我要上傳藥袋":
            return session, "請直接傳送藥袋照片；建議正面、背面各拍一張，文字保持清楚。", "AWAITING_IMAGE"
        if text in self.PAUSE_COMMANDS and self._is_intake_active(session):
            session = session.model_copy(update={"status": "PAUSED"})
            try:
                from tfda_context_gate.intake.tool import format_stage_progress

                progress = format_stage_progress(session.intake_snapshot)
                if progress and "第" not in progress:
                    return session, f"好的，已先暫停；目前資料會保留，不用重新填。你可以先問其他問題，想回來時點「繼續整理」即可。\n{progress}"[:60], "PAUSED"
            except Exception:
                pass
            return session, "好的，已先暫停；目前資料會保留，不用重新填。你可以先問其他問題，想回來時點「繼續整理」即可。", "PAUSED"
        if text in self.CANCEL_COMMANDS and self._is_intake_active(session):
            reset = self._new_subject_state(session, text)
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.PATIENT,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": None,
                "information_source": None,
                "status": "CLOSED",
            }, deep=True)
            return session, "已結束這次看診資料整理，尚未提交的內容已清除。需要時可再輸入「準備看診」重新開始。", "CANCELLED"
        if text in self.RESUME_COMMANDS and session.authorization_status in {
            AuthorizationStatus.PATIENT_SELF,
            AuthorizationStatus.AUTHORIZED_CAREGIVER,
            AuthorizationStatus.LEGAL_GUARDIAN,
        } and session.status == "PAUSED":
            pending_field = session.pending_field or self._next_pending_field(session.intake_snapshot)
            question = session.pending_question or self._question_for_field(pending_field)
            session = session.model_copy(update={"status": "ACTIVE", "pending_field": pending_field, "pending_question": question})
            base = question or "看診資料已經整理完成，請查看摘要。"
            try:
                from tfda_context_gate.intake.tool import format_stage_progress

                progress = format_stage_progress(session.intake_snapshot)
                if progress and "第" not in progress and "皆已完成" not in progress:
                    return session, f"{progress}\n\n{base}"[:60], "NEEDS_CLARIFICATION"
                if progress and "皆已完成" in progress:
                    return session, f"{progress}\n\n{base}"[:60], "NEEDS_CLARIFICATION"
            except Exception:
                pass
            return session, base, "NEEDS_CLARIFICATION"
        if text in {"使用說明與緊急協助", "使用說明"}:
            return session, "本系統提供糖尿病衛教與看診前整理，不是診斷或急診服務；若有呼吸困難、意識不清等緊急狀況，請立即聯絡當地緊急醫療服務。", "INFORMATION"
        if text in self.SUMMARY_COMMANDS:
            allowed = {PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.VIEW_PROXY_SUMMARY}
            if not allowed.intersection(session.permission_scopes):
                return session, "目前沒有權限查看這份摘要，請先完成身分與授權確認。", "FORBIDDEN"
            from tfda_context_gate.intake.summary import generate_previsit_summary
            summary = generate_previsit_summary(session.intake_snapshot, request_id=session.session_id)
            from tfda_context_gate.d_output_gate.gate import run_previsit_output_gate
            gate = run_previsit_output_gate({
                "request_id": session.session_id,
                "schema_version": "d.v0.1",
                "policy": {
                    "router_status": "G_GENERAL_EDUCATION",
                    "rag_allowed": True,
                    "risk_flags": [],
                    "intent_tags": [],
                    "reason_codes": ["PRODUCT_SUMMARY_REVIEW"],
                },
                "b_result": None,
                "c_result": summary.model_dump(mode="json"),
            })
            if gate.decision != "PASS":
                return session, gate.final_response, "FALLBACK"
            return session, f"目前看診摘要：\n{gate.final_response}\n\n尚缺：{'、'.join(summary.missing_fields) or '無'}", "SUMMARY"
        if text in self.MODIFY_COMMANDS:
            if not {PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.VIEW_PROXY_SUMMARY}.intersection(session.permission_scopes):
                return session, "目前沒有可修改的看診資料。", "FORBIDDEN"
            return session, "請選擇要修改的部分：用藥與病史、症狀、想問醫師的問題。", "NEEDS_MODIFICATION_SELECTION"
        modification = {
            "修改用藥與病史": ("stage1", {"known_medications": [], "allergies": [], "chronic_conditions": [], "family_history": []}),
            "修改症狀": ("stage2", {"symptom_onset": None, "symptom_description": None, "symptom_severity": None}),
            "修改想問醫師的問題": ("stage3", {"questions_for_doctor": []}),
        }.get(text)
        if modification is not None:
            stage, reset = modification
            intake = session.intake_snapshot.model_copy(update=reset, deep=True)
            pending_field = self._next_pending_field(intake)
            question = self._question_for_field(pending_field)
            session = session.model_copy(update={"intake_snapshot": intake, "intake_stage": stage, "status": "ACTIVE", "pending_field": pending_field, "pending_question": question}, deep=True)
            return session, question, "NEEDS_CLARIFICATION"
        if text in self.SELF_COMMANDS:
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.PATIENT
                or session.subject_id_hash not in {None, session.principal_id_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.PATIENT,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.PATIENT_SELF,
                "permission_scopes": [PermissionScope.CREATE_OWN_INTAKE, PermissionScope.VIEW_OWN_SUMMARY, PermissionScope.SHARE_OWN_SUMMARY],
                "subject_id_hash": session.principal_id_hash,
                "information_source": InformationSource.SELF_REPORTED,
                "intake_stage": "stage1",
                "status": "ACTIVE",
                "pending_field": "known_medications",
                "pending_question": self._question_for_field("known_medications"),
            }, deep=True)
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if text in self.PROXY_COMMANDS:
            subject_hash = self._hash(f"{session.session_id}:proxy-subject")
            reset_subject = (
                session.status == "SUBMITTED"
                or session.actor_role is not ActorRole.RELATED_PERSON
                or session.subject_id_hash not in {None, subject_hash}
            )
            reset = self._new_subject_state(session, text) if reset_subject else {}
            session = session.model_copy(update={
                **reset,
                "actor_role": ActorRole.RELATED_PERSON,
                "frontend_persona": FrontendPersona.PATIENT_FAMILY,
                "authorization_status": AuthorizationStatus.UNVERIFIED,
                "permission_scopes": [],
                "subject_id_hash": subject_hash,
                "information_source": InformationSource.PROXY_OBSERVED,
                "pending_field": None,
                "pending_question": "請先確認：是否已取得家人同意，由您代為整理這份看診資料？",
            }, deep=True)
            return session, session.pending_question or "請確認家人同意。", "NEEDS_AUTHORIZATION"
        if session.actor_role is ActorRole.RELATED_PERSON and text in self.PROXY_CONSENT_COMMANDS:
            session = session.model_copy(update={
                "authorization_status": AuthorizationStatus.AUTHORIZED_CAREGIVER,
                "permission_scopes": [PermissionScope.CREATE_PROXY_INTAKE, PermissionScope.VIEW_PROXY_SUMMARY, PermissionScope.SHARE_PROXY_SUMMARY],
                "pending_field": None,
                "pending_question": "這些資料主要是家人本人描述，還是您的觀察？",
                "intake_stage": "stage1",
            }, deep=True)
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if (
            session.actor_role is ActorRole.RELATED_PERSON
            and session.authorization_status is AuthorizationStatus.AUTHORIZED_CAREGIVER
            and text in self.PROXY_SUBJECT_SOURCE_COMMANDS.union(self.PROXY_OBSERVED_SOURCE_COMMANDS)
        ):
            session = session.model_copy(update={
                "pending_field": "known_medications",
                "pending_question": self._question_for_field("known_medications"),
            })
            return session, session.pending_question or "請提供看診資料。", "NEEDS_CLARIFICATION"
        if session.status == "AWAITING_CONFIRMATION" and text in self.CONFIRM_COMMANDS:
            session = session.model_copy(update={"status": "SUBMITTED", "intake_stage": "submitted", "pending_field": None, "pending_question": None}, deep=True)
            return session, "看診前資料已確認完成。您現在可以選擇「分享給醫護」。", "SUBMITTED"
        return None

    def _new_subject_state(self, session: ProductSession, command: str) -> dict[str, Any]:
        """切換資料主體時清除上一位 subject 的健康資料與近期對話。"""
        context = self.context_manager.create(session.session_id)
        context = self.context_manager.append_turn(context, role="user", content=command)
        return {
            "conversation_context": context,
            "intake_snapshot": PreVisitIntake(),
            "intake_stage": "stage1",
            "pending_field": None,
            "pending_question": None,
            "system_risk_classification": None,
            "status": "ACTIVE",
        }

    def _sync_clinical_context(self, session: ProductSession) -> ProductSession:
        """將已通過 intake／policy 的事實同步到不可壓縮 clinical state。"""
        intake = session.intake_snapshot
        stage = session.intake_stage
        updates: dict[str, Any] = {
            "known_medications": list(intake.known_medications),
            "allergies": list(intake.allergies),
            "chronic_conditions": list(intake.chronic_conditions),
            "family_history": list(intake.family_history),
            "symptom_onset": intake.symptom_onset,
            "symptom_description": intake.symptom_description,
            "reported_severity": intake.symptom_severity,
            "questions_for_doctor": list(intake.questions_for_doctor),
            "pending_question": session.pending_question[:2_000] if session.pending_question else None,
            "current_stage": stage,
            "authorization_status": session.authorization_status,
        }
        if session.system_risk_classification is not None:
            updates["system_risk_classification"] = session.system_risk_classification
        context = self.context_manager.apply_structured_updates(session.conversation_context, updates)
        return session.model_copy(update={"conversation_context": context}, deep=True)

    @classmethod
    def _next_pending_field(cls, intake: PreVisitIntake) -> str | None:
        for field in cls.INTAKE_FIELD_ORDER:
            if not getattr(intake, field, None):
                return field
        return None

    @staticmethod
    def _question_for_field(field: str | None) -> str | None:
        if field is None:
            return None
        from tfda_context_gate.intake.schemas import INTAKE_FIELD_QUESTIONS
        return INTAKE_FIELD_QUESTIONS.get(field)

    @staticmethod
    def _field_stage(field: str | None) -> str:
        if field in {"known_medications", "allergies", "chronic_conditions", "family_history"}:
            return "stage1"
        if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
            return "stage2"
        return "stage3"

    @classmethod
    def _normalize_intake_answer(
        cls, session: ProductSession, text: str
    ) -> tuple[ProductSession, str | None]:
        """將不知道、無、跳過與單欄自然語句轉成明確結構資料。"""
        field = session.pending_field or cls._next_pending_field(session.intake_snapshot)
        if field is None:
            return session, None
        try:
            from tfda_context_gate.intake.tool import INJECTION_FIXED_REPLY, is_injection_attempt

            if is_injection_attempt(text):
                return session, INJECTION_FIXED_REPLY
        except Exception:
            pass
        try:
            from tfda_context_gate.intake.tool import is_plausible_intake_value

            if not is_plausible_intake_value(text):
                pending_q = session.pending_question or cls._question_for_field(field)
                if pending_q:
                    return session, pending_q
                return session, "請再說明一次？"
        except Exception:
            pass
        _was_truncated = False
        _trunc_marker = "(已節錄)"
        try:
            from tfda_context_gate.intake.tool import INTAKE_MAX_LENGTH

            limit = INTAKE_MAX_LENGTH
        except Exception:
            limit = 120
        stripped_for_len = text.strip()
        if len(stripped_for_len) > limit:
            _was_truncated = True
            text = stripped_for_len[:limit]

        candidates: dict[str, Any] = {}
        try:
            from tfda_context_gate.intake.tool import PreVisitIntakeTool

            tool = PreVisitIntakeTool()
            candidates = tool.extract_fields_from_utterance(text, stage=None)
            if "questions_for_doctor" in candidates:
                has_q = bool(re.search(r"想問|想請問|想了解|問題是|疑問|？|\?|嗎|如何|怎麼|為何|為什麼", text))
                if not has_q:
                    candidates.pop("questions_for_doctor", None)
            if "chronic_conditions" in candidates and "symptom_description" in candidates:
                desc_val = str(candidates["symptom_description"])
                distinct = any(kw in desc_val for kw in ["口渴", "頻尿", "頭暈", "疲倦", "喘", "疼痛", "麻", "視力", "血糖"])
                if not distinct and any(kw in desc_val for kw in ["高血壓", "高血脂", "高脂血", "腎臟病", "心臟病"]):
                    candidates.pop("symptom_description", None)
        except Exception:
            candidates = {}

        intake = session.intake_snapshot.model_copy(deep=True)

        def _is_placeholder(fname: str, val: Any) -> bool:
            if isinstance(val, list) and val == ["不清楚（待看診確認）"]:
                return True
            if isinstance(val, str) and val in {"待確認", "不清楚（待看診確認）"}:
                return True
            return False

        valid: dict[str, Any] = {}
        for k, v in candidates.items():
            if k not in cls.INTAKE_FIELD_ORDER or not v:
                continue
            existing = getattr(intake, k, None)
            is_symptom = k in {"symptom_onset", "symptom_description", "symptom_severity"}
            if not existing or _is_placeholder(k, existing) or is_symptom:
                if isinstance(v, list):
                    tv = [str(x).strip()[:limit] for x in v]
                    v = tv
                elif isinstance(v, str) and len(v) > limit:
                    v = v[:limit]
                if k == "symptom_description" and "symptom_onset" in candidates and str(v).strip() == text.strip()[:limit]:
                    continue
                valid[k] = v

        if valid:
            for f, val in valid.items():
                setattr(intake, f, val)
            from tfda_context_gate.intake.tool import build_implicit_confirm, build_implicit_confirm_for_fields

            label_map = {
                "known_medications": "用藥",
                "allergies": "過敏",
                "chronic_conditions": "慢性病",
                "family_history": "家族史",
                "symptom_onset": "症狀開始時間",
                "symptom_description": "症狀描述",
                "symptom_severity": "程度",
                "questions_for_doctor": "想問醫師的問題",
            }
            raw_snip = text.strip()[:30]
            if field not in valid or len(valid) > 1:
                if len(valid) == 1:
                    f = next(iter(valid))
                    label = label_map.get(f, f)
                    confirm = f"你說的「{raw_snip}」我記在「{label}」"
                else:
                    base = build_implicit_confirm_for_fields(valid, raw_text=text)
                    labels = "、".join(label_map.get(k, k) for k in valid)
                    if base:
                        confirm = f"{base}（已分別記在「{labels}」）"
                    else:
                        norm_parts = []
                        for vv in valid.values():
                            if isinstance(vv, list):
                                norm_parts.append("、".join(str(x) for x in vv))
                            else:
                                norm_parts.append(str(vv))
                        confirm = build_implicit_confirm(text, "；".join(norm_parts))
            else:
                confirm = build_implicit_confirm_for_fields(valid, raw_text=text)
                if confirm is None:
                    first_val = next(iter(valid.values()))
                    norm = "、".join(str(x) for x in first_val) if isinstance(first_val, list) else str(first_val)
                    confirm = build_implicit_confirm(text, norm)
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm

        # F1-R1/R2: candidates hit already-filled non-symptom field -> don't pollute pending
        if candidates and not valid:
            pending_q = session.pending_question or cls._question_for_field(field)
            if pending_q:
                return session, pending_q
            return session, None

        normalized = re.sub(r"\s+", "", text).lower()
        uncertain = bool(re.search(r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚", normalized) or "不太知道" in normalized)
        skip = normalized in {"跳過", "略過", "先跳過", "稍後再補", "還沒想到"}
        none_answer = normalized in {"無", "沒有", "目前沒有", "沒有喔", "沒有欸", "沒吃", "沒有吃"}
        none_patterns = {
            "known_medications": r"(?:沒有|無).*(?:用藥|吃藥|藥物|藥)",
            "allergies": r"(?:沒有|無).*過敏|無過敏",
            "chronic_conditions": r"(?:沒有|無).*(?:慢性病|其他疾病)|無慢性病",
            "family_history": r"(?:沒有|無).*家族史|家族(?:沒有|無)",
        }
        none_answer = none_answer or bool(re.search(none_patterns.get(field, r"(?!x)x"), normalized))

        if (uncertain or skip) and not valid:
            if field in {"symptom_onset", "symptom_description", "symptom_severity"}:
                setattr(intake, field, "待確認")
                return session.model_copy(update={"intake_snapshot": intake}, deep=True), (
                    "沒關係，先記為『待確認』，看診時再跟醫師確認。"
                )
            value: Any = ["不清楚（待看診確認）"] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"
            } else "不清楚（待看診確認）"
            setattr(intake, field, value)
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), (
                "沒關係，我先把這一項標成「待看診確認」，不會替你猜。"
            )

        if none_answer:
            value = ["無"] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history"
            } else (["目前沒有特別想問的問題"] if field == "questions_for_doctor" else "目前沒有")
            setattr(intake, field, value)
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), "好，已記下目前沒有。"

        if text.strip():
            stripped = text.strip()
            if len(stripped) < 2 or re.fullmatch(r"[^\w\u4e00-\u9fa5]+", stripped) or not re.search(r"[\w\u4e00-\u9fa5]", stripped) or re.search(r"[#\/\*]{3,}", stripped):
                pending_q = session.pending_question or cls._question_for_field(field)
                if pending_q:
                    return session, pending_q
                return session, None
            direct: Any = [stripped[:limit]] if field in {
                "known_medications", "allergies", "chronic_conditions", "family_history", "questions_for_doctor"
            } else stripped[:limit]
            setattr(intake, field, direct)
            if isinstance(direct, list):
                norm_str = "、".join(str(x) for x in direct)
            else:
                norm_str = str(direct)
            from tfda_context_gate.intake.tool import build_implicit_confirm

            confirm = build_implicit_confirm(text, norm_str)
            if _was_truncated and _trunc_marker not in confirm:
                confirm = f"{confirm} {_trunc_marker}"
            return session.model_copy(update={"intake_snapshot": intake}, deep=True), confirm
        return session, None

    @classmethod
    def _looks_like_side_question(cls, session: ProductSession, text: str) -> bool:
        pending_field = session.pending_field or cls._next_pending_field(session.intake_snapshot)
        if pending_field == "questions_for_doctor":
            return False
        normalized = re.sub(r"\s+", "", text)
        education_topic = re.search(
            r"請說明|想了解|什麼是|為什麼|怎麼吃|如何|飲食原則|運動原則|藥物.*作用|血糖.*標準",
            normalized,
        )
        return bool(education_topic or (normalized.endswith(("?", "？")) and len(normalized) >= 6))

    @staticmethod
    def _stage_checkpoint(intake: PreVisitIntake, stage: str) -> str | None:
        def show(value: Any) -> str:
            if isinstance(value, list):
                return "、".join(str(item) for item in value) or "未填"
            return str(value or "未填")

        if stage == "stage1":
            base = (
                "用藥與病史已記下："
                f"用藥 {show(intake.known_medications)}；過敏 {show(intake.allergies)}；"
                f"慢性病 {show(intake.chronic_conditions)}；家族史 {show(intake.family_history)}。"
            )
        elif stage == "stage2":
            base = (
                "症狀資訊已記下："
                f"開始時間 {show(intake.symptom_onset)}；主要狀況 {show(intake.symptom_description)}；"
                f"程度 {show(intake.symptom_severity)}。"
            )
        else:
            base = None
        try:
            from tfda_context_gate.intake.tool import format_stage_progress

            progress = format_stage_progress(intake)
            if progress and "第" not in progress:
                if base:
                    return f"{base}\n{progress}"[:60]
                if stage in {"stage1", "stage2", "stage3"}:
                    return progress
        except Exception:
            pass
        return base

    @staticmethod
    def _without_intake_invitation(reply: str) -> str:
        marker = "\n\n如果要看醫生需要幫你整理嗎？"
        return reply.split(marker, 1)[0].strip()

    @staticmethod
    def _merge_risk(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
        """RED_FLAG 在同一 subject session 內為單調狀態，不可被安全訊息降級。"""
        previous = dict(existing or {})
        current = dict(incoming or {})
        if previous.get("level") != "RED_FLAG":
            return current or previous
        if current.get("level") != "RED_FLAG":
            return previous
        merged = dict(previous)
        merged["signals"] = list(dict.fromkeys([
            *list(previous.get("signals") or []),
            *list(current.get("signals") or []),
        ]))
        merged["action"] = "URGENT_HUMAN"
        return merged

    def _load_or_create(self, line_user_id: str) -> ProductSession:
        session_id = self._session_id(line_user_id)
        existing = self.repository.get(session_id)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        principal_hash = self._hash(line_user_id)
        return self.repository.create(ProductSession(
            session_id=session_id,
            principal_id_hash=principal_hash,
            conversation_context=self.context_manager.create(session_id),
            created_at=now,
            updated_at=now,
            expires_at=now + self.session_ttl,
        ))

    def _hash(self, value: str) -> str:
        return hmac.new(self._hash_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def principal_hash(self, external_id: str) -> str:
        return self._hash(external_id)

    def session_for_user(self, line_user_id: str) -> ProductSession | None:
        return self.repository.get(self._session_id(line_user_id))

    def _session_id(self, line_user_id: str) -> str:
        return f"line-{self._hash(line_user_id)[:32]}"

    @staticmethod
    def _declared_role(role: ActorRole) -> str:
        if role is ActorRole.RELATED_PERSON:
            return "CAREGIVER"
        if role is ActorRole.PRACTITIONER:
            return "HEALTHCARE_PROFESSIONAL"
        return "PATIENT"

    @staticmethod
    def _is_intake_active(session: ProductSession, text: str = "") -> bool:
        authorized = session.authorization_status in {
            AuthorizationStatus.PATIENT_SELF,
            AuthorizationStatus.AUTHORIZED_CAREGIVER,
            AuthorizationStatus.LEGAL_GUARDIAN,
        }
        return authorized and session.status in {"ACTIVE", "PAUSED", "AWAITING_CONFIRMATION"}
