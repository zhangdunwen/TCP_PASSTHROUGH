# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 语言偏好
请始终使用简体中文回答所有问题。
代码注释和文档也默认使用中文。

## Overview

Windows TCP 透传（端口转发）工具，Python 3 + tkinter 实现，零外部依赖。监听指定 IP/端口，将连接数据双向转发到目标地址，GUI 实时展示日志和十六进制数据。

## Commands

```bash
python tcp_forward.py    # 启动 GUI 程序
```

无需安装任何第三方包，仅需 Python 3（自带 tkinter）。

## Architecture

单文件 `tcp_forward.py`，包含三个核心类和 GUI 布局：

- **ConfigManager** — JSON 配置文件 (`config.json`) 读写，首次运行自动使用默认值
- **TcpForwarder** — TCP 转发引擎，`queue.Queue` 向 GUI 推送消息
- **App (tkinter.Tk)** — 主窗口，`after(100ms)` 轮询队列更新 UI

### 线程模型

```
Main Thread (GUI)  →  after() poll queue  →  update widgets
Server Thread      →  accept() loop       →  spawn 2 forward threads on connect
  ├─ C→T Thread    →  recv(client) → sendall(target)  →  queue data + hex dump
  └─ T→C Thread    →  recv(target) → sendall(client)  →  queue data + hex dump
```

- 仅支持单客户端连接（新连接在已有连接时被拒绝）
- 所有 socket 设置 1s timeout，保证 stop() 能及时响应
- `_stop_event` + socket close 双重保障线程退出

### 内部队列消息格式

```python
("log", str)              # 普通日志
("error", str)            # 错误信息（会重置按钮状态）
("status", str)           # listening | connected | stopped | error
("data", tag, peer, raw)  # tag: "C→T" 或 "T→C", raw: bytes
```

### 数据展示

采用 hexdump -C 格式：`OFFSET  HEX(16列)  ASCII`，上下行分别显示在两个 ScrolledText 面板中。

## Configuration

`config.json` 自动生成，四个字段：

- `listen_ip` / `listen_port` — 监听地址和端口
- `target_host` / `target_port` — 转发目标（支持域名）
