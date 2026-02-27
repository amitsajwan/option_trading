from __future__ import annotations

import time


def main() -> None:
    print("[ingestion.collectors.depth] collector disabled; ingestion API serves market data directly")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
