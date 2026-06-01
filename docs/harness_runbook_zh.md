# ProgramBench 测评 Harness Runbook

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

对每个实例，把 agent 工作区打成 `submission.tar.gz`：

```bash
RUN_DIR=/data/runs/my-agent-run
IID=ffmpeg__ffmpeg.360a402
AGENT_WORKSPACE=/data/agents/ffmpeg__ffmpeg.360a402/workspace

mkdir -p "$RUN_DIR/$IID"
tar -czf "$RUN_DIR/$IID/submission.tar.gz" -C "$AGENT_WORKSPACE" .
```

打包后不要继续修改该目录；如需修复候选代码，重新生成一个新的 run directory，方便结果追踪。

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
