from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
PositiveQuantity = Annotated[int, Field(strict=True, ge=1, le=10_000)]


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["raid", "craft"]
    ruleset: Literal["pc_vanilla"] = "pc_vanilla"
    goals: dict[str, PositiveQuantity] = Field(default_factory=dict, max_length=20)
    inventory: dict[str, Quantity] | None = Field(default=None, max_length=100)
    inventory_complete: bool = Field(default=True, strict=True)
    reserve: dict[str, Quantity] = Field(default_factory=dict, max_length=100)
    target: str | None = Field(default=None, max_length=100)
    hp: Annotated[int, Field(strict=True, ge=1, le=2000)] | None = None
    count: Annotated[int, Field(strict=True, ge=1, le=20)] = 1
    side: Literal["hard", "soft"] = "hard"
    methods: list[str] = Field(default_factory=list, max_length=5)
    workbench: Annotated[int, Field(strict=True, ge=0, le=3)] | None = None
    blueprints: list[str] | None = Field(default=None, max_length=100)
    gunpowder_recipe: Literal["workbench", "mixing_table"] = "workbench"
    expand_fuel: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def operation_fields(self):
        if self.operation == "craft" and (self.target is not None or self.hp is not None or self.methods or self.count != 1 or self.side != "hard"):
            raise ValueError("Craft requests cannot contain raid parameters")
        if self.operation == "raid" and self.goals:
            raise ValueError("Raid requests cannot contain crafting goals")
        if len(self.methods) != len(set(self.methods)):
            raise ValueError("Duplicate methods")
        return self
