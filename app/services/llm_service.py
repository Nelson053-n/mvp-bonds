import json
from typing import Any

import httpx

from app.config import settings
from app.models import (
    AddInstrumentInput,
    InstrumentMetrics,
    ValidationResponse,
)
from app.prompts import SYSTEM_PROMPT, VALIDATION_PROMPT


class LLMService:
    def __init__(self) -> None:
        self.mode = settings.llm_mode.lower()

    async def validate_instrument(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        if self.mode == "openai" and settings.openai_api_key:
            return await self._openai_validate(payload)
        return self._stub_validate(payload)

    async def generate_comment(self, payload: InstrumentMetrics) -> str:
        if self.mode == "openai" and settings.openai_api_key:
            return await self._openai_comment(payload)
        return self._stub_comment(payload)

    def _stub_validate(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        ticker = payload.ticker.strip().upper()
        warnings: list[str] = []

        bond_markers = [
            "ОФЗ",
            "BOND",
            "RU000",
            "SU",
            "-20",
            "20",
            "21",
            "22",
            "23",
            "24",
            "25",
            "26",
            "27",
            "28",
            "29",
            "30",
        ]
        is_bond = any(marker in ticker for marker in bond_markers)
        instrument_type = "bond" if is_bond else "stock"

        if payload.quantity <= 0:
            warnings.append("Количество должно быть больше нуля.")

        if payload.purchase_price <= 0:
            warnings.append("Цена покупки должна быть больше нуля.")

        if instrument_type == "stock" and payload.purchase_price < 1:
            warnings.append("Для акции цена выглядит как доля, а не рубли.")

        return ValidationResponse(
            instrument_type=instrument_type,
            validated=not warnings,
            warnings=warnings,
        )

    def _stub_comment(self, payload: InstrumentMetrics) -> str:
        if payload.type == "bond":
            high_yield = (
                payload.market_yield is not None
                and payload.market_yield >= 12
            )
            below_nominal = payload.current_price < 100
            if high_yield and below_nominal:
                return (
                    "Доходность выше средней,"
                    " бумага торгуется ниже номинала."
                )
            if payload.profit >= 0:
                return (
                    "Позиция в плюсе,"
                    " параметры доходности выглядят стабильными."
                )
            return (
                "Позиция в минусе,"
                " цена чувствительна к рыночной доходности."
            )

        dividend_note = (
            "умеренная дивидендная доходность"
            if (payload.dividend_yield or 0) >= 5
            else "низкая дивидендная доходность"
        )
        if payload.profit >= 0:
            return f"Позиция в плюсе, {dividend_note}."
        return f"Позиция в просадке, {dividend_note}."

    async def _openai_validate(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        body = {
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": VALIDATION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"user_input": payload.model_dump()},
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        data = await self._openai_chat(body)
        parsed = self._extract_json(data)
        return ValidationResponse(**parsed)

    async def _openai_comment(self, payload: InstrumentMetrics) -> str:
        body = {
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload.model_dump(mode="json"),
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        data = await self._openai_chat(body)
        parsed = self._extract_json(data)
        return str(parsed.get("comment", ""))

    async def _openai_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            return response.json()

    def _extract_json(self, response: dict[str, Any]) -> dict[str, Any]:
        content = response["choices"][0]["message"]["content"]
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = str(content)
        return json.loads(text)


llm_service = LLMService()
