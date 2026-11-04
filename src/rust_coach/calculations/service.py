import json
import logging
import re

from pydantic import ValidationError

from rust_coach.calculations.engine import Calculator
from rust_coach.calculations.presentation import render_calculation
from rust_coach.calculations.requests import contextual_request, standard_raid_request, switches_to_raid
from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.contracts import AppError, ModelMessage, ModelReply
from rust_coach.progress import report_stage
from rust_coach.vision.context import visual_input
from rust_coach.architecture.task_context import cancelled, continuation, conversational, inventory_selection
from rust_coach.player_state.inputs import resolve_calculation_input
from rust_coach.player_state.working import current_assertion, text_facts


logger = logging.getLogger("rust_coach")


class CalculationService:
    def __init__(self, model, instruction_role="system", knowledge=None):
        self.model = model
        self.instruction_role = instruction_role
        self.calculator = Calculator(knowledge)

    def matches(self, message, previous, pending_message=None):
        if message.startswith("/calc"):
            return True
        if cancelled(message) or conversational(message):
            return False
        if contextual_request(message, previous, pending_message):
            return True
        if pending_message or previous and continuation(message):
            return True
        return bool(re.search(
            r"爆炸|炸药|炸墙|炸门|拆|石墙|铁墙|木墙|金属墙|装甲墙|车库门|铁门|金属门|装甲门|合成|制作|缺口|材料|配方|"
            r"还差|造出|造一|做一|做\d|造\d|几颗|几发|多少发|多少颗|最省|算一下|"
            r"C4|火箭|硫磺|satchel|rocket|craft|raid|gunpowder|explosive", message, re.I))

    async def _extract_once(self, messages, previous, pending_message=None, inventory_container=None):
        text = messages[-1].content
        if text.startswith("/calc"):
            return json.loads(text[5:].strip())
        contextual = contextual_request(text, previous, pending_message)
        if contextual:
            return contextual
        standard = standard_raid_request(text)
        if standard:
            return standard
        catalog = self.calculator.catalog
        prompt = """你是 Rust PC 原版计算请求解析器，只返回一个 JSON 对象，不回答游戏数值。
operation 必须是 raid 或 craft。只提取用户明确提供的信息，数值必须整数；不得编造配方、伤害或结果。
新任务不要继承旧库存；续问、改数量、补库存时可更新 previous_request，其余条件保留。
pending_message 是尚未解决的用户请求，若存在则结合本条补充理解；旧 previous_request 可能属于更早的任务，不要覆盖新的目标。
不确定的目标或物品保留用户原文作为 ID，交给验证器拒绝；不要擅自替换成近似物品。
不明确的套装不可猜物品，goals 留空；不明确的炸药种类不能当作 C4。C4=explosive.timed，炸药材料=explosives。
目标明确为单个物品但未说数量时，先按制作1个解析；不要仅因省略数量返回 unsupported。
未提供库存用 null；明确零库存用 {}；仅部分库存也只填已提供的项。count 默认 1、side 默认 hard、hp 默认 null。
inventory_complete 表示是否提供完整库存；明确只有部分材料时为 false。消息未提供库存时保留 null，系统会自动接入本会话截图，不要求用户重复填写。
selected_inventory_container 非空表示用户已在界面选择档案库存，计算工具会读取并验证它；不要因为消息未列库存而返回 unsupported。
该字段不包含库存数量；不要猜测或复制档案库存到 inventory。仅当消息明确给出物品数量时才提取到 inventory，交给工具处理来源冲突。
几面墙的 hp 指每面当前生命值。不要将总血量填成每面血量。受损但不知血量、不同血量的多面墙不能计算。
没有提到受损、残血或特殊条件时，未提供血量是正常缺省：hp=null，工具从规则目录读取满血值；side 默认 hard。不得仅因没说血量或软硬面返回 unsupported。
例如“拆一面石墙最省资源的方案是什么？”返回 raid、target=building:base-wall:stone、hp=null、count=1、side=hard、methods=[]。
未指定 methods 用 []（比较已收录单一方案）；“最省资源/材料”默认比较新增硫磺，是可支持的请求，不返回 unsupported。只有用户明确要求其他不受支持的优化指标时才说明限制。
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
                    self.validate_inventory_evidence(parsed, messages[-1].content, previous)
                    self.validate_technology_evidence(parsed, messages[-1].content, previous)
                return parsed
            except (ValueError, TypeError):
                if attempt + 1 == attempts:
                    raise
                logger.info("calculation_parse_retry reason=invalid_schema")
            except AppError as error:
                if error.code != "model_invalid_response" or attempt + 1 == attempts:
                    raise
                logger.info("calculation_parse_retry reason=invalid_response")

    def validate_technology_evidence(self, parsed, message, previous):
        if message.startswith("/calc"):
            return
        prior = previous or {}
        workbench = parsed.get("workbench")
        if workbench is not None and workbench != prior.get("workbench"):
            if text_facts(message).get("technology.workbench") != workbench:
                raise ValueError("workbench_without_text_evidence")
        blueprints = parsed.get("blueprints")
        if blueprints is not None and blueprints != prior.get("blueprints"):
            clauses = [clause for clause in re.split(r"[，,。；;\n]", message) if current_assertion(clause)
                       and re.search(r"解锁|能制作|可制作|已学会", clause)
                       and not re.search(r"未|没|不能|对手|队友", clause)]
            evidence = "，".join(clauses)
            for identifier in blueprints:
                item = self.calculator.catalog.items.get(identifier, {})
                names = [identifier, item.get("name", identifier)] + (["C4"] if identifier == "explosive.timed" else [])
                if not any(name.lower() in evidence.lower() for name in names):
                    raise ValueError("blueprint_without_text_evidence")

    def validate_inventory_evidence(self, parsed, message, previous):
        if message.startswith("/calc") or parsed.get("inventory") is None:
            return
        inventory = parsed["inventory"]
        prior_inventory = (previous or {}).get("inventory")
        inventory_statement = re.search(r"已有|我有|库存|背包|现有|手上|手里|没有|have|inventory", message, re.I)
        if inventory == prior_inventory and not inventory_statement:
            parsed["inventory_complete"] = previous.get("inventory_complete", False)
            return
        clauses = [clause.strip() for clause in re.split(r"[，,。；;\n]", message)]
        declarations = [clause for clause in clauses if current_assertion(clause) and
                        not re.search(r"对手|敌人|队友|他有|她有|没有\s*\d|没\s*\d|不到|不足|不够|至少|大约|大概|(?:要|需要|缺|还差)\s*\d", clause)]
        evidence_text = "，".join(declarations)
        complete = bool(re.search(r"全部库存|完整(?:盘点|库存)|没有其他|只有|空(?:背包|库存)|背包(?:已)?空|库存为空|零库存|没有(?:任何)?材料|empty (?:inventory|backpack)|no materials|complete inventory", evidence_text, re.I))
        if not inventory and not complete:
            raise ValueError("empty_inventory_without_evidence")
        for identifier, quantity in inventory.items():
            item = self.calculator.catalog.items.get(identifier)
            names = [identifier, item["name"]] if item else [identifier]
            if identifier == "explosive.timed":
                names.append("C4")
            name_pattern = "(?:" + "|".join(re.escape(name) for name in names) + ")"
            mentioned = re.search(name_pattern, message, re.I)
            if prior_inventory is not None and prior_inventory.get(identifier) == quantity and not mentioned:
                continue
            amount = r"(?<!\d)" + str(quantity) + r"(?!\d)"
            evidence = re.search(amount + r"\s*(?:个|份|块|颗)?\s*" + name_pattern + "|" +
                                 name_pattern + r"\s*(?:[:：×x]|数量(?:是|为)?)?\s*" + amount, evidence_text, re.I)
            goal_as_inventory = identifier in parsed.get("goals", {}) and not re.search(r"已有|我有|库存|背包|现有|手上|手里|have|inventory", evidence_text, re.I)
            if not evidence or goal_as_inventory:
                raise ValueError("inventory_without_text_evidence")
        parsed["inventory_complete"] = complete

    async def run(self, messages, previous=None, pending_message=None, player_snapshot=None, inventory_container=None, visual=None, extracted=None, selection=None):
        report_stage("understand")
        parsed = None
        request = None
        player_usage = None
        if switches_to_raid(messages[-1].content, previous):
            previous, pending_message = None, None
        pending = "\n".join(part for part in [pending_message, messages[-1].content] if part)[-12000:]
        try:
            parsed = extracted if extracted is not None else await self.extract(messages, previous, pending_message, inventory_container)
            if isinstance(parsed, dict) and set(parsed) == {"chat"} and parsed["chat"] is True:
                return {"route": "chat", "calculation": None}
            if isinstance(parsed, dict) and set(parsed) == {"unsupported"} and isinstance(parsed["unsupported"], str):
                return {"reply": ModelReply(content="暂不能给出可靠计算：" + parsed["unsupported"][:500]),
                        "calculation": {"status": "needs_input", "input": previous, "pending_message": pending}}
            request = CalculationRequest.model_validate(parsed)
            self.validate_technology_evidence(parsed, messages[-1].content, previous)
            accepted = visual_input(visual)
            request, input_usage = resolve_calculation_input(request, player_snapshot, inventory_container, visual,
                self.calculator.catalog.items, selection=selection or inventory_selection(messages[-1].content))
            if input_usage["inventory_source"] == "text":
                input_usage["verification"] = "structured_user_input" if messages[-1].content.startswith("/calc") else "model_extracted_from_text"
            player_usage = input_usage.get("player_state_usage")
            result = self.calculator.calculate(request)
            result["input_usage"] = input_usage
            if input_usage.get("visual_usage"):
                result["visual_usage"] = input_usage["visual_usage"]
            if player_usage:
                result["player_state_usage"] = player_usage
            logger.info("calculation id=%s operation=%s snapshot=%s", result["calculation_id"],
                        request.operation, result["provenance"]["snapshot_id"])
            rendered = render_calculation(result, self.calculator.catalog)
            if accepted and result.get("visual_usage", {}).get("excluded_items"):
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
                    "calculation": {"status": "needs_input", "input": safe_input, "pending_message": pending,
                                    "error_code": error.code}}
