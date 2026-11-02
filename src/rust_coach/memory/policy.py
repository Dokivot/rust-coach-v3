import re

from rust_coach.memory.schemas import CONCEPTS, MemoryCandidate


UNTRUSTED = re.compile(r"队友|朋友|对手|攻略|引用|假如|假设|如果|比如|例如|示例|别人|他说|她说|[“”\"`]|\b(?:teammate|opponent|quote|suppose|if)\b", re.I)
TRIGGER = re.compile(r"记住|以后|今后|通常|一般|习惯|我总是|我每|我偏好|我喜欢|我不懂|我不明白|我理解|我学会|我已经|我刚|我昨|我的基地|被偷家|被抄家|实际结果|按你说的|\b(?:remember|usually|prefer|I learned|I tried)\b", re.I)


def deterministic_candidates(message):
    if UNTRUSTED.search(message):
        return []
    candidates = []
    persistent = bool(re.search(r"以后|今后|一直|记住|我喜欢|我偏好|from now on|remember|I prefer", message, re.I))
    mappings = [
        ("verbosity", "concise", r"简短|简洁|短一点|先说结论|brief|concise"),
        ("verbosity", "detailed", r"详细|展开解释|列出步骤|detailed"),
        ("terminology", "plain", r"易懂|解释术语|少用简称|plain language"),
        ("terminology", "game_terms", r"游戏术语|游戏简称|用简称"),
        ("language", "en", r"英语|英文|English"), ("language", "zh", r"中文|Chinese"),
        ("guidance", "guided", r"一步步|逐步讲|从基础讲"),
        ("guidance", "independent", r"跳过基础|不讲基础|直接讨论取舍"),
    ]
    if persistent:
        for key, value, pattern in mappings:
            if re.search(pattern, message, re.I) and not re.search(r"不要|别再|不喜欢|不想|don't|do not", message, re.I):
                candidates.append(MemoryCandidate(kind="preference", key=key, value=value, quote=message[:500]))
    habits = [("play_style", "solo", r"我(?:一般|通常|习惯|总是).{0,5}(?:独狼|单人|一个人)"),
              ("play_style", "team", r"我(?:一般|通常|习惯|总是).{0,5}组队"),
              ("risk", "cautious", r"我(?:偏好|喜欢|一般|通常).{0,5}稳健"),
              ("risk", "aggressive", r"我(?:偏好|喜欢|一般|通常).{0,5}(?:进取|冒险)")]
    for key, value, pattern in habits:
        if re.search(pattern, message):
            candidates.append(MemoryCandidate(kind="habit", key=key, value=value, quote=message[:500]))
    return candidates[:5]


def validate_candidate(candidate, message, previous_teaching=None):
    if candidate.signal == "discussed":
        return False
    if candidate.quote not in message or UNTRUSTED.search(message):
        return False
    if re.search(r"忽略.*(?:规则|指令)|系统提示|API.?key|密钥|密码|password|ignore.*instructions", message, re.I):
        return False
    if candidate.kind == "preference":
        known = {item.key: item.value for item in deterministic_candidates(message) if item.kind == "preference"}
        return (candidate.key not in known or candidate.value == known[candidate.key]) and bool(
            re.search(r"以后|今后|一直|记住|我喜欢|我偏好|remember|I prefer|from now on", message, re.I)) and not re.search(r"这次|本次|暂时", message)
    if candidate.kind == "habit":
        known = {item.key: item.value for item in deterministic_candidates(message) if item.kind == "habit"}
        return (candidate.key not in known or candidate.value == known[candidate.key]) and bool(
            re.search(r"我(?:一般|通常|习惯|总是|每|偏好|喜欢)|I (?:usually|prefer)", candidate.quote, re.I))
    if candidate.kind == "event":
        if candidate.key == "goal":
            return bool(re.search(r"我的目标|我想|我打算", candidate.quote))
        first_person = re.search(r"我|本人|我的|\bI\b|\bmy\b|^(?:昨晚|昨天|刚才|今天)?(?:又)?被(?:偷家|抄家)", candidate.quote, re.I)
        return bool(first_person) and not re.search(r"打算|准备|想要|计划|应该|建议|明天|以后|怎么办|怎么做|[?？]", candidate.quote)
    if candidate.key not in (previous_teaching or {}).get("concepts", []) and not re.search(CONCEPTS[candidate.key][1], message, re.I):
        return False
    if candidate.signal == "needs_help":
        return bool(re.search(r"不懂|不明白|不会|没理解|不理解|怎么理解|don't understand", message, re.I))
    if candidate.signal == "demonstrated":
        return len(candidate.quote) >= 16 and bool(re.search(r"因为|所以|因此|先.+再|不能|不等于|because|therefore", candidate.quote, re.I))
    return bool(re.search(r"懂了|明白了|理解了|会了|understand", candidate.quote, re.I))


def select_teaching(message, profile, previous=None):
    concepts = [key for key, (_, pattern, _) in CONCEPTS.items() if re.search(pattern, message, re.I)][:2]
    if not concepts and re.match(r"^(继续|那|为什么|懂了|明白|我理解|还是不懂|按你说的)", message):
        concepts = (previous or {}).get("concepts", [])[:2]
    items = profile.get("items", {})
    if not concepts and re.search(r"继续上次|接着上次|上次学到|继续学", message):
        recent = sorted(((key, item) for key, item in items.items() if key.startswith("learning.")),
                        key=lambda pair: pair[1].get("updated_at", ""), reverse=True)
        if recent:
            concepts = [recent[0][0].removeprefix("learning.")]
    guidance = items.get("guidance", {}).get("value", "auto")
    modes = []
    for concept in concepts:
        progress = items.get("learning." + concept, {}).get("value", {})
        if progress.get("last_signal") == "needs_help":
            modes.append("guided")
        elif len(progress.get("demonstrations", [])) >= 2:
            modes.append("independent")
        elif progress.get("demonstrations") or progress.get("last_signal") == "self_reported":
            modes.append("practice")
        else:
            modes.append("guided")
    mode = guidance if guidance != "auto" else ("guided" if "guided" in modes else modes[0] if modes else "standard")
    reason = "explicit_preference" if guidance != "auto" else "concept_evidence"
    if re.search(r"不懂|不明白|我是新手|刚开始玩|从基础|一步步|beginner", message, re.I):
        mode, reason = "guided", "current_request"
    elif re.search(r"跳过基础|不用解释基础|直接讨论|skip.*basics", message, re.I):
        mode, reason = "independent", "current_request"
    if re.search(r"正在被打|马上下线|时间紧|under attack|in a hurry", message, re.I):
        mode, reason = "urgent", "current_urgency"
    discussed = [key for key in concepts if items.get("learning." + key, {}).get("value", {}).get("last_discussed")]
    return {"mode": mode, "reason": reason, "concepts": concepts, "previously_discussed": discussed,
            "focus": [CONCEPTS[key][2] for key in concepts], "assessment": "support_level_not_player_rank"}


def teaching_instruction(teaching):
    modes = {
        "standard": "先回答任务，必要术语简短解释，不强制提问或测验。",
        "guided": "先解决当前任务，再围绕当前知识点逐步说明前提、原因和下一步；一次只引入必要概念，可用本轮工具结果作例子。",
        "practice": "减少重复定义，围绕当前知识点给一个关键提示；玩家愿意学习时才请其完成一个判断，不强制考试。",
        "independent": "直接说明适用条件、边界与取舍，不主动重讲基础定义或入门例子；必要定义最多一句，玩家明确要求复习时仍需展开。",
        "urgent": "当前紧急：先给行动和关键阻塞条件，暂缓教学与练习。",
    }
    return "\n".join(["教学策略：" + modes.get(teaching.get("mode"), modes["standard"]),
        "讲过不等于掌握；自报理解不等于完成行动；不要给玩家贴能力标签。讲解方式不能改变数值、证据或必要条件。",
        "数值例子只使用本轮代码工具提供的结果；没有计算结果时用不带具体数值的公式、步骤或条件，不自行编造示例参数和计算。",
        "这些主题此前讨论过，先简短衔接，再按当前问题展开；不要自动认定已掌握：" + ", ".join(teaching.get("previously_discussed", [])),
        *teaching.get("focus", [])])
