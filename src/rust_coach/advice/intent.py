import re


BEGINNER = re.compile(r"新手|小白|萌新|刚开始玩|刚入坑|刚进服|刚出生|从零开始|\b(?:beginner|new player|new to rust|first day)\b", re.I)
STARTING = re.compile(r"(?:不知|不懂|不清楚).*?(?:怎么开始|从哪|从何|做什么|干什么)|(?:怎么|如何|怎样).{0,6}(?:开局|起步|入门)|\b(?:where (?:do I|to) start|how (?:do I|to) get started)\b", re.I)
PLANNING = re.compile(r"做什么|干什么|怎么玩|开始|入门|起步|开局|优先|发展|下一步|攻略|指南|路线|\b(?:start|first|do|guide|next|progress)\b", re.I)
FOLLOWUP = re.compile(r"^(?:继续|那|如果|我现在|我只有|我已经|我有|已经|有了|还没有|没有|目前|现在|接下来|下一步|那之后|I have|I already|now|next|what next)", re.I)


def exact_calculation(message):
    return bool(re.search(r"还差|还缺|缺口|(?:多少|几[颗发个]).{0,10}(?:材料|资源|硫磺|木炭|木头|石头|炸药|火箭|C4)|(?:需要|消耗|成本|配方).{0,8}多少|(?:怎么|如何).{0,5}(?:合成|制作)|(?:合成|制作).{0,15}(?:需要|材料|配方)|how (?:much|many).*(?:need|cost|craft)|how (?:do I|to) craft|recipe for", message, re.I))


def is_onboarding(message, previous_answer=None, previous_request=None):
    text = message.strip()
    if text.startswith(("/calc", "/state")) or exact_calculation(text):
        return False
    if re.search(r"先.+还是|该不该|应不应该|\bversus\b|\bvs\.?\b", text, re.I):
        return False
    if re.search(r"正在被打|正在被拆|正在被追|under attack|being chased", text, re.I):
        return False
    if STARTING.search(text) or BEGINNER.search(text) and PLANNING.search(text):
        return True
    prior_guide = bool(previous_answer and previous_answer.get("stages")) or bool(previous_request and is_onboarding(previous_request))
    return bool(prior_guide and FOLLOWUP.search(text))
