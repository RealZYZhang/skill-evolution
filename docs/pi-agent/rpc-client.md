# Python RPC client

The project client is [`scripts/pi_rpc.py`](../../scripts/pi_rpc.py). It starts
Pi as a child process and implements the strict LF-delimited JSONL protocol
described in the pinned [RPC reference](upstream/rpc.md).

## Smoke test

The state request does not invoke a model:

```bash
python3 scripts/pi_rpc.py state
```

Run a prompt using the configured Pi provider and model:

```bash
python3 scripts/pi_rpc.py prompt \
  --prompt-file prompts/execution/document-html-visualizer-v1.md \
  --skill skills/document-html-visualizer-skill
```

The prompt file must have a matching project-owner approval. The low-level
`raw` command rejects RPC commands whose type is `prompt`.

Pass Pi options before the action. An option beginning with `-` uses the
`--pi-arg=<value>` form:

```bash
python3 scripts/pi_rpc.py \
  --pi-arg=--provider \
  --pi-arg=openai \
  --pi-arg=--model \
  --pi-arg=gpt-5 \
  prompt \
  --prompt-file prompts/execution/document-html-visualizer-v1.md \
  --skill skills/document-html-visualizer-skill
```

By default the client adds `--mode rpc --no-session`. Use `--session` when the
Pi session file is needed for debugging. Use `--approve-project` only for a
trusted working tree whose project skills and extensions should be loaded.

The standalone default is intended for smoke tests. The trajectory sampler
enables Pi sessions and assigns a per-run session directory because Pi events
and session entries are required parts of the framework trajectory. See
[`trajectory-definition.md`](../trajectory-definition.md) for the accepted storage
boundary.

## Library use

```python
from scripts.prompt_approval import load_approved_prompt, render_skill_prompt
from scripts.pi_rpc import PiRpcClient

template = load_approved_prompt(
    "prompts/execution/document-html-visualizer-v1.md"
)
prompt = render_skill_prompt(
    template,
    "skills/document-html-visualizer-skill",
)
with PiRpcClient(cwd=".") as pi:
    state = pi.request({"type": "get_state"})
    accepted = pi.request({"type": "prompt", "message": prompt.text})
    if accepted["success"]:
        for event in pi.events_until(
            lambda item: item.get("type") == "agent_settled",
            timeout=300,
        ):
            persist_trajectory_event(event)
```

Library callers may pass `env`. For backward compatibility the generic client
merges it over the current process environment by default. Security-sensitive
research uses `replace_environment=True`, which makes the supplied mapping the
complete child environment. `pass_fds=(...)` exposes only named, non-negative
file descriptors to that child through `subprocess.Popen(pass_fds=...)`; the
client does not make them globally inheritable. The research Runtime combines
these options with an isolated Pi directory and a one-provider, read-only
credential descriptor. General callers should not enable environment
replacement unless they also provide every variable their executable needs.

Responses carrying a request ID are routed to their waiting request. All other
records—including agent events and extension UI requests—are available from
`next_event()`. Pi stderr is never mixed into JSON records and is available as
the bounded `stderr_tail`.

For trajectory capture, pass `rpc_record_observer` and `stderr_observer`. The
RPC observer receives the direction, exact JSONL record without its LF
delimiter, the parsed object when valid, and a parse error when invalid. The
action-level trajectory writer ignores RPC request/response, message streaming,
and tool progress. It persists complete messages at `message_end`, combines
tool start/end into complete tool actions, and only retains raw input for
protocol parse failures. The observer sees correlated responses as well as
asynchronous events. Observer failures never break the Pi transport and are
exposed through `observer_errors`, so a caller can mark a trajectory incomplete.

The client transports extension UI requests but deliberately does not choose
answers for them. Production analysis and comparison runtimes therefore
disable built-in tools and load only trusted, non-interactive extensions; an
interactive extension needs a separate explicit interaction or denial policy.

## Structured analysis output

Pi 0.81.1 的 RPC `prompt` 命令没有 `response_format` 或 `json_schema` 字段。
单 trajectory 语义分析因此额外加载 `extensions/trajectory-error-output.ts`，使用 Pi 官方的
terminating structured-output tool 模式。模型通过
`submit_trajectory_error_analysis` 提交报告，Pi 在工具执行前验证完整参数结构；Python
runtime 按 tool-call ID 配对 `tool_execution_start` 和 `tool_execution_end`，只接受恰好
一次成功提交，然后继续执行 trajectory 语义和 EvidenceRef 检查。模型的普通文本不参与
正式结果解析。
