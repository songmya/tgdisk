#!/usr/bin/env python3
"""清理 multipart 上传孤儿记录。

用法：
    python3 cleanup_orphans.py            # dry-run，仅列出
    python3 cleanup_orphans.py --apply    # 实际软删除
    python3 cleanup_orphans.py --hours 72 # 阈值改为 72 小时

建议加 cron：
    0 5 * * *  cd /vol1/1000/openclaw/tgdisk && /usr/bin/python3 scripts/cleanup_orphans.py --apply >> data/cleanup.log 2>&1
"""

import sys
import os
import asyncio
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_io import cleanup_orphans


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24,
                    help="超过多少小时未完成视为孤儿（默认 24）")
    ap.add_argument("--apply", action="store_true",
                    help="实际执行软删除；默认 dry-run")
    args = ap.parse_args()

    res = await cleanup_orphans(hours=args.hours, dry_run=not args.apply)
    print(f"[orphan-cleanup] hours={args.hours} apply={args.apply} "
          f"count={res['count']}")
    for r in res["orphans"]:
        print(f"  id={r['id']:>6}  got={r['got']:>4}/{r['chunk_count']}  "
              f"name={r['file_name']}  created={r['created_at']}")


if __name__ == "__main__":
    asyncio.run(main())
