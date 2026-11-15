"""Post a sample support request through the mock-mode pipeline and show the result.

    python scripts/simulate_teams_message.py                 # valid sample request
    python scripts/simulate_teams_message.py --invalid       # incomplete request
    python scripts/simulate_teams_message.py --file my.txt   # your own message
    python scripts/simulate_teams_message.py --duplicates 3  # redelivery test

Requires the app running with MOCK_MODE=true.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx

SAMPLE = """API Name: CustomerOrderAPI
Environment: QA

Issue Summary: Order creation is failing

Issue Description:
The API returns HTTP 500 when creating an order.
This started after the latest deployment.

Correlation ID: CORR-123456

Business Impact: Order processing is blocked in QA.

Priority: P2"""

INVALID = """API Name: CustomerOrderAPI
Environment: DEV
Priority: P5"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("OPSBRIDGE_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.getenv("ADMIN_API_KEY", "dev-admin-key"))
    parser.add_argument("--invalid", action="store_true")
    parser.add_argument("--file")
    parser.add_argument("--duplicates", type=int, default=1)
    parser.add_argument("--sender", default="Sample User")
    args = parser.parse_args()

    text = open(args.file, encoding="utf-8").read() if args.file else (INVALID if args.invalid else SAMPLE)
    headers = {"X-API-Key": args.api_key}
    with httpx.Client(base_url=args.url, headers=headers, timeout=10) as client:
        sim = client.post("/dev/simulate-message", json={
            "text": text, "sender_name": args.sender, "duplicate_deliveries": args.duplicates,
        })
        sim.raise_for_status()
        result = sim.json()
        print("Simulated delivery:", json.dumps(result))

        request_id = result["request_id"]
        terminal = {"COMPLETED", "REJECTED", "IGNORED", "FAILED"}
        detail = {}
        for _ in range(20):  # processing happens in the background worker
            detail = client.get(f"/support-requests/{request_id}").json()
            if detail["processing_status"] in terminal:
                break
            time.sleep(0.5)

        print(f"\nRequest #{request_id}: {detail['processing_status']}")
        if detail.get("jira_issue_key"):
            print(f"Jira issue: {detail['jira_issue_key']}  {detail['jira_issue_url']}")
        for err in detail.get("validation_errors") or []:
            print(f"  - {err['message']}")

        state = client.get("/dev/mock-state").json()
        replies = state["teams_replies"].get(result["message_id"], [])
        print(f"\nTeams replies in thread: {len(replies)}")
        for reply in replies:
            print("  " + reply[:300] + ("..." if len(reply) > 300 else ""))


if __name__ == "__main__":
    main()
