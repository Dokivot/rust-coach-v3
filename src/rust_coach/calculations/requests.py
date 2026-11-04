import re

from rust_coach.calculations.schemas import CalculationRequest


RAID_TARGETS = {
    "木墙": "building:base-wall:wood", "石墙": "building:base-wall:stone",
    "铁墙": "building:base-wall:metal", "金属墙": "building:base-wall:metal",
    "装甲墙": "building:base-wall:toptier", "木门": "item:door.hinged.wood",
    "木头门": "item:door.hinged.wood", "铁门": "item:door.hinged.metal",
    "金属门": "item:door.hinged.metal", "装甲门": "item:door.hinged.toptier",
    "钢铁门": "item:door.hinged.toptier", "车库门": "item:wall.frame.garagedoor",
    "卷帘门": "item:wall.frame.garagedoor",
}
TARGET_PATTERN = "|".join(sorted(RAID_TARGETS, key=len, reverse=True))
RAID_COMPARISON = re.compile(
    r"(?:请问|请帮我算一下|帮我算一下|请帮我|帮我|我想|我要|改成|改为|换成)?"
    r"(?:拆除|拆掉|炸掉|摧毁|拆|炸)?"
    r"(?:(?P<count>[0-9]+|[一二两三四五六七八九十])(?:面|扇|个))?"
    r"(?P<target>" + TARGET_PATTERN + r")"
    r"(?:怎么拆|怎么炸|用什么拆|用什么炸)?"
    r"(?:最省资源|最省材料|最省硫磺|最省)"
    r"(?:的)?(?:方案|方法)?(?:是什么|是啥|有哪些|怎么选)?"
)


def standard_raid_request(message):
    match = RAID_COMPARISON.fullmatch(re.sub(r"[\s，,。？！?!]", "", message))
    if not match:
        return None
    count = match["count"] or "1"
    numerals = {name: index for index, name in enumerate("一二三四五六七八九十", 1)} | {"两": 2}
    return CalculationRequest(operation="raid", target=RAID_TARGETS[match["target"]],
        count=int(count) if count.isascii() else numerals[count]).model_dump(mode="json")


def switches_to_raid(message, previous):
    return bool(previous and previous.get("operation") == "craft" and
        re.search(r"拆|炸|摧毁", message) and re.search(TARGET_PATTERN, message))


def contextual_request(message, previous, pending_message=None):
    text = re.sub(r"[\s，,。？！?!]", "", message)
    health = re.fullmatch(r"(?:补充)?(?:它|这面墙|墙|目标)?(?:现在)?(?:还剩|剩余|剩|血量是|血量为)(\d+)(?:血|点血|HP|hp)", text)
    if health:
        if not previous or previous.get("operation") != "raid":
            return {"unsupported": "需要先明确正在拆哪种墙或门，才能使用补充的血量。"}
        return {**previous, "hp": int(health[1])}
    if re.search(r"(?:受损|残血|损坏).*(?:不知|不清楚|未知|看不清)|(?:不知|未知).*血量", message):
        return {"unsupported": "受损目标需要当前血量，不能按满血计算。"}
    if re.search(r"(?:又捡了?|消耗了|用了)\s*\d+", message):
        return {"unsupported": "增减量不能当作当前库存总量，请补充相关物品现在一共多少。"}
    if re.search(r"(?:制作|合成|造|做).*(?:并且|并|然后|同时|再).*(?:拆|炸).*(?:" + TARGET_PATTERN + ")", message):
        return {"unsupported": "这条消息包含制作和拆墙两个目标，请先选择本轮要计算的一个目标。"}
    return None
