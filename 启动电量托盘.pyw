# -*- coding: utf-8 -*-
"""双击启动雷蛇电量托盘（pythonw 运行，无黑窗）。"""
import os
import sys

_FALLBACK = r"D:\RazerBattery"

here = os.path.dirname(os.path.realpath(__file__))
if not os.path.exists(os.path.join(here, "razer_battery.py")):
    here = _FALLBACK
sys.path.insert(0, here)
os.chdir(here)

import razer_battery  # noqa: E402

sys.exit(razer_battery.main(sys.argv[1:]) or 0)
