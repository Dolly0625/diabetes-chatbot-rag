from __future__ import annotations

"""OCR adapter — 收斂 image_bytes 5 別名為單一 ImageInput，純搬運自 runner.py。

設計：
- ImageInput dataclass 封裝 front/back bytes，對外保持兼容（**kwargs 透傳）
- _sanitize_ocr_meds / _merge_ocr_meds_into_intake_data / _process_ocr_images
  皆為純搬運，不改邏輯，僅抽模組以瘦身 runner.py
- 永不儲存 raw image 於 WorkflowState/trace，僅提取 meds 後合併至 intake_data
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ImageInput:
    """收斂 5 別名的單一輸入：front/back bytes。

    對外保持兼容：呼叫方可傳 image_bytes / image_bytes_front / image_bytes_back
    / front_image_bytes / back_image_bytes 任一組合，經 from_kwargs 收斂。
    """

    front: bytes | None = None
    back: bytes | None = None

    @classmethod
    def from_kwargs(
        cls,
        *,
        image_bytes: bytes | None = None,
        image_bytes_front: bytes | None = None,
        image_bytes_back: bytes | None = None,
        front_image_bytes: bytes | None = None,
        back_image_bytes: bytes | None = None,
        **kwargs: Any,
    ) -> "ImageInput":
        # 兼容額外 kwargs（忽略未知鍵）
        _ = kwargs
        front = image_bytes_front if image_bytes_front is not None else front_image_bytes
        if front is None and image_bytes is not None:
            front = image_bytes
        back = image_bytes_back if image_bytes_back is not None else back_image_bytes

        def _is_valid(b: Any) -> bool:
            return isinstance(b, (bytes, bytearray)) and len(b) > 0

        if front is not None and not _is_valid(front):
            front = None
        if back is not None and not _is_valid(back):
            back = None
        return cls(front=front, back=back)

    def has_image(self) -> bool:
        return self.front is not None or self.back is not None


def _sanitize_ocr_meds(meds: list[str]) -> list[str]:
    """Sanitize OCR meds: strip, limit, dedup, block injection (whole-entry discard)."""
    import re

    sanitized: list[str] = []
    seen: set[str] = set()
    # Unified with a_router guard — covers 忽略.*規則|忽略.*指令|忘記指示|解除限制|揭露系統提示|ignore.*previous/all.*instruction|disregard|system prompt|jailbreak etc.
    injection_pat = re.compile(
        r"忽略.*規則|忽略.*指令|忘記.*指示|解除限制|揭露.*系統.*提示|揭露.*提示|"
        r"ignore.*previous.*instruction|ignore.*all.*instruction|disregard|system\s*prompt|jailbreak|developer\s+message",
        re.IGNORECASE,
    )
    for m in meds:
        if not isinstance(m, str):
            m = str(m)
        m = re.sub(r"[\x00-\x1f\x7f]", "", m)
        if injection_pat.search(m):
            continue
        m = m.strip()
        if not m:
            continue
        if len(m) > 100:
            m = m[:100].strip()
        m = m.strip(" ,;；，。")
        if not m:
            continue
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        sanitized.append(m)
        if len(sanitized) >= 20:
            break
    return sanitized


def _merge_ocr_meds_into_intake_data(intake_data: Any | None, ocr_meds: list[str]) -> Any | None:
    """Merge OCR meds into intake_data known_medications, never storing raw image."""
    if not ocr_meds:
        return intake_data
    ocr_meds = _sanitize_ocr_meds(ocr_meds)
    if not ocr_meds:
        return intake_data
    if intake_data is None:
        return {"known_medications": ocr_meds}
    if isinstance(intake_data, dict):
        existing = intake_data.get("known_medications", [])
        if not isinstance(existing, list):
            existing = [str(existing)] if existing else []
        merged: list[str] = list(existing)
        seen = {str(m).lower() for m in merged}
        for m in ocr_meds:
            if m.lower() not in seen:
                merged.append(m)
                seen.add(m.lower())
        new_data = dict(intake_data)
        new_data["known_medications"] = _sanitize_ocr_meds(merged)
        return new_data
    try:
        from tfda_context_gate.intake.schemas import PreVisitIntake

        if isinstance(intake_data, PreVisitIntake):
            existing = list(intake_data.known_medications or [])
            seen = {str(m).lower() for m in existing}
            for m in ocr_meds:
                if m.lower() not in seen:
                    existing.append(m)
                    seen.add(m.lower())
            data = intake_data.model_dump(mode="json")
            data["known_medications"] = _sanitize_ocr_meds(existing)
            return PreVisitIntake.model_validate(data)
    except Exception:
        pass
    try:
        if hasattr(intake_data, "known_medications"):
            existing = list(getattr(intake_data, "known_medications") or [])
            seen = {str(m).lower() for m in existing}
            for m in ocr_meds:
                if m.lower() not in seen:
                    existing.append(m)
            setattr(intake_data, "known_medications", _sanitize_ocr_meds(existing))
            return intake_data
    except Exception:
        pass
    return intake_data


def _process_ocr_images(
    *,
    intake_data: Any | None,
    image_bytes: bytes | None = None,
    image_bytes_front: bytes | None = None,
    image_bytes_back: bytes | None = None,
    front_image_bytes: bytes | None = None,
    back_image_bytes: bytes | None = None,
    ocr_service: Any | None = None,
    trace: Any | None = None,
    image_input: ImageInput | None = None,
    **kwargs: Any,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Process front/back images via MedicationBagOCRService, merge into intake_data.

    Returns (new_intake_data, ocr_result). Never stores raw image in state.
    OCR results go through intake, not direct evidence.
    支援 ImageInput 或 5 別名 kwargs，對外保持兼容。
    """
    _ = kwargs
    if image_input is not None:
        front = image_input.front
        back = image_input.back
    else:
        inp = ImageInput.from_kwargs(
            image_bytes=image_bytes,
            image_bytes_front=image_bytes_front,
            image_bytes_back=image_bytes_back,
            front_image_bytes=front_image_bytes,
            back_image_bytes=back_image_bytes,
        )
        front = inp.front
        back = inp.back

    if front is None and back is None:
        return intake_data, None

    try:
        svc = ocr_service
        if svc is None:
            try:
                from tfda_context_gate.intake.qr_ocr_service import MedicationBagOCRService

                svc = MedicationBagOCRService()
            except Exception:
                return intake_data, None

        ocr_meds: list[str] = []
        ocr_result: dict[str, Any] | None = None

        try:
            if hasattr(svc, "extract_front_back"):
                ocr_result = svc.extract_front_back(front, back)  # type: ignore[attr-defined]
                ocr_meds = ocr_result.get("meds", []) if isinstance(ocr_result, dict) else []
            elif hasattr(svc, "process"):
                ocr_result = svc.process(front_bytes=front, back_bytes=back, image_bytes=front)  # type: ignore[attr-defined]
                if isinstance(ocr_result, dict):
                    ocr_meds = ocr_result.get("known_medications", []) or ocr_result.get("meds", [])
                elif isinstance(ocr_result, list):
                    ocr_meds = ocr_result
            elif hasattr(svc, "extract"):
                meds_all: list[str] = []
                for b in [front, back]:
                    if b is not None:
                        try:
                            r = svc.extract(b)  # type: ignore[attr-defined]
                            if isinstance(r, dict):
                                m = r.get("meds", []) or r.get("known_medications", [])
                                meds_all.extend(m)
                                if ocr_result is None:
                                    ocr_result = r
                            elif isinstance(r, list):
                                meds_all.extend(r)
                        except Exception:
                            continue
                ocr_meds = meds_all
                if ocr_result is None:
                    ocr_result = {"meds": meds_all, "known_medications": meds_all}
            elif hasattr(svc, "extract_medications"):
                meds_all = []
                for b in [front, back]:
                    if b is not None:
                        try:
                            m = svc.extract_medications(b)  # type: ignore[attr-defined]
                            if isinstance(m, list):
                                meds_all.extend(m)
                        except Exception:
                            continue
                ocr_meds = meds_all
                ocr_result = {"meds": meds_all, "known_medications": meds_all}
            else:
                ocr_meds = []
        except Exception:
            ocr_meds = []
            ocr_result = None

        ocr_meds = _sanitize_ocr_meds(ocr_meds if isinstance(ocr_meds, list) else [])

        if trace is not None:
            try:
                with trace.span("OCR", "medication_bag_ocr") as span:
                    span.set(
                        status="COMPLETED" if ocr_meds else "EMPTY",
                        ocr_meds=ocr_meds,
                        ocr_medication_count=len(ocr_meds),
                        ocr_front_provided=front is not None,
                        ocr_back_provided=back is not None,
                        ocr_confidence=ocr_result.get("confidence", 0) if isinstance(ocr_result, dict) else 0,
                        qr_used=ocr_result.get("qr_used", False) if isinstance(ocr_result, dict) else False,
                        ocr_used=ocr_result.get("ocr_used", False) if isinstance(ocr_result, dict) else False,
                        reason_codes=["OCR_EXTRACTED" if ocr_meds else "OCR_EMPTY"],
                    )
            except Exception:
                pass

        new_intake_data = _merge_ocr_meds_into_intake_data(intake_data, ocr_meds)

        if ocr_result is None:
            ocr_result = {"known_medications": ocr_meds, "meds": ocr_meds}
        elif isinstance(ocr_result, dict) and "known_medications" not in ocr_result:
            ocr_result["known_medications"] = ocr_meds
            if "meds" not in ocr_result:
                ocr_result["meds"] = ocr_meds

        return new_intake_data, ocr_result
    except Exception:
        if trace is not None:
            try:
                with trace.span("OCR", "medication_bag_ocr") as span:
                    span.set(status="ERROR", ocr_meds=[], reason_codes=["OCR_ERROR"])
            except Exception:
                pass
        return intake_data, None


def process_ocr_images(
    *,
    intake_data: Any | None,
    image_input: ImageInput | None = None,
    ocr_service: Any | None = None,
    trace: Any | None = None,
    **kwargs: Any,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Public wrapper: 支援 ImageInput 或 5 別名 kwargs。"""
    if image_input is not None:
        return _process_ocr_images(intake_data=intake_data, image_input=image_input, ocr_service=ocr_service, trace=trace, **kwargs)
    return _process_ocr_images(intake_data=intake_data, ocr_service=ocr_service, trace=trace, **kwargs)


__all__ = ["ImageInput", "_sanitize_ocr_meds", "_merge_ocr_meds_into_intake_data", "_process_ocr_images", "process_ocr_images"]
