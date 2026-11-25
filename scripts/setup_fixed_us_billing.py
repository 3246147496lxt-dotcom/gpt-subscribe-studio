#!/usr/bin/env python3
"""生成固定美国免税账单文件（GSS_FIXED_BILLING_FILE）。

地址只写一次，之后所有订单共用；出口代理可以按订单自由切换。
文件必须位于仓库之外、绝对路径、权限 0600 或 0400，服务才会读取。

示例::

    python scripts/setup_fixed_us_billing.py \
        --name "Daniel Mercer" \
        --email billing@example.com \
        --line1 "1425 SW Morrison Street" \
        --city Portland --state OR --postal-code 97205

然后按输出提示导出：::

    export GSS_FIXED_BILLING_FILE="$HOME/.gss/fixed-billing.json"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core" / "src"))

from gss_core.fixed_billing import (  # noqa: E402
    DEFAULT_ADDRESS,
    TAX_FREE_STATES,
    validate_address,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the one fixed US tax-free billing address")
    parser.add_argument("--name", default=DEFAULT_ADDRESS["name"])
    parser.add_argument("--email", default=DEFAULT_ADDRESS["email"])
    parser.add_argument("--line1", default=DEFAULT_ADDRESS["line1"])
    parser.add_argument("--city", default=DEFAULT_ADDRESS["city"])
    parser.add_argument("--state", default=DEFAULT_ADDRESS["state"],
                        help=f"tax-free state: {', '.join(sorted(TAX_FREE_STATES))}")
    parser.add_argument("--postal-code", default=DEFAULT_ADDRESS["postal_code"])
    parser.add_argument("--out", default=str(Path.home() / ".gss" / "fixed-billing.json"))
    parser.add_argument("--dry-run", action="store_true", help="print the JSON without writing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    address = validate_address({
        "name": args.name,
        "email": args.email,
        "line1": args.line1,
        "city": args.city,
        "state": args.state.upper(),
        "postal_code": args.postal_code,
        "country": "US",
    })
    text = json.dumps(address, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(text, end="")
        return 0

    target = Path(args.out).expanduser()
    if not target.is_absolute():
        print("--out 必须是绝对路径", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    os.chmod(target, 0o600)

    print(f"已写入固定账单：{target}")
    print()
    print("启动服务前导出：")
    print(f'  export GSS_FIXED_BILLING_FILE="{target}"')
    print()
    print("可选：需要严格绑定出口与账单国时（默认关闭）：")
    print("  export GSS_REQUIRE_EXIT_BILLING_MATCH=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
