import argparse
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from rust_coach.config import Settings
from rust_coach.contracts import AppError
from rust_coach.knowledge.service import LocalKnowledge
from rust_coach.persistence import ConversationStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定演示知识包的本地检查、发布与回退；不会认证游戏build")
    parser.add_argument("--database", type=Path, default=Settings().database_path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("rebuild-index")
    export = commands.add_parser("export")
    export.add_argument("output", type=Path)
    install = commands.add_parser("install")
    install.add_argument("package", type=Path)
    activate = commands.add_parser("activate", help="发布或回退到已安装的不可变包")
    activate.add_argument("release_id")
    activate.add_argument("--sha256", required=True)
    activate.add_argument("--review-note", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("release_id")
    revoke.add_argument("document_id")
    revoke.add_argument("--reason", required=True)
    commands.add_parser("feedback")
    review = commands.add_parser("review-feedback")
    review.add_argument("conversation_id")
    review.add_argument("request_id")
    review.add_argument("--disposition", choices=["rejected", "needs_release"], required=True)
    review.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        knowledge = LocalKnowledge(ConversationStore(args.database))
        registry = knowledge.registry
        if args.command == "rebuild-index":
            result = registry.rebuild_index()
        elif args.command == "install":
            result = registry.install(json.loads(args.package.read_text()), knowledge.catalog.sha256)
        elif args.command == "activate":
            registry.activate(args.release_id, args.sha256, args.review_note)
            result = knowledge.reference()
        elif args.command == "export":
            with args.output.open("x") as output:
                json.dump(registry.snapshot()["release"], output, ensure_ascii=False, indent=2)
                output.write("\n")
            result = {"exported": str(args.output), **knowledge.reference()}
        elif args.command == "revoke":
            registry.revoke(args.release_id, args.document_id, args.reason)
            result = knowledge.reference()
        elif args.command == "feedback":
            with registry.store.connection() as connection:
                result = [{"conversation_id": row["conversation_id"], **json.loads(row["payload"])}
                          for row in connection.execute("SELECT conversation_id,payload FROM knowledge_feedback")]
        elif args.command == "review-feedback":
            result = registry.review_feedback(args.conversation_id, args.request_id, args.disposition, args.note)
        else:
            result = registry.snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AppError, ValidationError, ValueError, OSError, sqlite3.DatabaseError) as error:
        print(json.dumps({"error": getattr(error, "code", type(error).__name__), "message": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
