"""Read-only wall quality report from the running local engine; does not place orders."""

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--qualified-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with urllib.request.urlopen(args.url.rstrip("/") + "/api/walls", timeout=10) as response:
        walls = json.load(response)
    if args.qualified_only:
        walls = [w for w in walls if w.get("qualified")]
    report = {
        "observed_ns": time.time_ns(),
        "source": args.url,
        "walls": walls,
        "note": "Quality is an observed heuristic, not proof of intent or future profit.",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
