from typing import Literal, Protocol

from pydantic import BaseModel, Field


class CapabilityUnavailable(BaseModel):
    status: Literal["unavailable"] = "unavailable"
    capability: str
    reason: str = "not_configured"


class EvidenceQuery(BaseModel):
    question: str
    ruleset: str | None = None
    requires_current_information: bool = False


class CalculationInput(BaseModel):
    operation: Literal["raid.calculate", "craft.plan_gap"]
    user_input: str
    context: dict[str, str] = Field(default_factory=dict)


class DecisionInput(BaseModel):
    goal: str
    player_context: dict[str, str] = Field(default_factory=dict)


class DecisionPort(Protocol):
    async def advise(self, request: DecisionInput) -> CapabilityUnavailable: ...


class KnowledgePort(Protocol):
    async def retrieve(self, query: EvidenceQuery) -> CapabilityUnavailable: ...


class SearchPort(Protocol):
    async def search(self, query: EvidenceQuery) -> CapabilityUnavailable: ...


class RaidCalculatorPort(Protocol):
    async def calculate(self, request: CalculationInput) -> CapabilityUnavailable: ...


class CraftCalculatorPort(Protocol):
    async def plan_gap(self, request: CalculationInput) -> CapabilityUnavailable: ...


class UnavailableExtensions:
    async def advise(self, request: DecisionInput) -> CapabilityUnavailable:
        return CapabilityUnavailable(capability="decision")

    async def retrieve(self, query: EvidenceQuery) -> CapabilityUnavailable:
        return CapabilityUnavailable(capability="knowledge")

    async def search(self, query: EvidenceQuery) -> CapabilityUnavailable:
        return CapabilityUnavailable(capability="web_search")

    async def calculate(self, request: CalculationInput) -> CapabilityUnavailable:
        return CapabilityUnavailable(capability="raid.calculate")

    async def plan_gap(self, request: CalculationInput) -> CapabilityUnavailable:
        return CapabilityUnavailable(capability="craft.plan_gap")
