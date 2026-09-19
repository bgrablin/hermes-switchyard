"""Backward-compatible API shim; registration belongs to hermes_switchyard."""

from hermes_switchyard.automatic import AutomaticSkillRecommender, build_pre_llm_call_hook
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.computer_use import StaleTargetError, run_computer_goal
from hermes_switchyard.routing import route_model, select_skill, select_skills

__all__ = (
    "AutomaticSkillRecommender",
    "DecisionClient",
    "StaleTargetError",
    "build_pre_llm_call_hook",
    "route_model",
    "run_computer_goal",
    "select_skill",
    "select_skills",
)
