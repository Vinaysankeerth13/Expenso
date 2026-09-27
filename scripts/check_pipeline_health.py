"""Print pipeline health without sending notifications or calling external services."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import argparse
from app.pipeline_health import get_pipeline_health


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send the configured alert email if pipeline health is not healthy",
    )
    args = parser.parse_args()
    try:
        result = get_pipeline_health()
    except Exception as exc:
        result = {
            "status": "failed",
            "reasons": ["Database health check failed"],
            "error_code": type(exc).__name__,
        }
    print(json.dumps(result, default=str, indent=2))
    if args.notify and result["status"] != "healthy":
        from app.gmail_auth import send_email

        try:
            send_email(
                "Expenso Alert: Pipeline needs attention",
                json.dumps(result, default=str, indent=2),
            )
        except Exception as exc:
            print(f"Alert delivery failed: {type(exc).__name__}", file=sys.stderr)
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
