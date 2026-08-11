# ProgramBench 测评 Harness Runbook

English version: [docs/harness_runbook_en.md](harness_runbook_en.md)

## 适用范围

本文面向需要用 ProgramBench 作为评测 harness 的实验维护者。目标是把一个 SWE-agent 生成的代码库批量打包、运行官方测试、汇总分数，并保留可复现记录。

## 0. 前置条件

- 使用 Linux x86_64 主机。ProgramBench Docker 镜像按 `linux/amd64` 构建，不建议在 macOS/Windows 上通过 QEMU 仿真评测。
- 安装 Docker，并确认当前用户能运行 `docker ps`。
- 安装 `uv`，在仓库根目录执行：

```bash
uv sync
```

- 评测阶段需要访问 Docker Hub 和 HuggingFace，除非镜像和 test blobs 已提前缓存。推理阶段必须隔离互联网，agent 只能访问 cleanroom 二进制和文档。

快速自检：

```bash
uname -m
command -v uv
uv sync
docker version
docker ps
uv run programbench --help
uv run pytest -q
```

如果 `uv` 不存在，可按官方安装脚本安装到用户目录，并用绝对路径执行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
/home/"$USER"/.local/bin/uv sync
```

如果 Docker 已安装但 `docker ps` 报 socket 权限错误，生产环境优先把当前用户加入 `docker` 组并重新登录：

```bash
sudo usermod -aG docker "$USER"
```

临时自检时也可以使用一个 sudo wrapper，并通过 `PROGRAMBENCH_DOCKER_EXECUTABLE` 交给 ProgramBench。注意 wrapper 需要在 `docker cp` 后把拷出的临时文件 owner 改回当前用户，否则 harness 清理 JUnit XML 临时文件时可能触发 `PermissionError`：

```bash
cat >/tmp/programbench-docker <<'EOF'
#!/bin/bash
sudo docker "$@"
rc=$?
if [ "$rc" -eq 0 ] && [ "${1:-}" = "cp" ]; then
    last="${@: -1}"
    case "$last" in
        *:*) ;;
        *) sudo chown "$(id -u):$(id -g)" "$last" 2>/dev/null || true ;;
    esac
fi
exit "$rc"
EOF
chmod +x /tmp/programbench-docker
export PROGRAMBENCH_DOCKER_EXECUTABLE=/tmp/programbench-docker
```

## 1. 输入契约

每个待评测实例必须放在一个 run directory 下，目录名等于 ProgramBench instance id，内部只有候选代码压缩包 `submission.tar.gz`：

```text
my-agent-run/
  ffmpeg__ffmpeg.360a402/
    submission.tar.gz
  sharkdp__fd.40d8eb3/
    submission.tar.gz
```

`submission.tar.gz` 应展开为 agent 生成的完整代码库根目录内容，不要多包一层无意义的 wrapper 目录。可用以下命令检查：

```bash
tar -tzf my-agent-run/ffmpeg__ffmpeg.360a402/submission.tar.gz | head
```

镜像命名规则：instance id 中的 `__` 会映射为 `_1776_`。例如 `ffmpeg__ffmpeg.360a402` 的 Docker image 是 `programbench/ffmpeg_1776_ffmpeg.360a402`。推理使用 `task_cleanroom` tag，评测默认使用 `task` tag。

## 2. 打包候选结果

### Candidate Adapter

使用内置 adapter 让 first-pass 与 remediation 候选保持配对，冻结 reference probes，生成确定性 archive，并在正式评测前发现环境漂移：

```bash
# 在 remediation 之前 clone 同一个 first-pass candidate。
uv run programbench candidate clone /data/first-pass/$IID /data/remediation/$IID

# 冻结 reference 行为，并拒绝 candidate regression。
uv run programbench candidate probe \
  /data/probes/$IID/cases.json \
  /data/probes/$IID/ledger.json \
  /data/first-pass/$IID/reference \
  /data/remediation/$IID/executable

# 修复后只保留在精确 predecessor 上失败、且已稳定匹配 reference 的反例；
# 每次调用都会先重放此前保留的全部修复。
uv run programbench candidate retain \
  /data/probes/$IID/discoveries.json \
  /data/probes/$IID/regressions.json \
  /data/first-pass/$IID/reference \
  /data/first-pass/$IID/executable \
  /data/remediation/$IID/executable

# 只打包候选源码。
uv run programbench candidate package /data/remediation/$IID "$RUN_DIR" "$IID"

# 使用 evaluator 相同的 cleanroom 与重定位 /workspace 路径执行断网编译。
uv run programbench candidate preflight "$RUN_DIR" "$IID" --docker-cpus 4
```

`cases.json` 是 `{"argv": [...], "stdin": "..."}` 组成的 JSON list。第一次运行 `probe` 会创建不可变 ledger，记录 reference binary hash 以及精确的 exit/stdout/stderr/timeout；后续 cases 发生变化会 fail closed，candidate 出现 regression 时命令返回非零。若 probe 文件位于 workspace 内，应放在 `.programbench/` 下，打包时会自动排除。

`retain` 只在三个 subject 各自连续运行两次结果一致、精确 predecessor 与 reference 不同、修复后的 candidate 与 reference 相同时追加反例。Ledger 保存输入、精确观测、executable hash 与链式 head hash。每次调用可以提出不同的 cases，但已经保留的反例不能被遗漏：即使 `discoveries.json` 为空，后继 candidate 仍须重放全部历史反例。若发生回退，命令以非零状态退出且不修改 ledger。输出的 `head_sha256` 可绑定到外部 promotion 或 handoff 记录。

这些只是有限的本地观测，并不访问 ProgramBench 的官方隐藏评测。其价值在于把已经证明有效的修复变成持久证据，而不是 executor 的记忆。

Adapter 只负责 candidate layout 和 cleanroom 机制，不定义 agent goal、completion 或 acceptance policy。

对每个实例，把 agent 工作区打成 `submission.tar.gz`：

```bash
RUN_DIR=/data/runs/my-agent-run
IID=ffmpeg__ffmpeg.360a402
AGENT_WORKSPACE=/data/agents/ffmpeg__ffmpeg.360a402/workspace

mkdir -p "$RUN_DIR/$IID"
tar --owner=0 --group=0 --numeric-owner \
  -czf "$RUN_DIR/$IID/submission.tar.gz" \
  -C "$AGENT_WORKSPACE" .
```

打包后不要继续修改该目录；如需修复候选代码，重新生成一个新的 run directory，方便结果追踪。
`--owner=0 --group=0 --numeric-owner` 很重要：ProgramBench 会在 Docker 容器中以 root 解包提交，如果 tar 内的 `./` 记录带有宿主用户 UID，可能把 `/workspace` 改成非 root owner，导致后续 `seed_git` 步骤触发 Git `dubious ownership` 错误。

## 3. 预热缓存

评测会按需拉取 Docker 镜像和 HuggingFace test blobs。正式批量评测前建议预热：

```bash
uv run programbench blob sync
docker pull programbench/ffmpeg_1776_ffmpeg.360a402:task
```

只预热单个实例的 test blobs：

```bash
uv run programbench blob sync ffmpeg__ffmpeg.360a402
```

常用环境变量：

```bash
export PROGRAMBENCH_DOCKER_EXECUTABLE=docker
export PROGRAMBENCH_DOCKER_CPUS=10
export PROGRAMBENCH_DOCKER_ORG=programbench
export PROGRAMBENCH_HF_REPO=programbench/ProgramBench-Tests
export PROGRAMBENCH_HF_REVISION=main
```

## 4. Smoke Test

先跑一个小子集，确认目录结构、Docker、blob 下载和候选包都正常：

```bash
uv run programbench eval "$RUN_DIR" \
  --filter '^ffmpeg__ffmpeg\.360a402$' \
  -w 1 \
  -b 1 \
  --docker-cpus 4
```

如果只想按顺序抽样，可以用：

```bash
uv run programbench eval "$RUN_DIR" --slice 0:5 -w 1 -b 1
```

### 本地 fixture 自检

仓库内置了 `testorg__calculator.abc1234` fixture，可用于不依赖 HuggingFace 下载的本地 harness 自检。这个 fixture 是测试用资源，不等同于正式 ProgramBench 镜像；当前本地 Dockerfile 需要补上 `git` 和 `pytest-xdist`，测试 blob 的 `run.sh` 也需要临时调整 JUnit test name，才能完整模拟正式 harness 的成功路径。以下命令只写 `/tmp` 和本地 Docker 镜像，不修改仓库 fixture 文件：

```bash
IID=testorg__calculator.abc1234
IMAGE=programbench/testorg_1776_calculator.abc1234:task
BRANCH=33128f6b8600
RUN_DIR=/tmp/programbench-smoke-run
WORK=/tmp/programbench-smoke-work
BLOB=/tmp/programbench-smoke-blobs
DOCKER=${PROGRAMBENCH_DOCKER_EXECUTABLE:-docker}

"$DOCKER" build -t "$IMAGE" "src/programbench/data/tasks/$IID"
cid=$("$DOCKER" run -d "$IMAGE" sleep 2h)
"$DOCKER" exec "$cid" bash -lc \
  'apt-get update &&
   DEBIAN_FRONTEND=noninteractive apt-get install -y git &&
   pip3 install -q --disable-pip-version-check pytest-xdist &&
   rm -rf /var/lib/apt/lists/*'
"$DOCKER" commit "$cid" "$IMAGE"
"$DOCKER" rm -f "$cid"

rm -rf "$RUN_DIR" "$WORK" "$BLOB" /tmp/programbench-smoke-branch
mkdir -p "$RUN_DIR/$IID" "$WORK/correct" "$BLOB/$IID/tests" /tmp/programbench-smoke-branch

uv run python -m zipfile -e \
  "src/programbench/data/test_runs/correct/$IID/submission.zip" \
  "$WORK/correct"
tar --owner=0 --group=0 --numeric-owner \
  -czf "$RUN_DIR/$IID/submission.tar.gz" \
  -C "$WORK/correct" .

cp -a "src/programbench/data/tasks/$IID/tests/$BRANCH/." /tmp/programbench-smoke-branch/
uv run python - <<'PY'
from pathlib import Path

p = Path("/tmp/programbench-smoke-branch/eval/run.sh")
text = p.read_text()
text = text.replace(
    "python3 -m pytest eval/tests/ --junitxml=eval/results.xml -v || true",
    "rm -rf tests\ncp -a eval/tests tests\npython3 -m pytest tests/ --junitxml=eval/results.xml -v || true",
)
p.write_text(text)
PY
tar --owner=0 --group=0 --numeric-owner \
  -czf "$BLOB/$IID/tests/$BRANCH.tar.gz" \
  -C /tmp/programbench-smoke-branch .

PROGRAMBENCH_BLOB_DIR="$BLOB" \
uv run programbench eval "$RUN_DIR" \
  --filter '^testorg__calculator\.abc1234$' \
  -w 1 \
  -b 1 \
  --docker-cpus 2 \
  --force

uv run programbench info "$RUN_DIR"
```

期望输出中应看到：

```text
testorg__calculator.abc1234     ✅  3 tests
Average                        100  1 instances
```

## 5. 正式评测

基础命令：

```bash
uv run programbench eval "$RUN_DIR"
```

并行评测：

```bash
uv run programbench eval "$RUN_DIR" -w 4 -b 1 --docker-cpus 8
```

`-w/--workers` 控制并行实例数，`-b/--branch-workers` 控制单实例内并行测试分支数。活跃容器数大致为 `workers * branch_workers`，每个容器会获得 `--docker-cpus` 指定的 CPU 配额。资源不足时先增加 `workers`，谨慎增加 `branch-workers`。

把评测输出写到单独目录，避免污染原始 run：

```bash
OUT=/data/evals/programbench-$(date +%Y%m%d)
uv run programbench eval "$RUN_DIR" -o "$OUT" -w 4 -b 1 --docker-cpus 8
```

使用 `-o` 后，结果路径是 `$OUT/$(basename "$RUN_DIR")/<instance_id>/<instance_id>.eval.json`。

## 6. 结果汇总

原地评测后：

```bash
uv run programbench info "$RUN_DIR"
```

使用 `-o` 后：

```bash
uv run programbench info "$OUT/$(basename "$RUN_DIR")"
```

最终分数应以 `programbench info` 为准，因为它会根据当前 `tests.json` 过滤 ignored branches 和 ignored tests。汇总表里只有 `✅` 表示完全解决；显示为 `100` 的四舍五入分数不等价于 solved。

每个实例会生成 `<instance_id>.eval.json`，关键字段包括：

- `test_results`: 每个测试的 `name`、`branch`、`status` 和额外信息。
- `error_code` / `error_details`: 顶层评测错误，正常时为 `null`。
- `test_branch_errors`: 分支级错误。
- `warnings`: harness 完整性或数据一致性警告。
- `log`: 每个评测步骤的命令、输出、耗时和返回码。

## 7. 复跑策略

默认情况下，`programbench eval` 会跳过已经完整评测的实例，并补跑缺失或出错的分支。强制全量复跑：

```bash
uv run programbench eval "$RUN_DIR" --force -w 4 -b 1
```

只读取已有 JSON 并重新汇总：

```bash
uv run programbench eval "$RUN_DIR" --summarize-only
uv run programbench info "$RUN_DIR"
```

pytest-xdist worker crash 会按 `--branch-retries` 重试，默认值为 `1`。如需关闭：

```bash
uv run programbench eval "$RUN_DIR" --branch-retries 0
```

## 8. 故障排查

- `no_submission`: 对应实例缺少 `<instance_id>/submission.tar.gz`。
- 顶层 `error_code` 非空：构建、解包、容器或评测主流程失败，先看 `error_details` 和 `log`。
- `test_branch_errors` 非空：某些测试分支没有干净完成，需要检查该分支日志。
- `system_error` 或 `not_run`: 不要当成普通测试失败处理；这通常表示 harness、JUnit XML 或测试执行完整性存在问题。
- `warnings` 非空：查看是否有缺失测试、未知测试或 ignored branch 相关信息，正式报告前应解释清楚。
- `PermissionError` 且堆栈在 `_copy_file_from_container` / `host_tmp.unlink`: 通常是通过 `sudo docker cp` 拷出的临时 XML 变成 root-owned；使用正常 docker 组权限，或让 sudo wrapper 在 `docker cp` 后 `chown` 目标文件。
- `seed_git_failed` 且日志包含 `git: command not found`: 评测镜像缺少 Git。正式 ProgramBench 镜像应包含 harness 需要的工具；如果是本地 fixture 镜像，按「本地 fixture 自检」步骤补装 `git`。
- `seed_git_failed` 且日志包含 `dubious ownership`: 重新检查 `submission.tar.gz` 是否用 `--owner=0 --group=0 --numeric-owner` 打包；可用 `tar -tvf <submission.tar.gz> | head` 确认 owner/group 显示为 `0/0`。
- `results_read_failed` 且 `run_tests` 输出包含 `unrecognized arguments: --max-worker-restart=4`: 镜像内 pytest 缺少 `pytest-xdist`，导致 harness 注入的 xdist 参数无法识别，测试没有生成 `eval/results.xml`。
- 日志显示 `expected tests missing from JUnit XML` 或 `test(s) in JUnit XML not in tests.json`: JUnit testcase 名称和 `tests.json` 不一致。正式评测时先确认 test blob 与当前 ProgramBench 数据版本一致；本地 `testorg__calculator.abc1234` fixture 需要按「本地 fixture 自检」步骤临时调整 `run.sh` 的 pytest 收集路径。
- Docker 拉取失败：确认镜像 tag、网络和 `PROGRAMBENCH_DOCKER_ORG`。
- HuggingFace blob 下载失败：先执行 `uv run programbench blob sync <instance_id>` 单独定位。

## 9. 可复现记录

每次正式评测至少记录：

- ProgramBench git commit 或发布版本。
- 完整评测命令和所有 `PROGRAMBENCH_*` 环境变量。
- Docker 版本、CPU 配额、`workers`、`branch-workers`、`branch-retries`。
- run directory 的只读路径和每个 `submission.tar.gz` 的 sha256。
- 输出目录、`programbench info` 汇总文本、全部 `<instance_id>.eval.json`。

## 10. 推荐执行流程

```bash
uv sync
uv run programbench blob sync

RUN_DIR=/data/runs/my-agent-run
uv run programbench eval "$RUN_DIR" --slice 0:3 -w 1 -b 1
uv run programbench info "$RUN_DIR"

OUT=/data/evals/programbench-$(date +%Y%m%d)
uv run programbench eval "$RUN_DIR" -o "$OUT" -w 4 -b 1 --docker-cpus 8
uv run programbench info "$OUT/$(basename "$RUN_DIR")"
```

## 11. ProgramBench 作为冻结外部评测环境（auto-research lane 契约）

下游 auto-research harness 通过 **evaluation-environment（评测环境）** 协议消费
ProgramBench，其中 ProgramBench 是一个 **frozen_external（冻结外部）** 评测器——
不可变、回路中无 agent 的验证器。本节记录下游强制执行的契约，帮助 harness 维护者
保持 ProgramBench 证据达到可接受（acceptance-grade）标准。

- **评测器类型 — `frozen_external`。** ProgramBench 用不可变的官方测试、且回路中
  没有 agent 来给候选打分，因此其分数有资格作为 *acceptance evidence（可接受证据）*。
  自评或可变的评测器没有这个资格；绝不要让候选自带的检查替代一次正式的 ProgramBench
  评测。
- **不可变锚点 — evaluator hash。** 在任何评测 fan-out 之前，消费方 lane 会冻结一个
  一次性写入的 *preregistration lock（预登记锁）*，以 evaluator hash 为键：ProgramBench
  git commit（或发布版本）加上每个实例的 test-blob / `tests.json` 身份。这正是 §9 中
  “ProgramBench git commit 或发布版本”记录被提升为机器校验的锚点——锁定后若 baseline、
  split、实例集合或 evaluator hash 发生变化，将 fail closed。
- **主指标 — `official_filtered_pass_rate`。** 分数取自 `programbench info`（§6）：
  它按当前 `tests.json` 过滤被忽略的 branch/test，且只有 `✅` 才算解决。四舍五入显示为
  `100` 不等于“已解决”。
- **Split — calibration 与 confirmation/holdout。** calibration 运行仅用于迭代，
  **不可晋升（non-promotable）**。可晋升（acceptance-grade）的证据要求一个与每个
  calibration 实例 **不相交（disjoint）** 的 **confirmation** 或 **holdout** 实例，
  并且相对预登记的公开 baseline 非回归。
- **Conformance（一致性）往返。** 用已知分数的 golden 候选对 ProgramBench 做一次真实
  评测，就是让证据达到 acceptance-grade 的 *非 agent 验证*。本地
  `testorg__calculator.abc1234` fixture（§4）**仅**是 harness/管路自检——合成 fixture
  证据永远不是 acceptance-grade。
- **Cleanroom（净室）。** 推理使用 `task_cleanroom` 镜像 tag（§1）；构造候选期间不要
  读取 `tests/test*` blob。官方评测只在打包之后运行（§2）。

总之：§9 的可复现记录 *就是* 不可变锚点，`programbench info` 过滤后的 `✅` 分数
*就是* 主指标，而一次在不相交 confirmation/holdout 实例上的真实（而非 fixture）
ProgramBench 运行 *就是* acceptance-grade 证据。
