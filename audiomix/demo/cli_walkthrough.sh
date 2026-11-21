#!/usr/bin/env bash
# 端到端命令走查：素材登记 -> 编辑（含拒绝）-> 点查询 -> 发布 -> 渲染 -> 读取/追溯 -> 验证
set -euo pipefail
ROOT=/tmp/audiomix_cli
rm -rf "$ROOT"
REG="$ROOT/registry"
ARCH="$ROOT/archive"
PROD="$ROOT/product.json"
PY="python3 -m audiomix.cli"

echo "== 1. 素材登记（短整数序列，8k/16k/48k） =="
A8=$($PY asset-seq --root "$REG" --rate 8000  --values 0,100,200,300,400 --note hand8k  | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
A16=$($PY asset-seq --root "$REG" --rate 16000 --values 0,100,200,300,400,500 --note hand16k | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
A48=$($PY asset-seq --root "$REG" --rate 48000 --values 0,1,2,3,4,5,6 --note hand48k | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
echo "  A8=$A8"; echo "  A16=$A16"; echo "  A48=$A48"
$PY asset-list --root "$REG" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  登记素材数:",len(d))'

echo "== 2. 编辑：插入（跨采样率/2倍速/半倍速/淡化/重复引用/重叠） =="
$PY tl-insert --root "$REG" --clip-id c1 --asset-id "$A16" --a 1 --b 5 --start 0
$PY tl-insert --root "$REG" --clip-id c2 --asset-id "$A8"  --a 0 --b 4 --start 1/4000
$PY tl-insert --root "$REG" --clip-id c3 --asset-id "$A16" --a 0 --b 6 --start 0 --speed 2
$PY tl-insert --root "$REG" --clip-id c4 --asset-id "$A16" --a 0 --b 3 --start 0 --speed 1/2
$PY tl-insert --root "$REG" --clip-id c5 --asset-id "$A48" --a 0 --b 7 --start 3/16000 --fade-in 1/16000 --fade-out 1/16000
$PY tl-insert --root "$REG" --clip-id c6 --asset-id "$A16" --a 0 --b 2 --start 0 --gain 2
$PY tl-insert --root "$REG" --clip-id c7 --asset-id "$A16" --a 0 --b 2 --start 0 --gain -1

echo "== 3. 整笔拒绝：源区间越界 / 未知素材（状态不变） =="
set +e
$PY tl-insert --root "$REG" --clip-id bad1 --asset-id "$A8" --a 0 --b 99 --start 0
echo "  退出码=$?"
$PY tl-insert --root "$REG" --clip-id bad2 --asset-id deadbeefdeadbeef --a 0 --b 1 --start 0
echo "  退出码=$?"
set -e

echo "== 4. 不渲染点查询：t=0 上的片段与源实数位置 =="
$PY tl-at --root "$REG" --time 0

echo "== 5. 区间查询、修改、移动、删除 =="
$PY tl-query --root "$REG" --lo 0 --hi 1/8000 | python3 -c 'import sys,json;print("  命中 clip:",[c["clip_id"] for c in json.load(sys.stdin)])'
$PY tl-modify --root "$REG" --clip-id c3 --set gain=3
$PY tl-move   --root "$REG" --clip-id c3 --new-start 1/8000
$PY tl-delete --root "$REG" --clip-id c7

echo "== 6. 发布不可变产品 =="
$PY tl-publish --root "$REG" --out "$PROD"

echo "== 7. 产品校验 + 参考解释器 vs 分块渲染逐样本核对 =="
$PY verify-product --product "$PROD" --registry-root "$REG"
$PY render-compare --product "$PROD" --registry-root "$REG" --T 1/1000

echo "== 8. 渲染入仓（含块峰值/削顶/来源索引） =="
$PY render --archive-root "$ARCH" --product "$PROD" --registry-root "$REG" --T 1/1000 --render-id walk1
$PY render-list --archive-root "$ARCH"
$PY render-show --archive-root "$ARCH" --render-id walk1

echo "== 9. 范围读取 + 块追溯 + 导出 WAV =="
$PY read  --archive-root "$ARCH" --render-id walk1 --start 0 --end 8
$PY trace --archive-root "$ARCH" --render-id walk1 --start 0 --end 1600
$PY export-wav --archive-root "$ARCH" --render-id walk1 --out "$ROOT/walk1.wav"

echo "== 10. 外部产品独立入仓 + 编辑器私有文件移走后旧 render 仍可读 =="
cp "$PROD" "$ROOT/external_product.json"
rm -f "$REG"/timeline_*.json "$REG"/timeline_*.jsonl
$PY verify-product --product "$ROOT/external_product.json" --registry-root "$REG"
$PY render --archive-root "$ARCH" --product "$ROOT/external_product.json" \
           --registry-root "$REG" --T 1/1000 --render-id walk-external
$PY verify-integrity --archive-root "$ARCH"
echo "全部命令走查通过"
