import hashlib
import json
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from rust_coach.calculations.catalog import RuleCatalog
from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.contracts import AppError
from rust_coach.progress import report_stage


class Calculator:
    algorithm_version = "craft-dag-1/raid-single-method-1"

    def __init__(self):
        self.catalog = RuleCatalog()

    def calculate(self, request: CalculationRequest):
        report_stage("calculate_craft" if request.operation == "craft" else "calculate_raid")
        catalog = self.catalog
        catalog.require_items([*request.goals, *(request.inventory or {}), *request.reserve,
                               *(request.blueprints or []), *request.methods])
        if request.reserve and request.inventory is None:
            raise AppError("inventory_required", "设置保留资源时，请同时填写库存。", 422)
        if any(amount > (request.inventory or {}).get(item, 0) for item, amount in request.reserve.items()):
            raise AppError("invalid_reserve", "保留量不能超过已填写库存。", 422)
        normalized = request.model_dump(mode="json")
        if request.operation == "craft":
            if not request.goals:
                raise AppError("goals_required", "请提供要制作的物品及数量；套装需要逐件列出。", 422)
            unsupported = set(request.goals) - catalog.recipes.keys()
            if unsupported:
                raise AppError("recipe_unavailable", "尚未收录这些物品的制作配方：" + "、".join(sorted(unsupported)), 422)
            result = self.craft(request.goals, request)
        else:
            result = self.raid(request)
        provenance = catalog.provenance()
        identity = {"input": normalized, "snapshot": provenance["sha256"], "algorithm": self.algorithm_version}
        return {"status": "calculated", "operation": request.operation, "input": normalized,
                "result": result, "provenance": provenance, "algorithm": self.algorithm_version,
                "calculation_id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()}

    def craft(self, goals, request):
        expand_fuel = request.expand_fuel or "lowgradefuel" in goals
        demand = defaultdict(int, goals)
        available = {item: amount - request.reserve.get(item, 0) for item, amount in (request.inventory or {}).items()}
        consumed, acquired, leftovers, steps = {}, {}, {}, []
        for item in self.catalog.order(goals, expand_fuel):
            required = demand[item]
            used = min(required, available.get(item, 0))
            if used:
                consumed[item] = used
                available[item] -= used
            shortfall = required - used
            if not shortfall:
                continue
            recipe = self.catalog.recipe(item, expand_fuel, request.gunpowder_recipe)
            if recipe is None:
                acquired[item] = shortfall
                continue
            batches = (shortfall + recipe["batch"] - 1) // recipe["batch"]
            produced = batches * recipe["batch"]
            ingredients = {child: amount * batches for child, amount in recipe["lines"].items()}
            for child, amount in ingredients.items():
                demand[child] += amount
            if produced > shortfall:
                leftovers[item] = produced - shortfall
            steps.append({"item": item, "required": required, "owned_used": used,
                          "missing": shortfall, "batch_size": recipe["batch"], "batches": batches,
                          "produced": produced, "ingredients": ingredients, "workbench": recipe["bench"],
                          "source": self.catalog.items[item]["url"],
                          "source_record": self.catalog.data.get("recipe_sources", {}).get(item)})
        steps.reverse()
        bench = max((step["workbench"] for step in steps), default=0)
        tech = "not_checked" if steps else "not_needed"
        if steps and request.blueprints is not None:
            tech = "declared_available" if all(step["item"] in request.blueprints for step in steps) else "check_required"
        unknown_blueprints = [step["item"] for step in steps if request.blueprints is None or step["item"] not in request.blueprints]
        return {"goals": dict(goals), "acquisition": acquired, "inventory_known": request.inventory is not None,
                "inventory_complete": request.inventory is not None and request.inventory_complete,
                "inventory_used": consumed, "inventory_remaining": {key: value for key, value in available.items() if value},
                "reserved": request.reserve, "batch_leftovers": leftovers, "steps": steps,
                "requirements": {"workbench": bench, "workbench_status": (
                    "not_needed" if bench == 0 else "unknown" if request.workbench is None else
                    "satisfied" if request.workbench >= bench else "blocked"),
                    "blueprints_status": tech, "blueprints_to_check": unknown_blueprints,
                    "mixing_table_to_check": request.gunpowder_recipe == "mixing_table" and any(step["item"] == "gunpowder" for step in steps)}}

    def raid(self, request):
        if request.target not in self.catalog.targets:
            raise AppError("target_required", "请选择已收录的目标：木/石/金属/装甲墙，或木门/金属门/装甲门/车库门。", 422)
        target = self.catalog.targets[request.target]
        hp = request.hp if request.hp is not None else target["hp"]
        if hp > target["hp"]:
            raise AppError("invalid_hp", "当前生命值不能超过该目标满血值。", 422)
        methods = request.methods or list(target["methods"])
        options = []
        for item in methods:
            method = target["methods"].get(item)
            if method is None or request.side not in method["sides"]:
                raise AppError("unsupported_method", "该目标或受击面尚无所选爆炸物的可靠数据：" + item, 422)
            damage = Decimal(method["sides"][request.side]["damage"])
            per_target = int((Decimal(hp) / damage).to_integral_value(rounding=ROUND_CEILING))
            total = per_target * request.count
            plan = self.craft({item: total}, request)
            options.append({"item": item, "damage": str(damage), "per_target": per_target,
                            "count": total, "formula": f"ceil({hp} / {damage}) × {request.count} = {total}",
                            "new_sulfur": plan["acquisition"].get("sulfur", 0), "craft": plan,
                            "launcher_required": "rocket.launcher" if item.startswith("ammo.rocket.") else None})
        options.sort(key=lambda option: (option["new_sulfur"], option["count"], option["item"]))
        return {"target": request.target, "target_name": target["name"], "hp_each": hp,
                "target_count": request.count, "side": request.side, "options": options,
                "comparison": "minimum_additional_sulfur_among_single_method_options",
                "source": target["url"], "source_hash": target["source_hash"],
                "assumptions": ["PC 原版；按每个目标单独命中，无维修、衰减变化或溅射共享。",
                                "粘附爆炸物按成功贴附并爆炸；火箭按直接命中；不计哑火等待。",
                                "仅比较所列单一爆炸物方案的新增硫磺，不代表混搭或所有资源的全局最优。",
                                "发射器、学习蓝图、建造工作台的成本不包含在消耗品成本中。"]}
