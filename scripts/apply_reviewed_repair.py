"""Preview or atomically apply an explicitly reviewed historical repair manifest."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.reviewed_repair import run_repair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--expect-digest", help="Apply only if this preview digest still matches"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Reserve a private receipt before doing any database work. The durable
    # audit table remains authoritative if writing the local receipt fails.
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        result = run_repair(
            json.loads(args.manifest.read_text()), expected_digest=args.expect_digest
        )
        json.dump(result, stream, default=str, indent=2)
        stream.write("\n")
    print("Applied" if result["applied"] else "Read-only preview", result["actions"])
    print("Preview digest:", result["digest"])
    print("Receipt:", args.output)


if __name__ == "__main__":
    main()
