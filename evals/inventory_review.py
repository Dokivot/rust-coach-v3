import argparse
import json
from pathlib import Path

try:
    from .inventory_baseline import save_report
    from .inventory_evidence import compare_reports, file_digest, promote_feedback, review_template, validate_dataset
    from .inventory_grading import object_digest
except ImportError:
    from inventory_baseline import save_report
    from inventory_evidence import compare_reports, file_digest, promote_feedback, review_template, validate_dataset
    from inventory_grading import object_digest


def main():
    parser = argparse.ArgumentParser(description="Review captured evidence, compare versions, or promote human-verified feedback.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--review-template", type=Path)
    mode.add_argument("--compare", type=Path, nargs=2)
    mode.add_argument("--feedback-template", type=Path, help="Dataset with named images; creates a pending feedback record.")
    mode.add_argument("--promote-feedback", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path to preserve evidence history.")
    if args.review_template:
        result = review_template(json.loads(args.review_template.read_text()), file_digest(args.review_template))
    elif args.compare:
        result = compare_reports(*(json.loads(path.read_text()) for path in args.compare))
        result["sources"] = [{"path": str(path), "sha256": file_digest(path)} for path in args.compare]
    elif args.feedback_template:
        dataset = json.loads(args.feedback_template.read_text())
        if args.image not in dataset.get("images", {}):
            parser.error("Choose a named --image from the dataset.")
        result = {"version": "inventory-feedback-v1", "status": "pending", "dataset_sha256": object_digest(dataset),
                  "image": args.image, "annotation": dataset["images"][args.image]["annotation"],
                  "new_version": None, "reviewer": None, "reviewer_kind": None, "evidence": None,
                  "note": "Feedback is a candidate, never an automatic gold label or a player-state update."}
    else:
        if not args.cases:
            parser.error("--promote-feedback requires --cases.")
        result = promote_feedback(json.loads(args.cases.read_text()), json.loads(args.promote_feedback.read_text()))
        validate_dataset(result, args.output)
    save_report(args.output, result)
    print(json.dumps({"output": str(args.output), "mode": result.get("mode", result.get("version"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
