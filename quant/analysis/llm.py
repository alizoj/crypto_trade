"""Опциональное краткое резюме сетапа через Claude API (раздел 8.3).

Резюме строится только поверх уже рассчитанных фактов (факторы score, вероятность, план, шаблонный
разбор) — модель не получает рыночных данных и не должна добавлять утверждений, которых нет во входе
(раздел 10). Включается флагом QUANT_LLM_SUMMARY_ENABLED; ключ — QUANT_ANTHROPIC_API_KEY или стандартные
учётные данные Anthropic SDK. При ошибке или отказе модели возвращается None — интерфейс показывает
только детерминированный разбор.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from quant.config import get_settings

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
SYSTEM = (
    "Ты помощник трейдера. Тебе дают JSON с уже рассчитанными фактами о торговом сетапе: факторы score "
    "за и против, вероятность и её доверительный интервал, EV, план сделки и шаблонный разбор. "
    "Напиши резюме на русском в 2–3 предложениях: главное за и против и чем рискует сделка. "
    "Используй только факты из JSON: не добавляй новых чисел, уровней, прогнозов, новостей или советов, "
    "которых там нет. Если вероятность не откалибрована или данных мало — скажи об этом прямо."
)


def summarize(facts: dict[str, Any], *, client: Any | None = None) -> str | None:
    settings = get_settings()
    if not settings.llm_summary_enabled:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("LLM-резюме включено, но пакет anthropic не установлен (pip install anthropic)")
        return None
    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                      # при отказе модели — серверный fallback
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)}],
        )
    except anthropic.RateLimitError:
        log.warning("LLM-резюме: превышен лимит запросов")
        return None
    except anthropic.APIStatusError as e:
        log.warning("LLM-резюме: ошибка API %s", e.status_code)
        return None
    except anthropic.APIConnectionError:
        log.warning("LLM-резюме: нет соединения с API")
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None
