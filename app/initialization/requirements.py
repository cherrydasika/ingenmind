"""What the setup conversation learns about the knowledge system to build:
the input the Domain Blueprint (#19) is written from.

Every field is optional while the conversation runs; `missing()` says what
setup still needs before the user can confirm. The model fills them through
the record_requirements tool, which merges: a field it sends replaces the
saved one, a field it leaves out is kept.
"""

from pydantic import BaseModel, Field, field_validator

MAX_ITEMS = 20
MAX_TEXT = 400


class SetupRequirements(BaseModel):
    purpose: str = Field("", description="What the knowledge system helps people with, in one sentence")
    audience: list[str] = Field(default_factory=list,
                                description="Who asks: e.g. general passengers, staff, businesses, developers")
    regions: list[str] = Field(default_factory=list,
                               description="Countries or regions it covers ('worldwide' if not limited)")
    question_types: list[str] = Field(default_factory=list,
                                      description="Kinds of questions it must answer, e.g. refunds, accessibility")
    topics_out_of_scope: list[str] = Field(default_factory=list, description="What it must not cover")
    organisations: list[str] = Field(default_factory=list,
                                     description="Organisations the user named (operators, regulators, brands)")
    live_information: list[str] = Field(
        default_factory=list, description="Live or changing data users want (departures, delays…): answered "
                                          "through live tools, not the knowledge base")
    authority: str = Field("", description="How authoritative the sources must be, e.g. official sources only")
    language: str = Field("", description="The language answers are in")
    assumed: list[str] = Field(default_factory=list,
                               description="Fields set from a sensible default the user did not state")

    @field_validator("audience", "regions", "question_types", "topics_out_of_scope", "organisations",
                     "live_information", "assumed", mode="before")
    @classmethod
    def _items(cls, value):
        if isinstance(value, str):
            value = [value]
        return [str(v).strip()[:MAX_TEXT] for v in (value or []) if str(v).strip()][:MAX_ITEMS]

    @field_validator("purpose", "authority", "language", mode="before")
    @classmethod
    def _text(cls, value):
        return str(value or "").strip()[:MAX_TEXT]


# What setup cannot do without: the blueprint is written from these.
REQUIRED = {
    "purpose": "what the knowledge system is for",
    "audience": "who will ask the questions",
    "regions": "which countries or regions it covers",
    "question_types": "what kinds of questions it must answer",
}


def merge(saved: dict, update: dict) -> SetupRequirements:
    """The saved requirements with the fields in `update` replaced."""
    current = SetupRequirements.model_validate(saved or {}).model_dump()
    sent = {k: v for k, v in (update or {}).items() if k in SetupRequirements.model_fields}
    return SetupRequirements.model_validate({**current, **sent})


def missing(requirements: SetupRequirements) -> list[str]:
    """The required fields still empty, as phrases for the model and the user."""
    return [label for field, label in REQUIRED.items() if not getattr(requirements, field)]
