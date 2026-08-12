"""让全量测试不受生产环境功能开关影响。

pytest 会在收集阶段导入所有测试模块；部分院子联调测试会导入 ``home``，
而生产启动流程会按设计先加载 ``.env`` 再导入 ``garden``。若测试入口不先
给出中性默认值，生产开关就会在收集阶段泄漏到无关的旧版生命周期测试里。

现实天气与作物异常专项会在需要时用 ``patch.dict`` 显式开启；这里仅固定
默认基线，避免测试结果随模块收集顺序改变。
"""

import os
import tempfile
from pathlib import Path


os.environ["GARDEN_REAL_ENVIRONMENT_ENABLED"] = "0"
os.environ["GARDEN_NATURAL_CROP_CONDITIONS_ENABLED"] = "0"

# 活动日志全局改道：部分联调测试经 home.py 触发 log_store 活动日志，
# 单个测试忘了 patch 就会把"给豆包喂食"这类测试动物的记录写进生产
# logs.jsonl（2026-08-10 用户在前端日志页看到 88 条测试公鸡记录）。
# 这里在收集阶段就把 LOG_FILE 指向临时文件，任何测试都污染不到生产；
# 需要断言日志内容的专项（test_log_store 等）仍按各自的 patch 生效。
import log_store  # noqa: E402

log_store.LOG_FILE = Path(tempfile.gettempdir()) / "xiaoyu-test-logs.jsonl"
