import hashlib
import json
from pathlib import Path

from rust_coach.calculations.catalog import RuleCatalog
from rust_coach.knowledge.registry import digest


SNAPSHOT = "rustdata-pc-vanilla-2026-11-01"
RULES_HASH = "11033e0ec886eb101a301c7e3e4ecc8fc9c30a23afeacc67679fecd710bbb3d5"


def default_release():
    catalog = RuleCatalog()
    documents = []
    for identifier, recipe in catalog.recipes.items():
        name = catalog.items[identifier]["name"]
        lines = "、".join(f"{catalog.items[item]['name']} {amount}" for item, amount in recipe["lines"].items())
        documents.append({"id": "recipe:" + identifier, "title": name + "配方与工作台条件",
            "text": f"本快照中，{name}每批产出{recipe['batch']}，需要{lines}；工作台等级{recipe['bench']}。蓝图可用性仍需核对。",
            "entities": [identifier], "claims": ["recipe:" + identifier, "technology:" + identifier],
            "terms": "recipe craft ingredients workbench blueprint " + identifier,
            "kind": "rule", "source": "rules.json#/recipes/" + identifier,
            "source_hash": RULES_HASH, "verification": "snapshot_only", "snapshot": SNAPSHOT})
    for identifier, target in catalog.targets.items():
        details = []
        for item, method in target["methods"].items():
            for side, values in method["sides"].items():
                details.append(f"{catalog.items[item]['name']}在{side}面的有效伤害记录为{values['damage']}")
        documents.append({"id": "raid:" + identifier, "title": target["name"] + "生命值与伤害条件",
            "text": f"本快照中，{target['name']}满血{target['hp']}HP。" + "；".join(details) + "。只适用于对应目标、朝向与命中条件；未经游戏内验证。",
            "entities": [identifier], "claims": ["raid:" + identifier], "terms": "raid damage health hp wall door " + identifier,
            "kind": "rule", "source": "rules.json#/targets/" + identifier,
            "source_hash": RULES_HASH, "verification": "snapshot_only", "snapshot": SNAPSHOT})
    documents.append({"id": "recipe:mix", "title": "混合台火药配方", "text": json.dumps(catalog.data["mix"], ensure_ascii=False),
        "entities": ["gunpowder"], "claims": ["mixing"], "terms": "gunpowder mixing table recipe",
        "kind": "rule", "source": "rules.json#/mix", "source_hash": RULES_HASH,
        "verification": "snapshot_only", "snapshot": SNAPSHOT})
    curated = json.loads(Path(__file__).with_name("curated.json").read_text())
    for document in curated:
        document.setdefault("source_hash", hashlib.sha256(document["text"].encode()).hexdigest())
        document.setdefault("snapshot", SNAPSHOT)
        documents.append(document)
    return {"id": "demo-knowledge-2026-11-01-v1", "snapshot": SNAPSHOT, "rules_sha256": RULES_HASH,
            "verification": "build_unverified_no_independent_human_review", "documents": documents}


def release_identity(data):
    from rust_coach.knowledge.schemas import KnowledgeRelease

    return digest(KnowledgeRelease.model_validate(data).model_dump(mode="json"))
