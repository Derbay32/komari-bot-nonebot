"""``python -m komari_bot.cutover`` 入口：转发到 CLI 公共入口 main。"""

from __future__ import annotations

import sys

from komari_bot.cutover.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
