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
    r"(?:请问|请帮我算一下|帮我算一下|请帮我|帮我|我想|我要)?"
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
