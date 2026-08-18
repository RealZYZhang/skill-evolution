# 0006 — 受限 macOS 环境启动 Chrome 时应用注册崩溃

Status: Open
First observed: 2026-07-26

## Symptom

HTML Comparator 请求固定 viewport 截图时，Chrome 以 `exit_status_-6` 退出。macOS
诊断报告将其记录为 `EXC_CRASH`、`SIGABRT` 和 `Abort trap: 6`。

历史 batch 对五个 artifact 分别请求桌面和移动端截图，因缺少 batch 级熔断而重复
触发了等价崩溃。

## Reproduction

在当前 Codex 受限进程环境中，由 Python 启动系统安装的 Google Chrome，并传入
`--headless=new`、临时 `--user-data-dir`、固定窗口尺寸和本地 HTML URL。Chrome
在生成截图前退出。

相关 Harness：
`.skill-evolution/harness-runs/20260726T145302Z-166b1ee2/`。

## Diagnosis

崩溃栈经过
`TransformProcessType -> _RegisterApplication -> abort`，发生在 macOS 应用注册
阶段，而不是 HTML 解析或页面脚本执行阶段。现有证据表明当前 Codex 权限边界无法
让完整 Chrome 正常完成所需的 LaunchServices/WindowServer 注册。

Comparator 将标准错误丢弃，并在每个 viewport 独立启动 Chrome，所以报告只保留了
退出码，且第一次环境级失败后仍继续启动。

## Workaround

- 保留静态 Artifact Comparator 结果，并将截图状态标为 `partial`。
- 不在当前受限环境中继续使用相同 Chrome 启动方法。
- 如需截图，优先在获得明确授权后从沙箱外启动系统 Chrome，但使用独立临时 profile；
  或改用已验证的独立 headless runtime。

## Resolution

尚未修改执行代码。后续修复应加入：

- 一次性浏览器启动 preflight；
- 有限且脱敏的 stderr 记录；
- 遵循决策 0016 的 batch 级崩溃熔断；
- 成功截图与静态比较仍可独立完成的回归测试。
