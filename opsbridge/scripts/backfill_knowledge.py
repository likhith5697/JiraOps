"""Build knowledge entries for already-closed requests and (re)embed entries missing embeddings.

    docker compose exec opsbridge python scripts/backfill_knowledge.py
    docker compose exec opsbridge python scripts/backfill_knowledge.py --retry-skipped   # retry tickets skipped earlier

Safe to run repeatedly: one entry per request; already-embedded entries are left alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(container, *, retry_skipped: bool = False, batch: int = 50) -> dict[str, int]:  # noqa: ANN001
    totals: dict[str, int] = {}
    while True:
        counts = container.knowledge.process_pending(batch, include_skipped=retry_skipped)
        if not counts:
            break
        for status, n in counts.items():
            totals[status] = totals.get(status, 0) + n
        if retry_skipped:  # skipped rows stay SKIPPED, so one pass is enough
            break
    embedded = 0
    while True:
        n = container.knowledge.embed_missing(batch)
        embedded += n
        if n < batch:
            break
    totals["EMBEDDED"] = embedded
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--retry-skipped", action="store_true", help="also retry tickets skipped for lack of a reason")
    args = parser.parse_args()

    from app.config import get_settings
    from app.container import build_container

    container = build_container(get_settings(), sync_dispatch=True)
    container.db.create_all()
    try:
        totals = run(container, retry_skipped=args.retry_skipped)
    finally:
        container.close()
    print("Knowledge backfill:", ", ".join(f"{k.lower()}={v}" for k, v in sorted(totals.items())) or "nothing to do")


if __name__ == "__main__":
    main()
