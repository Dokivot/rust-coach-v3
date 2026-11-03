from rust_coach.advice.onboarding import validate_guide
from rust_coach.advice.schemas import GuideAnswer


SOURCE = "https://falconrust.com/guides/general/first-day/"
BASELINE = [
    {
        "quote": "Your first two crafts should be a Stone Hatchet and a Stone Pickaxe.",
        "action": "如果还在用初始石头采集，先准备石斧和石镐，作为继续采集的基础工具。已有这两种工具就跳过。",
        "completion": "已经备好石斧和石镐。",
        "action_en": "If you still gather with the starting rock, prepare a stone hatchet and stone pickaxe. Skip this if you already have both.",
        "completion_en": "A stone hatchet and stone pickaxe are available.",
    },
    {
        "quote": "Pick up hemp plants whenever you see them. They give cloth, which you need for sleeping bags, bows, and basic clothing.",
        "action": "如果还没有睡袋，沿途采集麻获得布，准备睡袋；已有睡袋就继续下一阶段。",
        "completion": "已经准备好睡袋。",
        "action_en": "If you do not have a sleeping bag, gather hemp for cloth and prepare one. Otherwise, continue to the next stage.",
        "completion_en": "A sleeping bag is ready.",
    },
    {
        "quote": "What matters is that it is locked, has a Tool Cupboard inside, and has walls that are not twig.",
        "action": "如果还没有落脚点，先建一个小基地，锁好入口、放置工具柜，并把树枝墙升级。已有这样的基地就跳过。",
        "completion": "有上锁、带工具柜且墙体已脱离树枝材质的落脚点。",
        "action_en": "If you lack shelter, build a small base, lock it, place a tool cupboard inside, and upgrade twig walls. Skip this if your base already meets these conditions.",
        "completion_en": "You have locked shelter with a tool cupboard and non-twig walls.",
    },
    {
        "quote": "For the doors: farm enough animal fat and cloth to produce 50 low grade fuel, then build a Furnace to smelt metal ore into metal fragments.",
        "action": "落脚点准备好后，如果还没有熔炉，就收集动物脂肪和布来准备低品质燃油，再制作熔炉，把金属矿处理成金属碎片。",
        "completion": "能够用熔炉处理金属矿。",
        "action_en": "Once shelter is ready, if you lack a furnace, gather animal fat and cloth for low grade fuel, then make a furnace to smelt metal ore into metal fragments.",
        "completion_en": "You can smelt metal ore in a furnace.",
    },
    {
        "quote": "Once the doors are closed and the TC is stocked, your next two targets are sheet metal doors and a Workbench Level 1.",
        "action": "基地封闭、工具柜备有维护材料后，把尚未完成的金属门和一级工作台作为后续目标，按制作界面逐项准备材料。",
        "completion": "基地已有金属门和一级工作台，可以继续推进早期发展。",
        "action_en": "Once the base is closed and the tool cupboard is stocked, work toward sheet metal doors and a level 1 workbench if you still lack them, gathering materials shown in the crafting interface.",
        "completion_en": "The base has sheet metal doors and a level 1 workbench for early progression.",
    },
]


def baseline_guide(documents, english=False):
    document = next((item for item in documents if item["url"].rstrip("/") == SOURCE.rstrip("/") and
        all(stage["quote"] in item["text"] for stage in BASELINE)), None)
    if document is None:
        return None
    suffix = "_en" if english else ""
    answer = GuideAnswer.model_validate({
        "goal": "Start with gathering tools, a sleeping bag and shelter, then move toward metal and early technology." if english else
            "先准备采集工具、睡袋和能储存物资的落脚点，再逐步发展金属与科技。",
        "assumption": "This is a basic PC vanilla route. Follow the unfinished stages; skip tools, sleeping bags or shelter you already have." if english else
            "这是一条 PC 原版的基础路线；从尚未完成的阶段继续，已有工具、睡袋或基地就跳过对应步骤。",
        "stages": [{"id": f"stage_{index}", "action": stage["action" + suffix], "completion": stage["completion" + suffix],
            "citations": [{"document_id": document["id"], "quote": stage["quote"]}]} for index, stage in enumerate(BASELINE, 1)],
        "stop_when": "Pause and move away if other players or animals threaten you; do not stay exposed to finish a task." if english else
            "附近出现玩家交火、野兽或其他明显威胁时，先停止手头动作并避开，不要为了做完步骤停留。",
        "followup": "What is the first stage you have not finished?" if english else "这些阶段里，你目前最先还没完成的是哪一步？",
        "limitations": ["A source-backed basic route is shown because the personalized route could not be completed; the current game build is unverified." if english else
            "本次个性化路线未完成，先提供有原文支持的基础路线；资料未核验当前游戏版本。"],
    })
    validate_guide(answer, {"documents": documents})
    return answer.model_dump()
