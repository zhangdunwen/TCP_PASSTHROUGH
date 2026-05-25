import base64
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

CONFIG_FILE = "config.json"
LOG_FILE = "build.log"
DEFAULT_CONFIG = {
    "listen_ip": "0.0.0.0",
    "listen_port": 16789,
    "target_host": "192.168.1.100",
    "target_port": 80,
}
RECV_BUF = 65536
IP_SERVICES = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]

try:
    from Crypto.Cipher import AES as _AES

    def _aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        cipher = _AES.new(key, _AES.MODE_CBC, iv)
        return _unpad(cipher.decrypt(data))

    def _aes_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        cipher = _AES.new(key, _AES.MODE_CBC, iv)
        return cipher.encrypt(_pad(data, 16))

    def _pad(s: bytes, blk: int) -> bytes:
        n = blk - len(s) % blk
        return s + bytes([n] * n)

    def _unpad(s: bytes) -> bytes:
        return s[: -s[-1]]

    HAS_AES = True
except ImportError:
    HAS_AES = False


def _get_external_ip() -> str:
    for url in IP_SERVICES:
        try:
            resp = urllib.request.urlopen(url, timeout=3)
            ip = resp.read().decode("utf-8").strip()
            if ip:
                return ip
        except Exception:
            continue
    return "0.0.0.0"


class ConfigManager:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except (json.JSONDecodeError, IOError):
                pass

    def save(self, d):
        self.data.update(d)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)


def _fmt_hex(data: bytes) -> str:
    """Format bytes as hex + ASCII dump, similar to hexdump -C."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        hex_part = hex_part.ljust(49)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04X}  {hex_part} {ascii_part}")
    return "\n".join(lines)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class TcpForwarder:
    def __init__(self, msg_queue: queue.Queue):
        self.q = msg_queue
        self.server_sock: socket.socket | None = None
        self.client_sock: socket.socket | None = None
        self.target_sock: socket.socket | None = None
        self._running = False
        self._accept_thread: threading.Thread | None = None
        self._fwd_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

    # ── public API ──────────────────────────────────────────────

    def send(self, data: bytes, direction: str) -> bool:
        """Inject data into the stream. direction: 'C→T' sends to target, 'T→C' sends to client."""
        sock = self.target_sock if direction == "C→T" else self.client_sock
        if sock is None:
            return False
        try:
            sock.sendall(data)
            self.q.put(("data", direction, "manual", data))
            return True
        except OSError:
            return False

    def start(self, listen_ip: str, listen_port: int, target_host: str, target_port: int):
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._server_loop,
            args=(listen_ip, listen_port, target_host, target_port),
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        # close sockets to unblock threads
        for s in (self.server_sock, self.client_sock, self.target_sock):
            self._close_socket(s)
        self.server_sock = self.client_sock = self.target_sock = None

    # ── internal ────────────────────────────────────────────────

    def _server_loop(self, listen_ip, listen_port, target_host, target_port):
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind(("0.0.0.0", listen_port))
            self.server_sock.listen(1)
            self.server_sock.settimeout(1.0)
        except OSError as e:
            self.q.put(("error", f"绑定失败: {e}"))
            self._running = False
            return

        self.q.put(("status", "listening"))
        self.q.put(("log", f"服务启动，监听 0.0.0.0:{listen_port}（外网IP: {listen_ip}）"))

        while not self._stop_event.is_set():
            try:
                client_sock, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            if self.client_sock is not None:
                self.q.put(("log", f"拒绝新连接 {addr[0]}:{addr[1]}（已有连接进行中）"))
                self._close_socket(client_sock)
                continue

            self.client_sock = client_sock
            self.q.put(("log", f"客户端连接: {addr[0]}:{addr[1]}"))

            # connect to target
            try:
                self.target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.target_sock.settimeout(5.0)
                self.target_sock.connect((target_host, target_port))
                self.target_sock.settimeout(1.0)
                self.client_sock.settimeout(1.0)
            except OSError as e:
                self.q.put(("error", f"连接目标失败: {e}"))
                self._close_socket(self.client_sock)
                self.client_sock = None
                continue

            self.q.put(("status", "connected"))
            self.q.put(("log", f"已连接到目标 {target_host}:{target_port}"))

            # start bidirectional forwarding
            self._stop_event.clear()
            t1 = threading.Thread(
                target=self._forward,
                args=(self.client_sock, self.target_sock, "C→T"),
                daemon=True,
            )
            t2 = threading.Thread(
                target=self._forward,
                args=(self.target_sock, self.client_sock, "T→C"),
                daemon=True,
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # cleanup after disconnect
            self._close_socket(self.client_sock)
            self._close_socket(self.target_sock)
            self.client_sock = self.target_sock = None
            self.q.put(("status", "listening"))
            self.q.put(("log", "连接已断开，继续监听..."))

        # shutting down
        self._close_socket(self.server_sock)
        self.server_sock = None
        self.q.put(("status", "stopped"))
        self.q.put(("log", "服务已停止"))

    def _forward(self, src: socket.socket, dst: socket.socket, tag: str):
        """Unidirectional forward loop: recv from src, send to dst."""
        peer = src.getpeername()
        peer_str = f"{peer[0]}:{peer[1]}"
        while not self._stop_event.is_set():
            try:
                data = src.recv(RECV_BUF)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                break  # peer closed

            try:
                dst.sendall(data)
            except OSError:
                break

            self.q.put(("data", tag, peer_str, data))

    @staticmethod
    def _close_socket(s: socket.socket | None):
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TCP 透传工具")
        self.geometry("960x720")
        self.minsize(700, 500)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cfg = ConfigManager()
        self.cfg.load()

        self.msg_queue: queue.Queue = queue.Queue()
        self.forwarder = TcpForwarder(self.msg_queue)
        self._total_sent = 0
        self._total_recv = 0
        self._conn_start: float | None = None

        self._build_ui()
        self._load_cfg_to_ui()
        self._poll_queue()

    # ── UI construction ─────────────────────────────────────────

    def _build_ui(self):
        # -- config frame --
        cfg_frame = ttk.LabelFrame(self, text="配置", padding=8)
        cfg_frame.pack(fill=tk.X, padx=8, pady=(8, 0))

        ttk.Label(cfg_frame, text="外网IP:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.lbl_external_ip = ttk.Label(cfg_frame, text="未检测", width=18, anchor="w")
        self.lbl_external_ip.grid(row=0, column=1, padx=(0, 4))
        self._external_ip = "0.0.0.0"
        ttk.Button(cfg_frame, text="获取外网IP", command=self._detect_external_ip).grid(row=0, column=2, padx=(0, 16))

        ttk.Label(cfg_frame, text="监听端口:").grid(row=0, column=3, sticky="e", padx=(0, 4))
        self.entry_listen_port = ttk.Entry(cfg_frame, width=8)
        self.entry_listen_port.grid(row=0, column=4, padx=(0, 20))

        ttk.Label(cfg_frame, text="目标地址:").grid(row=0, column=5, sticky="e", padx=(0, 4))
        self.entry_target_host = ttk.Entry(cfg_frame, width=18)
        self.entry_target_host.grid(row=0, column=6, padx=(0, 12))

        ttk.Label(cfg_frame, text="目标端口:").grid(row=0, column=7, sticky="e", padx=(0, 4))
        self.entry_target_port = ttk.Entry(cfg_frame, width=8)
        self.entry_target_port.grid(row=0, column=8, padx=(0, 10))

        ttk.Button(cfg_frame, text="保存配置", command=self._save_cfg).grid(row=0, column=9)

        # -- control frame --
        ctrl_frame = ttk.Frame(self, padding=4)
        ctrl_frame.pack(fill=tk.X, padx=8, pady=4)

        self.status_canvas = tk.Canvas(ctrl_frame, width=16, height=16, highlightthickness=0)
        self.status_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self._status_dot = self.status_canvas.create_oval(2, 2, 14, 14, fill="gray", outline="")

        self.lbl_status = ttk.Label(ctrl_frame, text="未启动")
        self.lbl_status.pack(side=tk.LEFT, padx=(0, 16))

        self.btn_start = ttk.Button(ctrl_frame, text="启动", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_stop = ttk.Button(ctrl_frame, text="停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        self.lbl_stats = ttk.Label(ctrl_frame, text="发送: 0 B  接收: 0 B  连接时长: --")
        self.lbl_stats.pack(side=tk.RIGHT)

        # -- main area (PanedWindow for resizable split) --
        pw = ttk.PanedWindow(self, orient=tk.VERTICAL)
        pw.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # log panel
        log_frame = ttk.LabelFrame(pw, text="运行日志", padding=2)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, wrap=tk.WORD, font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        pw.add(log_frame, weight=1)

        # data panel (merged, single window)
        data_frame = ttk.Frame(pw)

        # toolbar: view mode toggle + AES
        toolbar = ttk.Frame(data_frame)
        toolbar.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(toolbar, text="数据格式:").pack(side=tk.LEFT, padx=(0, 4))
        self._view_mode = tk.StringVar(value="hex")
        ttk.Radiobutton(toolbar, text="HEX", variable=self._view_mode, value="hex").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(toolbar, text="字符串", variable=self._view_mode, value="str").pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(toolbar, text="解密:").pack(side=tk.LEFT, padx=(0, 4))
        self._aes_mode = tk.StringVar(value="原文")
        self.cb_aes = ttk.Combobox(toolbar, textvariable=self._aes_mode, values=["原文", "AES-128-CBC"], state="readonly", width=12)
        self.cb_aes.pack(side=tk.LEFT, padx=(0, 4))
        self._aes_mode.trace_add("write", self._on_aes_mode_change)

        self._use_base64 = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Base64", variable=self._use_base64).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(toolbar, text="密钥:").pack(side=tk.LEFT, padx=(0, 2))
        self.entry_aes_key = ttk.Entry(toolbar, width=20, font=("Consolas", 9))
        self.entry_aes_key.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(toolbar, text="IV:").pack(side=tk.LEFT, padx=(0, 2))
        self.entry_aes_iv = ttk.Entry(toolbar, width=20, font=("Consolas", 9))
        self.entry_aes_iv.pack(side=tk.LEFT, padx=(0, 8))

        self._key_fmt = tk.StringVar(value="HEX")
        ttk.Combobox(toolbar, textvariable=self._key_fmt, values=["HEX", "字符串"], state="readonly", width=6).pack(side=tk.LEFT)

        ttk.Button(toolbar, text="清除数据", command=self._clear_data).pack(side=tk.RIGHT)

        # send bar (inside data panel, above the text)
        send_frame = ttk.Frame(data_frame)
        send_frame.pack(fill=tk.X, pady=(2, 2))

        self._send_direction = tk.StringVar(value="C→T")
        ttk.Radiobutton(send_frame, text="上行", variable=self._send_direction, value="C→T").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(send_frame, text="下行", variable=self._send_direction, value="T→C").pack(side=tk.LEFT, padx=(0, 8))

        self.entry_send = ttk.Entry(send_frame, font=("Consolas", 9))
        self.entry_send.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.entry_send.bind("<Return>", lambda e: self._send_manual())

        ttk.Button(send_frame, text="发送", command=self._send_manual).pack(side=tk.LEFT)

        self.data_text = scrolledtext.ScrolledText(
            data_frame, wrap=tk.NONE, font=("Consolas", 9)
        )
        self.data_text.pack(fill=tk.BOTH, expand=True)

        pw.add(data_frame, weight=3)

    def _detect_external_ip(self):
        self._log("正在检测外网IP...")
        self._external_ip = _get_external_ip()
        self.lbl_external_ip.config(text=self._external_ip)
        if self._external_ip != "0.0.0.0":
            self._log(f"检测到外网IP: {self._external_ip}")
        else:
            self._log("外网IP检测失败，将绑定 0.0.0.0")

    # ── config helpers ──────────────────────────────────────────

    def _load_cfg_to_ui(self):
        d = self.cfg.data
        self.entry_listen_port.delete(0, tk.END)
        self.entry_listen_port.insert(0, str(d["listen_port"]))
        self.entry_target_host.delete(0, tk.END)
        self.entry_target_host.insert(0, str(d["target_host"]))
        self.entry_target_port.delete(0, tk.END)
        self.entry_target_port.insert(0, str(d["target_port"]))

    def _save_cfg(self):
        try:
            d = {
                "listen_port": int(self.entry_listen_port.get()),
                "target_host": self.entry_target_host.get().strip(),
                "target_port": int(self.entry_target_port.get()),
            }
        except ValueError:
            messagebox.showerror("错误", "端口号必须为整数")
            return
        self.cfg.save(d)
        self._log("配置已保存")

    # ── start / stop ────────────────────────────────────────────

    def _start(self):
        d = self.cfg.data
        try:
            d["listen_port"] = int(self.entry_listen_port.get())
            d["target_port"] = int(self.entry_target_port.get())
        except ValueError:
            messagebox.showerror("错误", "端口号必须为整数")
            return
        d["listen_ip"] = getattr(self, "_external_ip", "0.0.0.0")
        d["target_host"] = self.entry_target_host.get().strip()

        self._total_sent = 0
        self._total_recv = 0
        self._conn_start = None
        self._log_once = set()

        self.forwarder.start(d["listen_ip"], d["listen_port"], d["target_host"], d["target_port"])
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self._set_status("running", "启动中...")

    def _stop(self):
        self.forwarder.stop()
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self._set_status("stopped", "已停止")
        self._conn_start = None

    # ── manual send ─────────────────────────────────────────────

    def _send_manual(self):
        text = self.entry_send.get()
        if not text:
            return
        if self._view_mode.get() == "hex":
            text = text.replace(" ", "")
            try:
                data = bytes.fromhex(text)
            except ValueError:
                messagebox.showerror("错误", "HEX 格式无效")
                return
        else:
            data = text.encode("utf-8")
            # encode chain: aes → base64
            data = self._apply_aes_encrypt(data)
            if self._use_base64.get():
                data = base64.b64encode(data)
                self._log("[Base64] 编码成功", once="b64_enc_ok")
        direction = self._send_direction.get()
        ok = self.forwarder.send(data, direction)
        if not ok:
            messagebox.showwarning("提示", "未连接，无法发送")
            return
        self.entry_send.delete(0, tk.END)
        dlen = len(data)
        if direction == "C→T":
            self._total_sent += dlen
        else:
            self._total_recv += dlen
        self._update_stats()

    # ── queue poll ──────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == "log":
            self._log(msg[1])
        elif kind == "error":
            self._log(f"[错误] {msg[1]}")
            self._set_status("error", msg[1])
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
        elif kind == "status":
            self._set_status(msg[1], None)
        elif kind == "data":
            _, tag, peer, data = msg
            self._append_data(tag, data)
            dlen = len(data)
            if tag == "C→T":
                self._total_sent += dlen
            else:
                self._total_recv += dlen
            self._update_stats()

    # ── AES helpers ──────────────────────────────────────────────

    def _on_aes_mode_change(self, *_):
        self._log_once = set()
        mode = self._aes_mode.get()
        if mode == "AES-128-CBC":
            fmt = self._key_fmt.get()
            self._log(f"[AES] 已切换为 AES-128-CBC 解密模式（密钥格式: {fmt}）")
            _, _, err = self._parse_aes_key_iv()
            if err:
                self._log(f"[AES] 警告: {err}")

    def _parse_aes_key_iv(self):
        if not HAS_AES:
            return None, None, "pycryptodome 未安装"
        raw_key = self.entry_aes_key.get().strip()
        raw_iv = self.entry_aes_iv.get().strip()
        if not raw_key or not raw_iv:
            return None, None, "密钥或IV为空"

        if self._key_fmt.get() == "HEX":
            key = raw_key.replace(" ", "")
            iv = raw_iv.replace(" ", "")
            if len(key) != 32:
                return None, None, f"密钥HEX长度={len(key)}（需32位）"
            if len(iv) != 32:
                return None, None, f"IV HEX长度={len(iv)}（需32位）"
            try:
                return bytes.fromhex(key), bytes.fromhex(iv), ""
            except ValueError:
                return None, None, "密钥/IV含非HEX字符"
        else:
            # string: truncate or pad to 16 bytes
            key_bytes = raw_key.encode("utf-8")[:16].ljust(16, b"\x00")
            iv_bytes = raw_iv.encode("utf-8")[:16].ljust(16, b"\x00")
            return key_bytes, iv_bytes, ""

    def _apply_aes_decrypt(self, data: bytes) -> bytes:
        if self._aes_mode.get() != "AES-128-CBC":
            return data
        key, iv, err = self._parse_aes_key_iv()
        if key is None:
            self._log(f"[AES] {err}", once="aes_key_err")
            return data
        try:
            result = _aes_decrypt(key, iv, data)
            self._log("[AES] 解密成功", once="aes_ok")
            return result
        except Exception as e:
            self._log(f"[AES] 解密异常: {e}", once="aes_err")
            return data

    def _apply_aes_encrypt(self, data: bytes) -> bytes:
        if self._aes_mode.get() != "AES-128-CBC":
            return data
        key, iv, err = self._parse_aes_key_iv()
        if key is None:
            self._log(f"[AES] {err}", once="aes_key_err")
            return data
        try:
            result = _aes_encrypt(key, iv, data)
            self._log("[AES] 加密成功", once="aes_ok")
            return result
        except Exception as e:
            self._log(f"[AES] 加密异常: {e}", once="aes_err")
            return data

    def _clear_data(self):
        self.data_text.delete("1.0", tk.END)

    # ── UI helpers ──────────────────────────────────────────────

    def _log(self, text: str, once: str = ""):
        if once:
            if not hasattr(self, "_log_once"):
                self._log_once = set()
            if once in self._log_once:
                return
            self._log_once.add(once)
        line = f"[{_ts()}] {text}\n"
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self._write_file_log(line)

    def _append_data(self, tag: str, data: bytes):
        tag_label = "上行" if tag == "C→T" else "下行"
        ts = _ts()
        # decode chain: base64 → aes
        display_data = data
        if self._use_base64.get():
            try:
                display_data = base64.b64decode(display_data)
                self._log("[Base64] 解码成功", once="b64_ok")
            except Exception as e:
                self._log(f"[Base64] 解码失败: {e}", once="b64_err")
        display_data = self._apply_aes_decrypt(display_data)
        if self._view_mode.get() == "hex":
            body = _fmt_hex(display_data)
        else:
            body = display_data.decode("utf-8", errors="replace")
        block = f"[{ts}] [{tag_label}]\n{body}\n\n"
        self.data_text.insert(tk.END, block)
        self.data_text.see(tk.END)
        self._write_file_log(block)

    def _set_status(self, state: str, text: str | None):
        colors = {
            "running": "orange",
            "listening": "#00aa00",
            "connected": "#00aa00",
            "stopped": "gray",
            "error": "red",
        }
        labels = {
            "running": "启动中...",
            "listening": "监听中",
            "connected": "已连接",
            "stopped": "已停止",
            "error": "错误",
        }
        self.status_canvas.itemconfig(self._status_dot, fill=colors.get(state, "gray"))
        self.lbl_status.config(text=text if text else labels.get(state, state))
        if state == "connected" and self._conn_start is None:
            self._conn_start = time.time()

    def _update_stats(self):
        duration = ""
        if self._conn_start:
            secs = int(time.time() - self._conn_start)
            m, s = divmod(secs, 60)
            duration = f"{m:02d}:{s:02d}"
        else:
            duration = "--"
        self.lbl_stats.config(
            text=f"发送: {self._fmt_bytes(self._total_sent)}  "
            f"接收: {self._fmt_bytes(self._total_recv)}  "
            f"连接时长: {duration}"
        )

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.2f} MB"

    @staticmethod
    def _write_file_log(text: str):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _on_close(self):
        self.forwarder.stop()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
