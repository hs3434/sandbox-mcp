# Sandbox 环境管理 MCP 服务器

一个提供持久化沙箱环境管理的 MCP（Model Context Protocol）服务器。
为 AI agent 管理 Docker 容器和 SSH 机器作为执行目标，支持基于 shell 的命令执行和完整的文件操作能力。

设计用来替代 Hermes Agent 内置的 terminal / file / code_execution 工具，
在内置工具基础上增加持久化的环境管理能力。

## 特性

- **简洁的 MCP 接口**：只暴露 7 个工具，通过 `sandbox_env` 渐进式发现管理能力
- **双传输**：stdio（Hermes 子进程）或 SSE/HTTP（独立服务）
- **多 backend**：Docker 容器（SDK，支持远程 daemon）+ SSH 远程机器
- **持久化机器**：Docker 容器在 MCP 重启后依然存在，可用 `docker_ps` 发现
- **Shell 执行**：双 marker 确认机制，长时间运行的命令可用 `read` 读后续输出
- **完整文件操作**：读、写（原子）、patch（模糊匹配）、搜索（ripgrep / glob）
- **进程内 linter**：Python `ast`、JSON、可选 YAML/TOML 写前校验
- **安全提示**：对敏感路径（`.ssh`、`.aws`、`.env*`）的非阻塞警告
- **审计日志**：所有工具调用的 JSON-line 流（内容做哈希）

## 快速开始

### 安装

```bash
pip install .
pip install -e ".[dev]"   # 加上测试 / lint 工具

# 跑单元测试（默认跳过集成测试）
pytest tests/ -v

# 跑集成测试（需要本机 Docker daemon 在跑）
pytest tests/ -m integration -v
```

### 运行

sandbox-mcp 有两种传输模式：

- **`sandbox-mcp-http`** —— 独立 HTTP/SSE 服务，从 shell 启动：
  ```bash
  sandbox-mcp-http
  # 然后用任意 MCP 客户端连 http://127.0.0.1:8010/sse
  ```
- **`sandbox-mcp`**（stdio）—— 由 MCP host 作为子进程拉起。
  不要在 shell 里直接跑这个命令，要在 host 里配置（见下面
  [注册到 Hermes](#注册到-hermesstdio)）。

### 命令行参数

| 参数 | 适用 | 用途 |
|---|---|---|
| `--config PATH` / `-c PATH` | 两者 | TOML 配置文件路径 |
| `--host ADDR` / `-H ADDR` | `sandbox-mcp-http` | HTTP 绑定地址 |
| `--port N` / `-p N` | `sandbox-mcp-http` | HTTP 端口 |

```bash
# 独立 HTTP/SSE 服务
sandbox-mcp-http -c /etc/sandbox-mcp/prod.toml --port 9000

# stdio（在 MCP host 的配置里传，不从 shell 跑）
#   下面"注册到 Hermes"小节有完整示例
```

优先级（从高到低）：**CLI 参数** → 环境变量 → 配置文件 → 内置默认值。

### 配置

sandbox-mcp 按以下优先级读配置（从高到低）：

1. **CLI 参数**（见上表）
2. **环境变量** —— `SANDBOX_MCP_*`（例如 `SANDBOX_MCP_SERVER_PORT`）
3. **配置文件** —— 默认 `~/.sandbox-mcp/config.toml`，可用 `--config PATH` 或 `SANDBOX_MCP_CONFIG` 覆盖
4. **内置默认值**（在 `src/sandbox_mcp/config.py` 里声明）

要自定义，把 [`config.example.toml`](config.example.toml) 拷贝到
`~/.sandbox-mcp/config.toml` 后改需要的字段。保持默认就什么都不用做。

主要配置项：

```toml
[server]                # HTTP/SSE 服务
host = "0.0.0.0"
port = 8010

[storage]               # 持久化 workspace 目录
work_home = "~/.sandbox-mcp/workspaces/"

[audit]                 # JSON-line 审计日志
log_path = ""           # "" = stderr；填文件路径则追加到文件

[docker]                # 容器默认设置
container_name_prefix = "sandbox-"
default_image = "debian:stable-slim"
default_workdir = "/workspace"
restart_policy_name = "on-failure"
restart_max_retry_count = 3

[ssh]
connect_timeout = 10
socket_dir_prefix = "sandbox-mcp-ssh-"
tmpfile_pattern = ".sandbox-mcp-tmp.XXXXXX"

[shell]
default_max_output = 50000
head_size = 5120
tail_size = 46080

[files]
max_file_size = 51200
default_read_limit = 500
max_read_limit = 2000
default_search_limit = 50
```

每个值都能用环境变量覆盖（大写、点 → 下划线）：

```bash
SANDBOX_MCP_SERVER_PORT=9000 sandbox-mcp-http
SANDBOX_MCP_DOCKER_CONTAINER_NAME_PREFIX="box-" sandbox-mcp
SANDBOX_MCP_AUDIT_LOG_PATH=/var/log/sandbox-mcp/audit.log sandbox-mcp
```

`work_home` 目录会自动创建。`docker_run` 被调用时，会在 `work_home/<机器名>/`
下创建子目录并 bind-mount 到容器内的 `/workspace` —— agent 在 `/workspace`
工作，**永远看不到宿主路径**。

### 注册到 Hermes（stdio）

加到 `~/.hermes/config.yaml`：

```yaml
mcp_servers:
  sandbox:
    command: sandbox-mcp
    # 可选：给 server 传 CLI 参数。flags 跟独立服务一样——
    # sandbox-mcp 从 --config / $SANDBOX_MCP_CONFIG / ~/.sandbox-mcp/config.toml
    # 读配置。
    args:
      - --config
      - /etc/sandbox-mcp/prod.toml

# 禁用 Hermes 内置工具（可选，避免 schema 重复）
agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

Hermes 把 `sandbox-mcp` 当成子进程拉起，通过它的 stdin/stdout 走 JSON-RPC。
server 没有 UI，只等请求。

## 工具列表

| 工具 | 用途 |
|---|---|
| `sandbox_shell_exec` | 执行 shell 命令（wait 或非阻塞） |
| `sandbox_shell_read` | 读 shell 的新输出 |
| `sandbox_file_read` | 读文本文件，带行号 |
| `sandbox_file_write` | 写文件（自动 mkdir、语法检查、原子写） |
| `sandbox_file_patch` | 模糊匹配的定向编辑 |
| `sandbox_file_search` | ripgrep 内容搜索 + glob 文件搜索 |
| `sandbox_env` | 渐进式发现：`default_set`, `shell_*`, `docker_*`, `ssh_*` |

## sandbox_env 操作

`sandbox_env` 默认只暴露 `help` 和 `status`。
调用 `action=help` 看完整列表，或 `action=docker_help` / `action=ssh_help` 看 backend 专属操作：

| 命名空间 | 操作 |
|---|---|
| Discovery | `help`, `status` |
| General | `machine_list`, `default_set` |
| Shell | `shell_new`, `shell_list`, `shell_remove` |
| Docker | `docker_run`, `docker_build`, `docker_commit`, `docker_stop`, `docker_start`, `docker_remove`, `docker_ps`, `docker_images` |
| SSH | `ssh_connect`, `ssh_disconnect`, `ssh_reconnect`, `ssh_remove` |

`docker_run` 是幂等的：如果名为 `sandbox-<name>` 的容器已经存在
（比如 MCP 重启后），会重新挂载而不是失败。

### `docker_build` 用法

agent 永远不接触宿主文件系统。`docker_build` 提供两种模式：

**文件模式**（推荐）：agent 先用 `sandbox_file_write` 把 Dockerfile 写到容器内的
`/workspace/`，然后调用：

```python
sandbox_file_write(path="/workspace/Dockerfile",
                   content="FROM debian:stable-slim\nRUN apt install -y python3\n")
sandbox_env(action="docker_build",
            machine="dev",
            image_tag="myapp:v1")
# 默认 dockerfile=/workspace/Dockerfile, context_dir=/workspace
# sandbox-mcp 自动把容器路径翻译成宿主 work_home/<machine>/ 下的路径
```

**内联模式**（适合快速构建 / 还没有容器）：

```python
sandbox_env(action="docker_build",
            image_tag="myapp:latest",
            dockerfile_content="FROM debian:stable-slim\nRUN apt install -y python3\n")
# sandbox-mcp 把内容写到 work_home/_builds/<uuid>/Dockerfile，build 完清理
```

**沙箱边界保护**：`dockerfile` 和 `context_dir` 必须在 `/workspace/` 下，
宿主路径会被拒绝 —— 防止 agent 读到 `work_home` 之外的文件。

## HTTP 鉴权

HTTP/SSE 模式（`sandbox-mcp-http`）需要 bearer token 鉴权。token 存在文件里，一行一个：

```
~/.sandbox-mcp/auth_tokens           # 默认路径
```

**文件必须 0600 权限**，否则 sandbox-mcp 拒绝启动（fail-closed）：

```bash
chmod 600 ~/.sandbox-mcp/auth_tokens
```

路径可在 `config.toml` 里改：

```toml
[server]
auth_tokens_file = "/etc/sandbox-mcp/auth_tokens"
```

或通过环境变量（优先级最高）：

```bash
SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE=/run/secrets/auth_tokens sandbox-mcp-http
```

MCP 客户端连接时传 `Authorization: Bearer <token>` header：

```bash
curl -N -H "Authorization: Bearer <你的token>" http://127.0.0.1:8010/sse
```

### 自动生成开发用 token

在 `config.toml` 里设 `auto_generate_if_empty = true`，
或导出 `SANDBOX_MCP_SERVER_AUTO_GENERATE_IF_EMPTY=true`。
如果 token 文件不存在或为空，启动时生成一个临时 token 并打印到 stderr：

```
[sandbox-mcp-http] WARNING: no tokens found at ~/.sandbox-mcp/auth_tokens.
Generated ephemeral token (capture now, will not be shown again):
  XKTUv1Gjv2...33-chars-long
Pass it as: Authorization: Bearer <token>
```

拷贝这个 token 给当前 session 用。server 重启后不会重复生成同一个（文件还在会读文件）。

## 限制

- **SSH backend 只支持 key 认证**。当前版本不支持密码认证。
- **没有 PTY / 交互式 stdin**。命令非交互运行。需要 TTY 的命令（vim、ssh 密码提示）不支持。
- **状态在内存里**。Shell session 服务端重启后丢失，重新 `shell_new`。容器能跨重启存活，重新 `docker_run` 挂载，或 `docker_ps` 查看。
- **没有 session 隔离**。多个 agent 连同一个 server 共享 machine / shell registry。这跟 Hermes 自带的 MCP 行为一致。

## 架构概览

```text
Agent (LLM)
  │
  ▼
MCP Client (Hermes Gateway | 任意 MCP host)
  │  JSON-RPC over stdio │  或  │ SSE/HTTP
  ▼                              ▼
sandbox-mcp                     sandbox-mcp-http
  │  (stdio transport)           │  (SSE transport, port 8010)
  │                              │
  └──────────┬───────────────────┘
             │
             ▼
      Application Layer
  ┌──────────────────────┐
  │ 7 个 MCP 工具        │
  │ sandbox_env 调度      │
  │ ShellSession / ShellReg│
  │ MachineRegistry       │
  │ FileOperations        │
  │ AuditLogger / Safety  │
  └──────────┬───────────┘
             │
     ┌───────┴───────┐
     ▼               ▼
  Docker SDK      SSH (subprocess)
  (put_archive,    (ControlMaster,
   exec_run,        exec_oneoff,
   exec socket)     stdin pipe)
```

## 设计

设计规格见 [docs/design-spec-v2.md](docs/design-spec-v2.md)。
TDD 实现计划见 [docs/implementation-plan.md](docs/implementation-plan.md)。

## 贡献

```bash
# 跑本地 CI（跟 GitHub Actions 一致）
./scripts/ci.sh
```
