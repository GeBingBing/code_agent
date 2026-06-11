"""A/B test hooks (PR-19, extracted from AgentEngine).

Two hooks:
  - `apply_variants`: BEFORE_LLM_CALL — substitute system-prompt
    sections per the user's experiment variant assignment. Records
    (exp_id, variant_id) on the payload so the observation hook can
    write a matching row at session end.
  - `record_observation`: ON_SESSION_END — read the in-flight list,
    write one ExperimentObservation per (exp_id, variant) with token
    usage and session duration.

Both hooks swallow exceptions — AB tests must never break the agent.
Originally `AgentEngine._ab_apply_variants_hook` and
`_ab_record_observation_hook`.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..governance.ab_test import ExperimentObservation


def resolve_ab_user_id(config, workspace) -> str:
    """Resolve a stable user identifier for AB bucketing.

    Priority:
      1. AgentConfig.ab_user_id (explicit override)
      2. USER / USERNAME / LOGNAME env var
      3. workspace path (same project → same bucket)
      4. fallback "anonymous"
    """
    explicit = getattr(config, "ab_user_id", "") or ""
    if explicit:
        return explicit
    for env_name in ("USER", "USERNAME", "LOGNAME"):
        val = os.environ.get(env_name)
        if val:
            return val
    try:
        return f"ws:{workspace.resolve()}"
    except Exception:
        return "anonymous"


class ABTestApplyHook:
    """Substitute system-prompt sections per active experiments."""

    def __init__(self, ab_test, user_id: str):
        self._ab_test = ab_test
        self._user_id = user_id

    async def __call__(self, payload: Any) -> Any:
        if self._ab_test is None or not isinstance(payload, dict):
            return payload
        try:
            system_prompt = payload.get("system")
            if not isinstance(system_prompt, str) or not system_prompt:
                return payload
            in_flight = payload.setdefault("_ab_experiments", [])
            for exp in list(self._ab_test.list()):
                if exp.status != "running":
                    continue
                if exp.target != "system_prompt":
                    continue
                if not exp.target_key:
                    continue
                variant = self._ab_test.assign_variant(exp.id, self._user_id)
                if variant is None:
                    continue
                marker = exp.target_key
                replacement = variant.config.get("new_content", "")
                if marker in system_prompt and replacement:
                    system_prompt = system_prompt.replace(marker, replacement, 1)
                if not any(e.get("experiment_id") == exp.id for e in in_flight):
                    in_flight.append(
                        {
                            "experiment_id": exp.id,
                            "variant_id": variant.id,
                            "variant_name": variant.name,
                        }
                    )
            payload["system"] = system_prompt
        except Exception:
            pass
        return payload


class ABTestRecordObservationHook:
    """Write one observation per in-flight experiment at session end."""

    def __init__(
        self,
        ab_test,
        user_id: str,
        get_task_start_ts,
        get_last_task,
        get_total_input_tokens,
        get_total_output_tokens,
    ):
        self._ab_test = ab_test
        self._user_id = user_id
        self._get_task_start_ts = get_task_start_ts
        self._get_last_task = get_last_task
        self._get_total_input_tokens = get_total_input_tokens
        self._get_total_output_tokens = get_total_output_tokens

    async def __call__(self, payload: Any) -> Any:
        if self._ab_test is None or not isinstance(payload, dict):
            return payload
        try:
            in_flight = payload.get("_ab_experiments", []) or []
            if not in_flight:
                return payload
            success = not bool(payload.get("error"))
            duration_ms = 0.0
            start_ts = self._get_task_start_ts()
            if start_ts is not None:
                duration_ms = (time.time() - start_ts) * 1000.0
            for info in in_flight:
                try:
                    obs = ExperimentObservation(
                        experiment_id=info["experiment_id"],
                        variant_id=info["variant_id"],
                        user_id=self._user_id,
                        task=self._get_last_task() or "",
                        success=success,
                        token_input=self._get_total_input_tokens(),
                        token_output=self._get_total_output_tokens(),
                        duration_ms=duration_ms,
                    )
                    self._ab_test.record_observation(obs)
                except Exception:
                    continue
        except Exception:
            pass
        return payload
