import argparse
import hashlib
import json
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from rust_coach.knowledge.bundle import default_release, release_identity
from rust_coach.knowledge.service import LocalKnowledge
from rust_coach.knowledge.web import relevant
from rust_coach.persistence import ConversationStore


ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ("B0_coarse", "B1_eligible_coarse", "B2_exact_fts")


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(knowledge, case):
    fault = case.get("fault", {})
    if fault.get("field"):
        package = default_release()
        package["id"] = "fixture-" + case["id"]
        document = next(document for document in package["documents"] if document["id"] == fault["document_id"])
        document[fault["field"]] = fault["value"]
        result = knowledge.registry.install(package, knowledge.catalog.sha256)
        knowledge.registry.activate(package["id"], result["release_sha256"], "evaluation fixture; not game verification")
    if fault.get("revoke"):
        knowledge.registry.revoke(knowledge.reference()["release_id"], fault["document_id"], "evaluation fixture")
    if fault.get("index_unavailable"):
        with knowledge.registry.store.connection() as connection:
            connection.execute("DROP TABLE knowledge_fts")


def coarse(knowledge, case, *, eligibility):
    started = perf_counter()
    snapshot = knowledge.registry.snapshot()
    need = knowledge.need(case["message"], case.get("previous"))
    documents = snapshot["release"]["documents"]
    if eligibility:
        documents, excluded = knowledge.eligible(snapshot)
    else:
        excluded = []
    result = {"documents": [], "excluded": excluded, "status": "missing_evidence", "context_chars": 0,
              "network_calls": 0, "model_calls": 0, "cache_hit": False}
    if eligibility and need["scope"] != "snapshot":
        result["status"] = need["scope"]
    else:
        claims = set(need["claims"])
        topics = (["updates"] if claims & {"version", "feedback"} else
                  ["first_day"] if "inventory" in claims else
                  ["combat"] if claims & {"rounding", "targets", "optimization", "conditions", "unknown_hp", "unsupported_claim"}
                  else ["progression"])
        for document in documents:
            view = {"title": document["title"], "text": " ".join([document["text"], document["terms"], *document["entities"], *document["claims"]]),
                    "url": "https://fixture.invalid/doc/" + document["id"]}
            if not relevant(view, topics):
                continue
            if len(result["documents"]) >= 5 or result["context_chars"] + len(document["text"]) > case.get("max_chars", 6000):
                continue
            result["documents"].append(document)
            result["context_chars"] += len(document["text"])
        if result["documents"]:
            result["status"] = "supported_snapshot"
    result["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    return result


def grade(case, result, eligible_ids):
    selected = {document["id"] for document in result["documents"]}
    invalid = sorted(selected - eligible_ids)
    missing = sorted(set(case["required_ids"]) - selected)
    checks = {"status": result["status"] == case["expected_status"], "required_evidence": not missing,
              "eligible_evidence": not invalid, "context_budget": result.get("context_chars", 0) <= case.get("max_chars", 6000),
              "no_external_calls": result["model_calls"] == result["network_calls"] == 0}
    return {"passed": all(checks.values()), "checks": checks, "missing": missing, "ineligible": invalid}


def evaluate(cases_path):
    data = json.loads(cases_path.read_text())
    results = []
    with tempfile.TemporaryDirectory(prefix="rust-knowledge-eval-") as temporary:
        for case in data["cases"]:
            for strategy in STRATEGIES:
                knowledge = LocalKnowledge(ConversationStore(Path(temporary) / (case["id"] + strategy + ".sqlite3")))
                prepare(knowledge, case)
                if strategy == "B2_exact_fts":
                    result = knowledge.retrieve(case["message"], case.get("previous"), max_chars=case.get("max_chars", 6000))
                else:
                    result = coarse(knowledge, case, eligibility=strategy == "B1_eligible_coarse")
                eligible, _ = knowledge.eligible(knowledge.registry.snapshot())
                judgment = grade(case, result, {document["id"] for document in eligible})
                results.append({"case_id": case["id"], "group": case["group"], "split": case["split"], "strategy": strategy,
                    **judgment, "status": result["status"], "selected": [document["id"] for document in result["documents"]],
                    "context_chars": result.get("context_chars", 0), "elapsed_ms": result["elapsed_ms"],
                    "excluded": result["excluded"], "model_calls": result["model_calls"], "network_calls": result["network_calls"]})
    summary = {}
    for strategy in STRATEGIES:
        rows = [row for row in results if row["strategy"] == strategy]
        latencies = sorted(row["elapsed_ms"] for row in rows)
        summary[strategy] = {"passed": sum(row["passed"] for row in rows), "total": len(rows),
            "reserved_passed": sum(row["passed"] for row in rows if row["split"] == "reserved"),
            "reserved_total": sum(row["split"] == "reserved" for row in rows),
            "mean_context_chars": round(statistics.mean(row["context_chars"] for row in rows), 2),
            "p50_ms": statistics.median(latencies), "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))],
            "model_calls": sum(row["model_calls"] for row in rows), "network_calls": sum(row["network_calls"] for row in rows)}
    sources = [*sorted((ROOT / "src/rust_coach/knowledge").glob("*.py")), ROOT / "src/rust_coach/knowledge/curated.json",
               ROOT / "src/rust_coach/calculations/rules.json", Path(__file__)]
    return {"version": data["version"], "baseline_commit": data["baseline_commit"],
        "dataset_sha256": hash_file(cases_path), "default_release_sha256": release_identity(default_release()),
        "source_hashes": {str(path.relative_to(ROOT)): hash_file(path) for path in sources},
        "case_count": len(data["cases"]), "summary": summary, "results": results,
        "limitations": [data["scope"], data["split_policy"],
            "B0/B1 reuse the existing relevant() coarse topic predicate on the same local retrieval view, same 5-document/6000-character budget. They are controlled adapters, not a replay of live web/model behavior at the baseline commit.",
            "Eligibility and status checks are software-contract fixtures; they do not establish semantic correctness of every curated claim or game build applicability.",
            "Latency measures one cold query, excludes package installation and model/network latency; no general production speedup is claimed."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/knowledge-cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite existing report")
    report = evaluate(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if report["summary"]["B2_exact_fts"]["passed"] == report["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
