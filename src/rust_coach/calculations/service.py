import json
import logging
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from rust_coach.calculations.engine import Calculator
from rust_coach.calculations.presentation import render_calculation
from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.contracts import AppError, ModelMessage, ModelReply
from rust_coach.player_state.domain import calculation_inventory, project


logger = logging.getLogger("rust_coach")


class CalculationService:
    def __init__(self, model, instruction_role="system"):
        self.model = model
        self.instruction_role = instruction_role
        self.calculator = Calculator()

    def matches(self, message, previous, pending_message=None):
        if message.startswith("/calc"):
            return True
        if (previous or pending_message) and not re.search(r"取消|换个话题|不算了|cancel", message, re.I):
            return True
        return bool(re.search(
            r"爆炸|炸药|炸墙|炸门|拆|石墙|铁墙|木墙|金属墙|装甲墙|车库门|铁门|金属门|装甲门|合成|制作|缺口|材料|配方|"
            r"还差|造出|造一|做一|做\d|造\d|几颗|几发|多少发|多少颗|最省|算一下|"
            r"C4|火箭|硫磺|satchel|rocket|craft|raid|gunpowder|explosive", message, re.I))

    async def _extract_once(self, messages, previous, pending_message=None, inventory_container=None):
        text = messages[-1].content
        if text.startswith("/calc"):
            return json.loads(text[5:].strip())
        catalog = self.calculator.catalog
        prompt = """你是 Rust PC 原版计算请求解析器，只返回一个 JSON 对象，不回答游戏数值。
operation 必须是 raid 或 craft。只提取用户明确提供的信息，数值必须整数；不得编造配方、伤害或结果。
新任务不要继承旧库存；续问、改数量、补库存时可更新 previous_request，其余条件保留。
pending_message 是尚未解决的用户请求，若存在则结合本条补充理解；旧 previous_request 可能属于更早的任务，不要覆盖新的目标。
不确定的目标或物品保留用户原文作为 ID，交给验证器拒绝；不要擅自替换成近似物品。
不明确的套装不可猜物品，goals 留空；不明确的炸药种类不能当作 C4。C4=explosive.timed，炸药材料=explosives。
未提供库存用 null；明确零库存用 {}；仅部分库存也只填已提供的项。count 默认 1、side 默认 hard、hp 默认 null。
selected_inventory_container 非空表示用户已在界面选择档案库存，计算工具会读取并验证它；不要因为消息未列库存而返回 unsupported。
该字段不包含库存数量；不要猜测或复制档案库存到 inventory。仅当消息明确给出物品数量时才提取到 inventory，交给工具处理来源冲突。
几面墙的 hp 指每面当前生命值。不要将总血量填成每面血量。受损但不知血量、不同血量的多面墙不能计算。
未指定 methods 用 []（比较已收录单一方案）；最省资源解释为比较新增硫磺，并在 unsupported 标明其他优化要求。
只有明确指定才用混合台；普通工作台为默认。没有科技信息时 workbench/blueprints 用 null。
blueprints 表示用户明确确认可制作的配方（包括默认配方）。不要推断用户已解锁。
修改服、主机版、自定义配方/伤害、混搭最优、溅射、维修、火焰、战斗伤害、未列武器、缺血量的受损目标等不受支持条件，
返回 {"unsupported":"用简短中文说明需要补充或当前不支持的条件"}，不要忽略这些限制继续算。
如果本条是普通闲聊、取消计算或询问能力而不是计算或续问，返回 {"chat":true}。
字段结构如下，禁止额外字段：\n"""
        compact = {"items": {key: value["name"] for key, value in catalog.items.items()},
                   "targets": {key: value["name"] for key, value in catalog.targets.items()}}
        prompt += json.dumps(CalculationRequest.model_json_schema(), ensure_ascii=False)
        prompt += "\n仅可映射这些 ID（未收录项不得改名冒充）：" + json.dumps(compact, ensure_ascii=False)
        reply = await self.model.complete([
            ModelMessage(role=self.instruction_role, content=prompt),
            ModelMessage(role="user", content=json.dumps({"previous_request": previous,
                                                         "pending_message": pending_message,
                                                         "selected_inventory_container": inventory_container,
                                                         "message": text}, ensure_ascii=False)),
        ])
        if reply.truncated:
            raise ValueError("Truncated extraction")
        content = reply.content.strip()
        if content.startswith("```json") and content.endswith("```"):
            content = content[7:-3].strip()
        return json.loads(content)

    async def extract(self, messages, previous, pending_message=None, inventory_container=None):
        attempts = 1 if messages[-1].content.startswith("/calc") else 2
        for attempt in range(attempts):
            try:
                parsed = await self._extract_once(messages, previous, pending_message, inventory_container)
                if not isinstance(parsed, dict) or not (set(parsed) == {"chat"} or set(parsed) == {"unsupported"}):
                    CalculationRequest.model_validate(parsed)
                return parsed
            except (ValueError, TypeError):
                if attempt + 1 == attempts:
                    raise
                logger.info("calculation_parse_retry reason=invalid_schema")
            except AppError as error:
                if error.code != "model_invalid_response" or attempt + 1 == attempts:
                    raise
                logger.info("calculation_parse_retry reason=invalid_response")

    async def run(self, messages, previous=None, pending_message=None, player_snapshot=None, inventory_container=None, visual=None):
        parsed = None
        request = None
        player_usage = None
        pending = "\n".join(part for part in [pending_message, messages[-1].content] if part)[-12000:]
        try:
            parsed = await self.extract(messages, previous, pending_message, inventory_container)
            if isinstance(parsed, dict) and set(parsed) == {"chat"} and parsed["chat"] is True:
                return {"route": "chat", "calculation": None}
            if isinstance(parsed, dict) and set(parsed) == {"unsupported"} and isinstance(parsed["unsupported"], str):
                return {"reply": ModelReply(content="暂不能给出可靠计算：" + parsed["unsupported"][:500]),
                        "calculation": {"status": "needs_input", "input": previous, "pending_message": pending}}
            request = CalculationRequest.model_validate(parsed)
            accepted = (visual or {}).get("accepted")
            if accepted and inventory_container and any(item["kind"] == "inventory" for item in accepted["observations"]):
                raise AppError("ambiguous_inventory","同时提供了截图观测与档案库存，请只选一个库存来源。",422)
            if accepted and accepted["inventory_complete"]:
                if inventory_container or request.inventory is not None:
                    raise AppError("ambiguous_inventory","同时提供了截图库存与其他库存来源，请只选一个来源。",422)
                if not visual["fresh"] or accepted["subject"] != "self":
                    raise AppError("visual_inventory_not_current","这份截图库存不是已确认的当前本人库存，请重新盘点。",422)
                supported = self.calculator.catalog.items
                request = request.model_copy(update={"inventory":{key:value for key,value in accepted["inventory"].items() if key in supported}})
            if inventory_container:
                if player_snapshot is None:
                    raise AppError("player_scope_required", "请先关联玩家状态档案。", 422)
                player_snapshot = project(player_snapshot, datetime.now(timezone.utc))
                if request.inventory is not None:
                    raise AppError("ambiguous_inventory", "消息中已有库存，同时又选择了档案库存。请取消容器选择，或只使用档案库存，避免混合来源。", 422)
                inventory = calculation_inventory(player_snapshot, inventory_container)
                supported = self.calculator.catalog.items
                updates = {"inventory": {key: amount for key, amount in inventory.items() if key in supported}}
                for key, state_key in [("workbench", "technology.workbench"), ("blueprints", "technology.unlocked_blueprints")]:
                    record = player_snapshot["facts"].get(state_key)
                    if getattr(request, key) is None and record and record["status"] == "fresh":
                        updates[key] = record["value"] if key == "workbench" else [item for item in record["value"] if item in supported]
                player_usage = {"scope_id": player_snapshot["scope"]["id"], "revision": player_snapshot["revision"],
                                "container_id": inventory_container, "fields": sorted(updates),
                                "observed_at": player_snapshot["containers"][inventory_container]["observed_at"],
                                "excluded_items": sorted(set(inventory) - supported.keys())}
                request = CalculationRequest.model_validate(request.model_dump() | updates)
            result = self.calculator.calculate(request)
            if accepted:
                result["visual_usage"] = {"draft_id":visual["draft_id"],"confirmation_id":accepted["confirmation_id"],
                    "observed_at":accepted["observed_at"],"complete":accepted["inventory_complete"],"subject":accepted["subject"],
                    "inventory_used":accepted["inventory_complete"],"excluded_items":sorted(set(accepted["inventory"]) - self.calculator.catalog.items.keys())}
            if player_usage:
                result["player_state_usage"] = player_usage
            logger.info("calculation id=%s operation=%s snapshot=%s", result["calculation_id"],
                        request.operation, result["provenance"]["snapshot_id"])
            rendered = render_calculation(result, self.calculator.catalog)
            if accepted and result["visual_usage"]["excluded_items"]:
                rendered += "\n计算目录未收录、未参与抵扣的图片物品：" + "、".join(result["visual_usage"]["excluded_items"])
            if player_usage:
                rendered += (f"\n使用玩家档案版本 {player_usage['revision']} 的容器 {inventory_container} 完整盘点；"
                             f"观测于 {player_usage['observed_at']}。其他容器未计入，真实库存没有被扣减。")
                if player_usage["excluded_items"]:
                    rendered += "\n计算目录未收录、未参与配方抵扣的物品：" + "、".join(player_usage["excluded_items"])
            return {"reply": ModelReply(content=rendered), "calculation": result}
        except (ValueError, ValidationError, TypeError):
            return {"reply": ModelReply(content="未能可靠解析计算条件。请明确目标、数量、现有材料；也可以打开本地计算器逐项填写。不会使用模型猜测的数值。"),
                    "calculation": {"status": "needs_input", "input": previous, "pending_message": pending}}
        except AppError as error:
            if error.status != 422 or request is None:
                raise
            safe_input = request.model_dump(mode="json")
            if player_usage:
                for field in player_usage["fields"]:
                    safe_input[field] = None
            return {"reply": ModelReply(content=error.message + " 可在本地计算器查看已收录项目。"),
                    "calculation": {"status": "needs_input", "input": safe_input, "pending_message": pending}}
