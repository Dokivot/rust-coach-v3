import re

from rust_coach.calculations.requests import TARGET_PATTERN


def conversational(message):
    text = message.strip()
    return bool(re.fullmatch(r"(?:你好|您好|嗨|谢谢|谢谢你|好的|收到|hello|hi|thanks)[！!。.\s]*", text, re.I) or
                re.search(r"^(?:请)?(?:教我理解|解释(?:一下)?|什么是|怎么理解|继续上次.*(?:讲解|学习))", text)
                and not re.search(r"\d|多少|几颗|几发|还差|成本|伤害", text))


def cancelled(message):
    return any(re.fullmatch(r"(?:请|帮我)?(?:取消(?:当前)?(?:计算|任务)?|换个话题|不算了|cancel)[！!\s]*",
                            clause.strip(), re.I) for clause in re.split(r"[，,。；;\n]", message))


def continuation(message):
    return bool(re.search(r"^(?:那|它|这个|这面|这些|改成|改为|换成|换为|重新|继续|还有|已有|现在有|我有|剩|补充|不是|不对|按|用)|"
                          r"\d|[一二三四五六七八九十两]+\s*(?:个|颗|面|发)|血量|残血|背包|库存", message))


def operation_hint(message):
    if re.search(r"拆|炸|摧毁", message) and re.search(TARGET_PATTERN, message):
        return "raid"
    if re.search(r"(?:制作|合成|造|做).*(?:C4|炸药|火药|火箭)|\bcraft\b", message, re.I):
        return "craft"
    return None


def transition(message, previous, operation=None):
    if cancelled(message):
        return "cancel"
    if not previous or message.startswith("/calc"):
        return "new"
    previous_operation = previous.get("operation")
    desired = operation or operation_hint(message)
    if desired and previous_operation and desired != previous_operation:
        return "switch"
    if re.search(r"新任务|重新开始|另外(?:算|做)|再算一个", message):
        return "new"
    if re.search(r"不是|不对|更正|纠正|改成|改为", message):
        return "correct"
    return "continue"


def inventory_selection(message):
    if re.search(r"只算总需求|忽略(?:所有)?库存|不(?:用|再使用)库存", message):
        return "total"
    if re.search(r"(?:只用|只使用|仅用|仅使用)(?:当前)?(?:截图|图片)", message):
        return "image"
    if re.search(r"(?:只用|只使用|仅用|仅使用)(?:本轮)?(?:文本|文字)", message):
        return "text"
    if re.search(r"(?:只用|只使用|仅用|使用|按)(?:档案)?(?:背包|基地箱子|仓库)", message):
        return "storage" if re.search(r"基地箱子|仓库", message) else "backpack"
    return None


def previous_working_state(turns, scope_id):
    for turn in reversed(turns):
        if (turn.player_state_ref or {}).get("scope_id") != scope_id:
            break
        architecture = (turn.presentation or {}).get("architecture") or {}
        if "working_state" in architecture:
            return architecture["working_state"]
        if cancelled(turn.user):
            return None
        if turn.calculation:
            return {"task_id": architecture.get("task", {}).get("task_id"), "revision": 1,
                    "calculation": turn.calculation, "scope_id": scope_id,
                    "inventory_container": (turn.player_state_ref or {}).get("inventory_container")}
    return None
