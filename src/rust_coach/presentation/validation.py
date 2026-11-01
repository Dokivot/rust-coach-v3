import hashlib
import json
import re

from rust_coach.presentation.contracts import AnswerEnvelope


NUMERIC = re.compile(r"\d|[零一二三四五六七八九十百千万亿两]+\s*(?:个|颗|发|枚|层|级|秒|分钟|小时|点|倍|%|％|硫磺|碎片|木头|石头)")
RULES = {
    "abusive_voice": r"(?:你|玩家)(?:真|就是|太|是个|是)?(?:菜鸡|废物|蠢货|傻逼)|\byou(?:'re| are) (?:an? )?(?:idiot|moron|loser)\b",
    "unsupported_guarantee": r"包赢|稳赚|必胜|保证.{0,8}(?:赢|成功|不会被拆)|肯定(?:能赢|赢|成功)|\b(?:guaranteed (?:win|success)|you (?:will|definitely) win)\b",
    "fabricated_experience": r"我(?:亲自|亲身|本人).{0,12}(?:玩过|拆过|实战|经历)|我(?:已经)?玩了.{0,12}小时|我有.{0,12}(?:小时|职业选手)(?:经验|经历)?|\bI (?:have played|personally raided|have .{0,12}hours of)\b",
    "false_operation_receipt": r"(?:我)?(?:已为你|已经帮你|已帮你|已经|已)(?:保存|更新|扣减|扣除|修好|建好|加固).{0,12}(?:背包|档案|库存|基地|入口)|\bI(?:'ve| have) (?:saved|updated|deducted|repaired|built)\b",
    "certainty_upgrade": r"(?:材料|资源)(?:已经|完全|都)?(?:够用|足够|充足)|无需补充材料|现在(?:就)?(?:能|可以)直接(?:制作|完成)|\b(?:you have enough|materials are sufficient|ready to craft now)\b",
    "unsupported_numeric_claim": r"(?:需要|消耗|造成|伤害|血量|耐久|成本|requires?|costs?|damage)[^。！？!?\n]{0,20}\d+\s*(?:颗|发|枚|点|硫磺|木炭|碎片|HP|damage|sulfur|charcoal)",
}
NEGATION = re.compile(r"不能|不会|不该|不要|不应|无法|没有|并非|不代表|不保证|不等于|不能说|not |never |cannot |can't |don't ", re.I)
CONDITIONAL = re.compile(r"如果|若|补齐后|核对后|确认后|条件满足后|\bif\b|once |after ", re.I)


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def narrative_issues(text, *, forbid_numbers=False, status=None):
    issues = []
    normalized = re.sub(r"(?<![A-Za-z0-9])C4(?![A-Za-z0-9])", "定时炸弹", text, flags=re.I)
    if forbid_numbers and NUMERIC.search(normalized):
        issues.append("numeric_prose_forbidden")
    for code, pattern in RULES.items():
        if code == "certainty_upgrade" and status in (None, "feasible_in_snapshot"):
            continue
        for match in re.finditer(pattern, text, re.I):
            prefix = re.split(r"[。！？!?\n;；]", text[:match.start()])[-1][-100:]
            if NEGATION.search(prefix):
                continue
            if code == "certainty_upgrade" and CONDITIONAL.search(prefix):
                continue
            issues.append(code)
            break
    return list(dict.fromkeys(issues))


def check_envelope(envelope: AnswerEnvelope, expected_payload_hash, expected_blocks_hash):
    if signature(envelope.protected_payload) != expected_payload_hash:
        raise ValueError("protected_payload_changed")
    blocks = [block.model_dump() for block in envelope.blocks if block.kind == "protected"]
    if signature(blocks) != expected_blocks_hash:
        raise ValueError("protected_blocks_changed")
    if len({block.id for block in envelope.blocks}) != len(envelope.blocks):
        raise ValueError("duplicate_block_id")
    return [{"code": "protected_payload_unchanged", "status": "pass"},
            {"code": "required_blocks_preserved", "status": "pass"},
            {"code": "free_text_semantics", "status": "not_proven"}]
