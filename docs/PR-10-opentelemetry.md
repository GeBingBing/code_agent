# PR-10: OpenTelemetry 集成

> 关联：SPECS.md Phase 14-1 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §9 全链路可观测性](../1.md) | [docs/参考.md OpenAI Codex / Opik/Langfuse](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 三大支柱 | Traces + Metrics + Logs | 1.md §9 明确 |
| 库选择 | `opentelemetry-api` + `opentelemetry-sdk` | 官方标准 |
| 导出器 | OTLP gRPC (`localhost:4317`) + Console fallback | 标准化 + 易调试 |
| Span 覆盖 | LLM call / tool execution / Hook execute | 与 PR-01 Hook 系统对齐 |
| Metrics | tool_call_count / avg_duration / failure_rate / token_usage | 4 个核心 |
| 与 Audit Log 区分 | OTel = 性能/追踪，Audit = 合规/取证 | 并存 |

---

## 现状 / 目标

**现状**：
- 无统一可观测性
- tool 执行时间靠 `time.time()` 散落记录
- token 消耗只在 stream 模式偶尔返回
- 无法做"哪类 task 最慢" / "哪个 tool 失败率最高"分析
- 无法接入 Jaeger / Zipkin 等分布式追踪系统

**目标**（1.md §9）：
> **三大支柱**：集成 OpenTelemetry 标准，收集 Traces、Metrics、Logs
>
> - **视觉**：浏览器自动化协议
> - **诊断**：内置日志/指标查询工具
> - **评估器 Agent**：(见 PR-09)

---

## 设计

### Tracer 抽象

```python
# agent/observability/tracing.py (新文件)

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource


_tracer: Optional[trace.Tracer] = None


def init_tracer(service_name: str = "coding-agent", otlp_endpoint: str = None):
    """Initialize global tracer. Idempotent."""
    global _tracer
    if _tracer is not None:
        return _tracer
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        # Console fallback for debugging
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    return _tracer


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = init_tracer()
    return _tracer
```

### Metrics

```python
# agent/observability/metrics.py (新文件)

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter


_meter: Optional[metrics.Meter] = None


def init_meter(otlp_endpoint: str = None):
    global _meter
    if _meter is not None:
        return _meter
    if otlp_endpoint:
        exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
        reader = PeriodicExportingMetricReader(exporter)
        provider = MeterProvider(metric_readers=[reader])
    else:
        provider = MeterProvider()
    metrics.set_meter_provider(provider)
    _meter = metrics.get_meter("coding-agent")
    return _meter


class AgentMetrics:
    """常用 metrics 集合."""

    def __init__(self):
        meter = init_meter()
        self.tool_call_counter = meter.create_counter(
            "agent_tool_call_total",
            description="Total number of tool calls"
        )
        self.tool_duration_histogram = meter.create_histogram(
            "agent_tool_duration_ms",
            unit="ms",
            description="Tool execution duration in milliseconds"
        )
        self.failure_counter = meter.create_counter(
            "agent_tool_failure_total",
            description="Total number of tool failures"
        )
        self.token_usage_counter = meter.create_counter(
            "agent_token_usage_total",
            description="Total LLM tokens consumed"
        )
        self.active_tasks_gauge = meter.create_up_down_counter(
            "agent_active_tasks",
            description="Currently active tasks"
        )

    def record_tool_call(self, tool: str, duration_ms: float, success: bool):
        self.tool_call_counter.add(1, {"tool": tool, "status": "ok" if success else "fail"})
        self.tool_duration_histogram.record(duration_ms, {"tool": tool})
        if not success:
            self.failure_counter.add(1, {"tool": tool})

    def record_tokens(self, input_tokens: int, output_tokens: int, model: str):
        self.token_usage_counter.add(input_tokens, {"type": "input", "model": model})
        self.token_usage_counter.add(output_tokens, {"type": "output", "model": model})
```

### Engine 集成

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ...):
        # Init OTel
        self.tracer = init_tracer(otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
        self.metrics = AgentMetrics()
        # Hook: before_llm_call → start span
        self.hooks.register("before_llm_call", self._otel_before_llm)
        # Hook: after_llm_call → end span
        self.hooks.register("after_llm_call", self._otel_after_llm)
        # Hook: before_tool_execution → start span + record
        self.hooks.register("before_tool_execution", self._otel_before_tool)
        # Hook: after_tool_execution → end span + record metrics
        self.hooks.register("after_tool_execution", self._otel_after_tool)

    async def _otel_before_llm(self, payload):
        span = self.tracer.start_span("llm.call", attributes={
            "llm.model": self.config.model,
            "llm.message_count": len(payload["messages"]),
        })
        payload["_span"] = span

    async def _otel_after_llm(self, payload):
        span = payload.get("_span")
        if span:
            response = payload.get("response")
            if hasattr(response, "usage"):
                span.set_attribute("llm.input_tokens", response.usage.input_tokens)
                span.set_attribute("llm.output_tokens", response.usage.output_tokens)
                self.metrics.record_tokens(
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    self.config.model,
                )
            span.end()

    async def _otel_before_tool(self, payload):
        span = self.tracer.start_span("tool.execute", attributes={
            "tool.name": payload["tool"],
        })
        payload["_span"] = span
        payload["_start_ts"] = time.time()

    async def _otel_after_tool(self, payload):
        span = payload.get("_span")
        start = payload.get("_start_ts", time.time())
        duration = (time.time() - start) * 1000
        success = payload.get("error") is None
        if span:
            span.set_attribute("tool.duration_ms", duration)
            span.set_attribute("tool.success", success)
            span.end()
        self.metrics.record_tool_call(payload["tool"], duration, success)
```

### Logs（结构化）

```python
# agent/observability/logging.py (新文件)

import logging
import json
from datetime import datetime


class JSONFormatter(logging.Formatter):
    """结构化 JSON 日志，OTel 友好."""

    def format(self, record):
        return json.dumps({
            "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        })


def setup_logging(level: str = "INFO"):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
```

### 工具：诊断查询

```python
# agent/tools/diagnostics.py (新文件)

class MetricsQueryTool(BaseTool):
    name = "metrics_query"
    description = "Query agent's own runtime metrics. Helps agent self-diagnose."

    async def execute(self, metric: str, **kwargs) -> ToolResult:
        # Read recent metrics from local exporter
        return ToolResult(output=json.dumps({
            "metric": metric,
            "note": "Connect to Jaeger/Grafana for full query"
        }))


class LogsQueryTool(BaseTool):
    name = "logs_query"
    description = "Query recent log entries."

    async def execute(self, level: str = "INFO", limit: int = 50, **kwargs) -> ToolResult:
        log_file = Path.home() / ".coding-agent" / "agent.log"
        if not log_file.exists():
            return ToolResult(output="No log file found")
        with log_file.open() as f:
            lines = f.readlines()[-limit:]
        return ToolResult(output="".join(lines))
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/observability/__init__.py` | **新建** — observability 子包 |
| `agent/observability/tracing.py` | **新建** — init_tracer + get_tracer |
| `agent/observability/metrics.py` | **新建** — AgentMetrics |
| `agent/observability/logging.py` | **新建** — JSONFormatter + setup_logging |
| `agent/core/engine.py` | 集成 tracer + metrics；4 个 hook 注入 |
| `agent/tools/diagnostics.py` | **新建** — MetricsQueryTool + LogsQueryTool |
| `agent/tools/__init__.py` | 注册 diagnostics |
| `pyproject.toml` | `opentelemetry-api` / `opentelemetry-sdk` / `opentelemetry-exporter-otlp` 依赖 |
| `tests/test_tracing.py` | **新建** — span 创建、属性设置、context propagation |
| `tests/test_metrics.py` | **新建** — counter/histogram/gauge 行为 |
| `tests/test_engine_otel.py` | **新建** — hook 自动创建 span、记录 metrics |

---

## 验收标准

- [ ] `init_tracer()` 创建全局 tracer provider
- [ ] OTLP endpoint 配置后导出到 `localhost:4317`；未配置时导出到 console
- [ ] Span 覆盖：每个 LLM call + 每个 tool execution 都有 span
- [ ] Span 属性：`llm.model` / `tool.name` / `tool.duration_ms` / `llm.input_tokens` 等
- [ ] Metrics 4 个核心：tool_call / duration / failure / token_usage
- [ ] 结构化 JSON 日志输出
- [ ] `metrics_query` / `logs_query` 工具注册成功
- [ ] 现有 398+ 测试不回归
- [ ] 手动验证：跑 1 个任务后，console 输出可见 span JSON

---

## 实施顺序

```
Step 1: agent/observability/__init__.py        (新文件，0.1h)
Step 2: agent/observability/tracing.py         (新文件，1.5h)
Step 3: agent/observability/metrics.py         (新文件，1.5h)
Step 4: agent/observability/logging.py         (新文件，0.5h)
Step 5: tests/test_tracing.py                  (新文件，0.5h)
Step 6: tests/test_metrics.py                  (新文件，0.5h)
Step 7: agent/core/engine.py 集成               (改文件，2h)
Step 8: agent/tools/diagnostics.py             (新文件，1h)
Step 9: tests/test_engine_otel.py              (新文件，1h)
Step 10: pyproject.toml 依赖                   (改文件，0.5h)
Step 11: pytest tests/ 验证                     (0.5h)
```

总工作量：~9.5h

**前置依赖**：PR-01（Hook）

---

## 与其他 PR 的关系

- 与 PR-01 Hook：OTel 通过 hook 自动埋点
- 与 PR-08 Audit：OTel 关注性能/追踪，Audit 关注合规/取证
- 与 PR-09 Evaluator：Evaluator 可用 OTel metrics 评分
- 与 PR-07 Orchestrator：每个子 agent span 关联到父 span（context propagation）

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/observability/tracing.py` | OTel Tracer 包装 — Span 覆盖每个 LLM call / tool execution / Hook execute |
| `agent/observability/metrics.py` | Metrics — tool 调用次数 / 平均耗时 / 失败率 / token 消耗 |
| `agent/observability/logging.py` | 结构化 JSON log（区别于 audit log：audit 是合规记录，otel log 是调试记录） |
| `agent/hooks/otel.py` | 通过 `before_llm_call` / `after_tool_execution` hook 埋点 |
| 导出 | 本地 OTLP endpoint（`localhost:4317`）+ 控制台 fallback |
