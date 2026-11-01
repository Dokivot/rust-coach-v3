import hashlib
import json
from decimal import Decimal
from pathlib import Path

from rust_coach.contracts import AppError
from rust_coach.calculations.schemas import CalculationRequest


class RuleCatalog:
    def __init__(self):
        raw = Path(__file__).with_name("rules.json").read_bytes()
        self.data = json.loads(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.recipes = self.data["recipes"]
        self.items = self.data["items"]
        self.targets = self.data["targets"]
        for item, recipe in self.recipes.items():
            if item not in self.items or recipe["batch"] <= 0 or not recipe["lines"]:
                raise ValueError("Invalid recipe catalog")
            for child, amount in recipe["lines"].items():
                if child not in self.items or type(amount) is not int or amount <= 0:
                    raise ValueError("Invalid ingredient catalog")
        self.order(set(self.recipes), expand_fuel=True)
        for target in self.targets.values():
            for method in target["methods"].values():
                for side in method["sides"].values():
                    damage = Decimal(side["damage"])
                    if not damage.is_finite() or damage <= 0:
                        raise ValueError("Invalid damage catalog")

    def recipe(self, item, expand_fuel=False, gunpowder_recipe="workbench"):
        if item == "lowgradefuel" and not expand_fuel:
            return None
        if item == "gunpowder" and gunpowder_recipe == "mixing_table":
            return {**self.data["mix"], "bench": 0}
        return self.recipes.get(item)

    def order(self, roots, expand_fuel=False):
        visited, active, ordered = set(), set(), []

        def visit(item):
            if item in active:
                raise ValueError("Recipe cycle")
            if item in visited:
                return
            active.add(item)
            recipe = self.recipe(item, expand_fuel)
            if recipe:
                for child in sorted(recipe["lines"]):
                    visit(child)
            active.remove(item)
            visited.add(item)
            ordered.append(item)

        for root in sorted(roots):
            visit(root)
        return list(reversed(ordered))

    def require_items(self, names):
        unknown = sorted(set(names) - self.items.keys())
        if unknown:
            raise AppError("unsupported_item", "规则快照尚未收录物品：" + "、".join(unknown), 422)

    def provenance(self):
        return {key: self.data[key] for key in (
            "snapshot_id", "retrieved_at", "ruleset", "game_build", "verification",
            "provider", "source_url", "source_versions",
        )} | {"sha256": self.sha256}

    def public(self):
        return {"provenance": self.provenance(), "items": self.items,
                "craftable": sorted(self.recipes), "targets": self.targets,
                "schema": CalculationRequest.model_json_schema()}
