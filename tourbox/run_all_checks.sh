#!/usr/bin/env bash
# 一键验证：宽整数差分流 + 封版/来源身份 + 布局计划/锚点 + 端到端 CLI 走查
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_wide_int
python3 -m tests.test_bundle
python3 -m tests.test_planner
python3 -m tests.test_scale
python3 -m demo.cli_walkthrough /tmp/tourbox_verify_demo >/tmp/tourbox_demo_out.json
# 跨仓集成（旧 audiomix 仓位于同级目录时执行；仅适配器使用 numpy）
if [ -d "../audiomix/audiomix" ]; then
  PYTHONPATH="$(cd ../audiomix && pwd)" \
    python3 -m demo.audiomix_integration /tmp/tourbox_verify_integration
fi
echo
echo "================ tourbox 全量验证通过 ================"
