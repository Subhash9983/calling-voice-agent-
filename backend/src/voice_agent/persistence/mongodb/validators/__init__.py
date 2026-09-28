"""Strict collection validators for all fourteen collections (docs/02 §24; docs/16 §14).

Each validator is derived from the strict record model that guards writes
(``validationLevel: strict``, ``validationAction: error``), plus hand
conditional rules the generator cannot express.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.consent import ConsentRecord
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.dataset import EvaluationDataset
from voice_agent.domain.evaluation.rating import EvaluationHumanRating
from voice_agent.domain.evaluation.result import EvaluationResult
from voice_agent.domain.evaluation.run import EvaluationRun
from voice_agent.persistence.mongodb.codecs.schema import Schema, model_schema
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import (
    DURABLE_EVENT_VALUES,
    SessionEventDocument,
)
from voice_agent.persistence.mongodb.documents.feedback import UserFeedbackDocument
from voice_agent.persistence.mongodb.documents.session import (
    SESSION_STATE_RULES,
    VoiceSessionDocument,
)
from voice_agent.persistence.mongodb.documents.timeline import (
    TURN_ACCEPTED_RULE,
    ConversationTurnDocument,
    ProviderOperationDocument,
)

VALIDATION_LEVEL: Final = "strict"
VALIDATION_ACTION: Final = "error"

DOCUMENT_MODELS: Mapping[Collection, type[BaseModel]] = MappingProxyType(
    {
        Collection.AGENT_CONFIGS: AgentConfig,
        Collection.VOICE_SESSIONS: VoiceSessionDocument,
        Collection.CONVERSATION_TURNS: ConversationTurnDocument,
        Collection.PROVIDER_OPERATIONS: ProviderOperationDocument,
        Collection.SESSION_EVENTS: SessionEventDocument,
        Collection.COST_ENTRIES: CostEntryRecord,
        Collection.USER_FEEDBACK: UserFeedbackDocument,
        Collection.ERROR_EVENTS: ErrorEventRecord,
        Collection.CONSENT_RECORDS: ConsentRecord,
        Collection.EVALUATION_DATASETS: EvaluationDataset,
        Collection.EVALUATION_CASES: EvaluationCase,
        Collection.EVALUATION_RUNS: EvaluationRun,
        Collection.EVALUATION_RESULTS: EvaluationResult,
        Collection.EVALUATION_HUMAN_RATINGS: EvaluationHumanRating,
    }
)
_EXTRA_RULES: Mapping[Collection, Schema] = MappingProxyType(
    {
        Collection.VOICE_SESSIONS: SESSION_STATE_RULES,
        Collection.CONVERSATION_TURNS: TURN_ACCEPTED_RULE,
    }
)


def json_schema(collection: Collection) -> Schema:
    schema = model_schema(DOCUMENT_MODELS[collection], root=True)
    if collection is Collection.SESSION_EVENTS:
        schema["properties"]["event_type"]["enum"] = list(DURABLE_EVENT_VALUES)
    rules = _EXTRA_RULES.get(collection)
    if rules is not None:
        return {**schema, **rules}
    return schema


def validator_for(collection: Collection) -> dict[str, Any]:
    return {"$jsonSchema": json_schema(collection)}


def collection_options(collection: Collection) -> dict[str, Any]:
    return {
        "validator": validator_for(collection),
        "validationLevel": VALIDATION_LEVEL,
        "validationAction": VALIDATION_ACTION,
    }
