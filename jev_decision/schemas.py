"""Model-facing schemas for the bounded Jev decision plugin."""

from .routing import (
    DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    DEFAULT_SKILL_NEEDS_THRESHOLD,
    DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
)

_HOTKEYS = [
    "SUBMIT", "CANCEL", "SAVE", "UNDO", "REDO", "SELECT_ALL", "COPY", "FIND",
    "NEXT_TAB", "PREVIOUS_TAB", "NEW_TAB", "BOLD", "ITALIC", "UNDERLINE",
]

_ACKNOWLEDGEMENT = {
    "type": "boolean",
    "default": False,
    "description": (
        "Required true acknowledgement that all state sent to a model is public or already sanitized. "
        "This is a caller attestation, not DLP or authorization: private, employer, and regulated data are prohibited. Do not treat regex redaction as permission."
    ),
}

COMPUTER_USE = {
    "name": "jev_computer_use",
    "description": (
        "Bounded multi-step Windows browser or native desktop loop over Hermes computer_use. The loop performs "
        "a fresh capture before each action, refuses changed targets, and preserves Hermes dispatch/approval. "
        "DONE produces completion_candidate with verified=false; an independent coordinator-owned verifier is "
        "required. Hotkeys are denied unless explicitly listed. Only public or sanitized UI may be sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "minLength": 1, "description": "Complete GUI goal and all constraints."},
            "app": {
                "type": "string",
                "minLength": 1,
                "description": "Required non-empty target application, for example Google Chrome.",
            },
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Hard action budget."},
            "min_actions_before_done": {
                "type": "integer", "minimum": 0, "maximum": 29,
                "description": "Minimum actions before completion_candidate is offered.",
            },
            "allowed_hotkeys": {
                "type": "array",
                "default": [],
                "items": {"type": "string", "enum": _HOTKEYS},
                "uniqueItems": True,
                "description": "Explicit semantic hotkeys permitted for this call. Omitted means no hotkeys.",
            },
            "public_or_sanitized_data_ack": _ACKNOWLEDGEMENT,
        },
        "required": ["goal", "app"],
        "additionalProperties": False,
    },
}

SKILL_SELECT = {
    "name": "jev_skill_select",
    "description": (
        "Advisory-only choice among an explicit candidate set. It never loads a skill or mutates a prompt. "
        "It abstains when the uncalibrated Choice confidence, intended yes/no needs_skill probability, or "
        "winning probability is below the caller's bounded policy thresholds. Candidate identifiers are exact "
        "and cannot be trimmed; correctness calibration is not independently established."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "candidates": {
                "type": "array", "minItems": 1, "maxItems": 255,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "description"],
                    "additionalProperties": False,
                },
            },
            "choice_confidence_threshold": {
                "type": "number", "minimum": 0, "maximum": 1,
                "default": DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
                "description": "Uncalibrated local policy threshold; correctness calibration is not independently established.",
            },
            "needs_skill_threshold": {
                "type": "number", "minimum": 0, "maximum": 1,
                "default": DEFAULT_SKILL_NEEDS_THRESHOLD,
                "description": "Uncalibrated local policy threshold for intended yes/no needs_skill probability; calibration is not independently established.",
            },
            "winning_probability_threshold": {
                "type": "number", "minimum": 0, "maximum": 1,
                "default": DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
                "description": "Uncalibrated local policy threshold for the winning Choice distribution entry.",
            },
            "public_or_sanitized_data_ack": _ACKNOWLEDGEMENT,
        },
        "required": ["task", "candidates"],
        "additionalProperties": False,
    },
}

_MODEL_CANDIDATE_PROPERTIES = {
    "id": {"type": "string", "minLength": 1},
    "description": {"type": "string"},
    "approved": {"type": "boolean"},
    "data_classes_allowed": {
        "type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
        "description": "Explicit data classes this candidate may receive; never inferred from description.",
    },
    "tool_capabilities": {
        "type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
        "description": "Explicit tool capabilities; never inferred from description.",
    },
    "context_limit": {"type": "integer", "minimum": 1, "description": "Candidate context-token limit."},
    "cost": {"type": "number", "minimum": 0, "description": "Candidate unit cost used for code-owned cheapest selection."},
}

MODEL_ROUTE = {
    "name": "jev_model_route",
    "description": (
        "Advisory closed-set model routing. Code first filters explicit approved candidates by data classes, "
        "tool capabilities, context limit, and budget/cost; descriptions never supply policy metadata. Jev then "
        "provides an intended yes/no capability-fit probability for each eligible candidate, and code chooses the "
        "cheapest candidate above the bounded local policy threshold. No automatic fallback and no runtime model "
        "change; probability calibration is not independently established."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "requirements": {
                "type": "object",
                "properties": {
                    "data_classes": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
                    "tool_capabilities": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
                    "context_limit": {"type": "integer", "minimum": 1},
                    "budget": {"type": "number", "minimum": 0},
                },
                "additionalProperties": False,
                "default": {},
            },
            "candidates": {
                "type": "array", "minItems": 1, "maxItems": 255,
                "items": {
                    "type": "object",
                    "properties": _MODEL_CANDIDATE_PROPERTIES,
                    "required": ["id", "description", "approved", "cost"],
                    "additionalProperties": False,
                },
            },
            "capability_fit_threshold": {
                "type": "number", "minimum": 0, "maximum": 1,
                "default": DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
                "description": "Uncalibrated local policy threshold for intended yes/no fit probabilities; calibration is not independently established.",
            },
            "public_or_sanitized_data_ack": _ACKNOWLEDGEMENT,
        },
        "required": ["task", "candidates"],
        "additionalProperties": False,
    },
}
