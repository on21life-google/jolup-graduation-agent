"""Build only the public-document index. Never accepts student files."""
from pathlib import Path
import sys
import argparse
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jolup.rag import KnowledgeBase

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--lexical", action="store_true", help="Explicit non-vector test mode")
    parser.add_argument("--query")
    args = parser.parse_args()
    kb = KnowledgeBase(mode="lexical" if args.lexical else "vector")
    report = kb.build_index(force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.query:
        print(json.dumps(kb.answer(args.query), ensure_ascii=False, indent=2))
    if not args.lexical and report["backend"] != "chroma_fastembed":
        sys.exit(2)
