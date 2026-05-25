# TCP 透传工具 — 原型设计文档

## 1. 产品概述

TCP 透传（端口转发）工具，在本地监听指定端口，将接入的 TCP 连接数据双向转发到远程目标地址，并在 GUI 中实时展示日志和十六进制数据流。适用于内网穿透、协议调试、数据抓包分析等场景。

- **技术栈**：Python 3 + tkinter，零外部依赖（AES 加解密可选依赖 pycryptodome）
- **运行方式**：`python tcp_forward.py`
- **部署形态**：单文件 GUI 桌面程序

---

## 2. 功能模块

```
┌─────────────────────────────────────────────────────┐
│                  TCP 透传工具                        │
├───────────────┬─────────────────┬───────────────────┤
│   配置管理     │   转发引擎       │    数据展示        │
├───────────────┼─────────────────┼───────────────────┤
│ • 外网IP检测   │ • 服务端监听     │ • 实时日志         │
│ • 监听端口配置 │ • 客户端接入     │ • HEX/字符串双模式  │
│ • 目标地址配置 │ • 双向数据转发   │ • 上下行数据合并展示 │
│ • JSON 持久化  │ • 手动数据注入   │ • AES 加解密       │
│               │ • 连接状态管理   │ • Base64 编解码    │
│               │ • 流量统计       │ • 日志文件持久化    │
└───────────────┴─────────────────┴───────────────────┘
```

---

## 3. 系统架构

### 3.1 组件结构图

```
┌──────────────────────────────────────────────────────────┐
│                      App (tkinter.Tk)                     │
│  主窗口 — 配置面板 | 控制栏 | 日志面板 | 数据面板           │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ ConfigManager │  │ TcpForwarder │  │  queue.Queue  │   │
│  │  config.json  │  │  转发引擎     │◄─┤  消息队列      │   │
│  │  读写管理      │  │              │  │              │   │
│  └──────────────┘  └──────┬───────┘  └──────┬───────┘   │
│                           │                  │            │
│                    ┌──────┴──────┐          │            │
│                    │ socket 层    │          │            │
│                    │ server/client│          │            │
│                    │ /target      │          │            │
│                    └─────────────┘          │            │
│                                            │            │
│  after(100ms) poll ◄────────────────────────┘            │
└──────────────────────────────────────────────────────────┘
```

### 3.2 模块职责

| 模块 | 职责 | 文件位置 |
|------|------|----------|
| `ConfigManager` | JSON 配置读写，首次自动生成默认配置 | `tcp_forward.py:59-76` |
| `TcpForwarder` | TCP 转发核心引擎，管理 socket 生命周期和双向数据转发 | `tcp_forward.py:95-254` |
| `App` | tkinter GUI 主窗口，UI 构建、事件处理、队列轮询 | `tcp_forward.py:261-689` |
| `queue.Queue` | 线程安全消息队列，解耦转发线程与 GUI 线程 | 标准库 |

---

## 4. 线程模型

### 4.1 线程拓扑

```
Main Thread (GUI)
  │
  ├─ after(100ms) ──► poll queue ──► _handle_msg()
  │                                    ├─ 更新日志面板
  │                                    ├─ 更新数据面板
  │                                    ├─ 更新状态指示灯
  │                                    └─ 更新流量统计
  │
  ├─ Server Thread (daemon)
  │    │
  │    ├─ socket.bind() + listen()
  │    ├─ accept() loop (timeout=1s)
  │    │    │
  │    │    └─ on new client:
  │    │         ├─ 检查是否已有连接 → 拒绝/放行
  │    │         ├─ 连接目标服务器
  │    │         └─ spawn 2 forward threads:
  │    │              │
  │    │              ├─ C→T Thread (daemon)
  │    │              │    recv(client) → sendall(target)
  │    │              │    queue.put("data", "C→T", peer, raw)
  │    │              │
  │    │              └─ T→C Thread (daemon)
  │    │                   recv(target) → sendall(client)
  │    │                   queue.put("data", "T→C", peer, raw)
  │    │
  │    └─ on stop_event: 关闭所有 socket，线程退出
  │
  └─ Mainloop
```

### 4.2 线程安全

- **唯一通信通道**：`queue.Queue` — 转发线程只写，GUI 线程只读
- **退出机制**：`threading.Event` + socket `settimeout(1.0)` 双重保障
- **连接互斥**：同一时刻仅允许一个客户端连接，新连接在已有连接时被拒绝

### 4.3 生命周期

```
  启动             连接建立              连接断开            停止
  ──► listening ──► connected ──► listening ──► stopped
         │               │              │
         └─ 绑定端口      └─ 双向转发     └─ 清理socket
            accept循环       等待join       重新accept
```

---

## 5. 核心模块设计

### 5.1 ConfigManager

```
ConfigManager
├─ __init__(path)      → 初始化，加载默认配置
├─ load()              → 从 JSON 文件读取，merge 到默认值
└─ save(dict)          → 更新内存 + 写入 JSON 文件

配置文件格式 (config.json):
{
  "listen_ip": "0.0.0.0",       // 外网IP（自动检测填充）
  "listen_port": 16789,          // 监听端口
  "target_host": "192.168.1.100",// 目标地址（支持域名）
  "target_port": 80              // 目标端口
}
```

### 5.2 TcpForwarder（转发引擎）

```
TcpForwarder
├─ 属性
│   ├─ server_sock  : socket | None    // 监听 socket
│   ├─ client_sock  : socket | None    // 客户端连接 socket
│   ├─ target_sock  : socket | None    // 目标服务器 socket
│   ├─ _running     : bool             // 运行状态标记
│   ├─ _stop_event  : threading.Event  // 停止信号
│   ├─ _accept_thread : Thread | None  // 服务端 accept 线程
│   └─ _fwd_threads   : list[Thread]   // 转发线程列表
│
├─ 公有方法
│   ├─ start(l_ip, l_port, t_host, t_port) → 启动服务
│   ├─ stop()                              → 停止服务
│   └─ send(data, direction)               → 手动注入数据
│
├─ 内部方法
│   ├─ _server_loop()  → accept 循环 + 连接管理
│   ├─ _forward()      → 单向转发循环 (recv → sendall)
│   └─ _close_socket() → 安全关闭 socket
│
└─ 数据流（队列消息）
    ├─ ("log", msg)           → 普通日志
    ├─ ("error", msg)         → 错误信息（触发按钮状态重置）
    ├─ ("status", state)      → 状态变更 (listening|connected|stopped|error)
    └─ ("data", tag, peer, raw) → 转发数据 (tag: "C→T"|"T→C")
```

### 5.3 关键设计点

| 设计点 | 实现 | 原因 |
|--------|------|------|
| Socket 超时 | `settimeout(1.0)` | 避免 recv/accept 无限阻塞，保证 stop() 1 秒内响应 |
| 停止机制 | `_stop_event` + socket close 双保险 | 线程可能卡在 recv，close socket 可强制打断 |
| 单连接限制 | `if self.client_sock is not None: reject` | 简化状态管理，避免多连接竞态 |
| 数据缓冲 | `RECV_BUF = 65536` | 64KB 缓冲区，平衡内存与系统调用次数 |
| 连接超时 | target connect 5s，之后重置为 1s | 避免 DNS/网络故障时长时间卡住 |

---

## 6. UI 布局设计

```
┌─────────────────────────────────────────────────────────────┐
│  TCP 透传工具                                         _ □ X │
├─────────────────────────────────────────────────────────────┤
│ ┌─ 配置 ──────────────────────────────────────────────────┐ │
│ │ 外网IP: [203.0.113.5] [获取]  监听端口: [16789]          │ │
│ │ 目标地址: [192.168.1.100    ]  目标端口: [80   ] [保存]  │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ● 监听中    [启动] [停止]          发送: 1.2KB  接收: 856B │
│                                                             │
│ ┌─ 运行日志 ──────────────────────────────────────────────┐ │
│ │ [14:30:01.234] 服务启动，监听 0.0.0.0:16789              │ │
│ │ [14:30:15.567] 客户端连接: 10.0.0.5:54321                │ │
│ │ [14:30:15.589] 已连接到目标 192.168.1.100:80             │ │
│ │ [14:30:45.123] 连接已断开，继续监听...                     │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌─ 数据面板 ───────────────────────────────────────────────┐ │
│ │ 数据格式: ○HEX ○字符串  解密: [原文 ▼] □Base64          │ │
│ │ 密钥: [________________] IV: [________________] [HEX ▼]  │ │
│ │                                                         │ │
│ │ 上行 ○下行 [___________________________] [发送]  [清除]  │ │
│ │ ┌─────────────────────────────────────────────────────┐ │ │
│ │ │ [14:30:15.600] [上行]                                │ │ │
│ │ │   0000  47 45 54 20 2F 20 48 54 54 50 2F 31 2E 31 0D GET / HTTP/1.1.│ │ │
│ │ │   0010  0A 48 6F 73 74 3A 20 31 39 32 2E 31 36 38 2E .Host: 192.168.│ │ │
│ │ │                                                      │ │ │
│ │ │ [14:30:15.612] [下行]                                │ │ │
│ │ │   0000  48 54 54 50 2F 31 2E 31 20 32 30 30 20 4F 4B HTTP/1.1 200 OK│ │ │
│ │ └─────────────────────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 6.1 布局组件树

```
App (960×720, min 700×500)
├─ cfg_frame (LabelFrame "配置")
│   ├─ lbl_external_ip, btn_detect_ip, entry_listen_port
│   ├─ entry_target_host, entry_target_port, btn_save
│   └─ grid layout: 1行10列
│
├─ ctrl_frame (Frame)
│   ├─ status_canvas (16×16 Canvas, 圆形指示灯)
│   │   └─ _status_dot (oval, 颜色映射状态)
│   ├─ lbl_status, btn_start, btn_stop
│   └─ lbl_stats (右对齐: 发送/接收/连接时长)
│
├─ PanedWindow (VERTICAL, 可拖拽分割)
│   ├─ log_frame (LabelFrame "运行日志", weight=1)
│   │   └─ log_text (ScrolledText, Consolas 9, WORD wrap)
│   │
│   └─ data_frame (Frame, weight=3)
│       ├─ toolbar (Frame)
│       │   ├─ view_mode: HEX / 字符串 (Radiobutton)
│       │   ├─ aes_mode: Combobox (原文 / AES-128-CBC)
│       │   ├─ use_base64: Checkbutton
│       │   ├─ entry_aes_key, entry_aes_iv
│       │   ├─ key_fmt: Combobox (HEX / 字符串)
│       │   └─ btn_clear_data
│       │
│       ├─ send_frame (Frame)
│       │   ├─ send_direction: 上行 / 下行 (Radiobutton)
│       │   ├─ entry_send (Entry, Enter 键绑定)
│       │   └─ btn_send
│       │
│       └─ data_text (ScrolledText, NONE wrap, Consolas 9)
```

---

## 7. 核心交互流程

### 7.1 启动转发

```
用户点击 [启动]
  │
  ├─ 校验端口输入（非整数 → messagebox 报错）
  ├─ 读取 UI 配置 + 外网IP
  ├─ 重置统计计数器
  ├─ forwarder.start(listen_ip, listen_port, target_host, target_port)
  │    ├─ 创建 server_socket
  │    ├─ bind("0.0.0.0", listen_port)
  │    ├─ listen(1)
  │    ├─ 启动 _server_loop 线程
  │    └─ queue.put("status", "listening")
  │
  ├─ 禁用 [启动]，启用 [停止]
  └─ 状态灯 → 橙色（启动中...→ 监听中）
```

### 7.2 数据转发（连接建立后自动触发）

```
Client ──connect──► server_sock
  │
  ├─ accept() 返回 client_sock, addr
  ├─ 检查是否已有连接 → 有则 reject + close
  ├─ 创建 target_sock, connect(target_host, target_port)
  ├─ queue.put("status", "connected")
  │
  ├─ Thread-1 (C→T):
  │    loop: data = client_sock.recv(65536)
  │          if data: target_sock.sendall(data)
  │                   queue.put("data", "C→T", peer, data)
  │          if not data: break (对端关闭)
  │
  └─ Thread-2 (T→C):
       loop: data = target_sock.recv(65536)
             if data: client_sock.sendall(data)
                      queue.put("data", "T→C", peer, data)
             if not data: break (对端关闭)
  │
  ├─ join() 等待两个转发线程结束
  ├─ 清理 socket
  └─ queue.put("status", "listening") + 日志 "连接已断开，继续监听..."
```

### 7.3 停止转发

```
用户点击 [停止]
  │
  ├─ forwarder.stop()
  │    ├─ _running = False
  │    ├─ _stop_event.set()
  │    ├─ 依次 shutdown + close server_sock, client_sock, target_sock
  │    └─ queue.put("status", "stopped")
  │
  ├─ 启用 [启动]，禁用 [停止]
  └─ 状态灯 → 灰色（已停止）
```

### 7.4 手动发送数据

```
用户在输入框填写数据，点击 [发送] 或按 Enter
  │
  ├─ 解析输入（HEX 模式 → bytes.fromhex / 字符串模式 → encode）
  ├─ 字符串模式: 可选 AES 加密 → 可选 Base64 编码
  ├─ 选择方向: C→T（上行, 发往目标） / T→C（下行, 发往客户端）
  ├─ forwarder.send(data, direction)
  │    ├─ 根据 direction 选择 target_sock 或 client_sock
  │    ├─ sock.sendall(data)
  │    └─ queue.put("data", direction, "manual", data)
  │
  └─ 清除输入框，更新流量统计
```

### 7.5 数据解密展示

```
收到原始数据 bytes
  │
  ├─ 可选: Base64 解码 (use_base64=True 时)
  ├─ 可选: AES-128-CBC 解密 (aes_mode="AES-128-CBC" 时)
  │
  └─ 展示格式
       ├─ HEX 模式: _fmt_hex() → OFFSET + 16列HEX + ASCII
       └─ 字符串模式: decode("utf-8", errors="replace")
```

---

## 8. 数据结构

### 8.1 队列消息

```python
# 日志消息
("log", str)           # 例: ("log", "客户端连接: 10.0.0.5:54321")

# 错误消息
("error", str)         # 例: ("error", "绑定失败: [Errno 10048]")

# 状态变更
("status", str)        # 例: ("status", "listening")
                       # 取值: "listening" | "connected" | "stopped" | "error"

# 转发数据
("data", tag, peer, raw)
                       # tag:  "C→T" (客户端→目标, 上行)
                       #       "T→C" (目标→客户端, 下行)
                       # peer: "10.0.0.5:54321" (连接对端地址)
                       # raw:  b'GET / HTTP/1.1\r\n...' (原始字节)
```

### 8.2 Hexdump 格式

```
  OFFSET  HEX(16列，空格分隔，补齐49字符)  ASCII(不可打印→.)
  ──────  ──────────────────────────────  ──────────────────
   0000   47 45 54 20 2F 20 48 54 54 50  GET / HTTP
   0010   2F 31 2E 31 0D 0A 48 6F 73 74  /1.1..Host
```

### 8.3 状态映射

| 内部状态 | 指示灯颜色 | 显示文本 | 触发时机 |
|----------|-----------|---------|---------|
| `running` | 橙色 | 启动中... | start() 调用后 |
| `listening` | 绿色 #00aa00 | 监听中 | bind 成功后 / 连接断开后 |
| `connected` | 绿色 #00aa00 | 已连接 | 目标连接成功后 |
| `stopped` | 灰色 | 已停止 | stop() 调用后 |
| `error` | 红色 | 错误信息 | bind/connect 失败 |

---

## 9. 配置与持久化

| 存储项 | 格式 | 文件 | 说明 |
|--------|------|------|------|
| 用户配置 | JSON | `config.json` | 启动时加载，保存时写入 |
| 运行日志 | 纯文本（追加） | `build.log` | 所有日志和数据面板内容同步写入 |

### config.json

```json
{
  "listen_ip": "0.0.0.0",
  "listen_port": 16789,
  "target_host": "192.168.1.100",
  "target_port": 80
}
```

---

## 10. 关键常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `RECV_BUF` | 65536 (64KB) | socket recv 缓冲区大小 |
| `IP_SERVICES` | api.ipify.org, ifconfig.me/ip, icanhazip.com | 外网IP检测服务列表（3个容错） |
| `LOG_FILE` | build.log | 日志输出文件 |
| `CONFIG_FILE` | config.json | 配置文件路径 |
| Queue poll 间隔 | 100ms | `after(100)` 轮询间隔 |
| Server accept timeout | 1s | 保证 stop() 及时响应 |
| Forward recv timeout | 1s | 同上 |
| Target connect timeout | 5s | 连接目标超时（DNS/网络容错） |

---

## 11. 错误处理策略

| 场景 | 处理方式 |
|------|----------|
| 端口绑定失败 | `queue.put("error", ...)` → GUI 状态灯变红，[启动] 恢复可用 |
| 目标连接失败 | 日志记录错误，关闭客户端 socket，继续 accept |
| recv/send 异常 | 静默退出转发循环，触发连接清理流程 |
| JSON 配置文件损坏 | 静默使用默认配置 |
| 外网IP检测失败 | 显示 "0.0.0.0"，绑定所有接口 |
| pycryptodome 未安装 | `HAS_AES = False`，AES 功能在 UI 中可配置但无实际效果 |
| 非整数端口输入 | `messagebox.showerror("错误", "端口号必须为整数")` |
| 手动发送时未连接 | `messagebox.showwarning("提示", "未连接，无法发送")` |
| HEX 格式无效 | `messagebox.showerror("错误", "HEX 格式无效")` |
