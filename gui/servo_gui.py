"""Windows serial control panel for the existing servo firmware."""

import json
import math
import os
from pathlib import Path
import re
import sys
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

try:
    import serial
except ModuleNotFoundError as exc:
    if exc.name != "serial":
        raise
    # PlatformIO already installs pySerial. Reuse it with the desktop Python's Tk.
    core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    packages = core_dir / "penv" / "Lib" / "site-packages"
    if not (packages / "serial" / "__init__.py").is_file():
        sys.exit("找不到 pySerial。请运行 python -m pip install pyserial 后重试。")
    sys.path.append(str(packages))
    import serial

from serial.tools import list_ports


BAUD_RATE = 115200
BOARD_REPORT = re.compile(r"^Board ([12]) \((0x[\da-fA-F]{2})\): (.+)$")
LIMIT_REPORT = re.compile(r"^Default pulse range: (\d+)\.\.(\d+) us; smooth: 0\.\.(\d+) ms$")
BG = "#edf2f7"
NAVY = "#15263a"
TEXT = "#1f3247"
MUTED = "#566a80"
PANEL = "#ffffff"


def read_integer(value, label, minimum, maximum):
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{label}请输入整数。") from None
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}范围为 {minimum}–{maximum}。")
    return number


def read_angle(value):
    try:
        angle = float(value)
    except ValueError:
        raise ValueError("映射角请输入数值。") from None
    if not math.isfinite(angle) or not 0 <= angle <= 180:
        raise ValueError("映射角范围为 0–180°。")
    return format(angle, ".7g")


class ServoGUI:
    def __init__(self, root):
        self.root = root
        self.link = None
        self.library = None
        self.joints = None
        self.received = bytearray()
        self.port_options = {}
        self.actions = []
        self.board_labels = []
        self.board_updates = [None, None]

        self.port_choice = tk.StringVar(root)
        self.connection = tk.StringVar(root, "串口未连接")
        self.notice = tk.StringVar(root, "选择 XIAO 所在的 COM 口后点击连接。")
        self.channel = tk.StringVar(root, "0")
        self.pulse = tk.StringVar(root, "1500")
        self.angle = tk.StringVar(root, "90")
        self.duration = tk.StringVar(root, "1000")
        self.demo_angle = tk.StringVar(root, "135")
        self.limits = tk.StringVar(root)

        root.title("灵巧手 · 舵机控制台")
        root.geometry("880x680")
        root.minsize(780, 640)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.configure_style()
        self.build_window()
        self.refresh_ports()
        self.set_connected(False)
        root.after(50, self.poll_serial)

    def configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG)
        style.configure("Panel.TLabel", background=PANEL)
        style.configure("PanelTitle.TLabel", background=PANEL, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Online.TLabel", background=PANEL, foreground="#116747")
        style.configure("Offline.TLabel", background=PANEL, foreground="#a62f3b")
        style.configure("TButton", padding=(10, 5))
        for name, color, hover in (
            ("Primary.TButton", "#176383", "#124e69"),
            ("Stop.TButton", "#a32f40", "#812435"),
        ):
            style.configure(name, background=color, foreground="white", borderwidth=0)
            style.map(
                name,
                background=[("disabled", "#dfe5eb"), ("active", hover), ("pressed", hover)],
                foreground=[("disabled", MUTED), ("!disabled", "white")],
            )

    def build_window(self):
        header = tk.Frame(self.root, bg=NAVY, padx=22, pady=13)
        header.pack(fill="x")
        title = tk.Frame(header, bg=NAVY)
        title.pack(side="left")
        tk.Label(title, text="灵巧手 · 舵机控制台", bg=NAVY, fg="white",
                 font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(title, text="24 路舵机  /  XIAO ESP32S3", bg=NAVY, fg="#c6d7e8",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w")
        tk.Label(header, textvariable=self.connection, bg="#243b53", fg="white",
                 padx=12, pady=6, font=("Microsoft YaHei UI", 10)).pack(side="right")

        body = ttk.Frame(self.root, padding=(18, 12))
        body.pack(fill="both", expand=True)
        connection = ttk.Frame(body, style="Panel.TFrame", padding=(12, 9))
        connection.pack(fill="x")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="USB 串口", style="Panel.TLabel").grid(row=0, column=0, padx=(0, 9))
        self.port_menu = ttk.Combobox(connection, textvariable=self.port_choice, state="readonly", width=25)
        self.port_menu.grid(row=0, column=1, sticky="ew")
        self.refresh_button = ttk.Button(connection, text="刷新", command=self.refresh_ports)
        self.refresh_button.grid(row=0, column=2, padx=7)
        self.connect_button = ttk.Button(connection, text="连接", command=self.toggle_connection,
                                         style="Primary.TButton")
        self.connect_button.grid(row=0, column=3)
        ttk.Label(connection, text=f"{BAUD_RATE} baud", style="Muted.TLabel").grid(row=0, column=4, padx=(11, 0))

        boards = ttk.Frame(body)
        boards.pack(fill="x", pady=9)
        for i in range(2):
            boards.columnconfigure(i, weight=1, uniform="board")
            card = ttk.Frame(boards, style="Panel.TFrame", padding=(12, 8))
            card.grid(row=0, column=i, sticky="ew", padx=(0, 5) if i == 0 else (5, 0))
            first = i * 12
            ttk.Label(card, text=f"驱动板 {i + 1}    ·    舵机 {first}–{first + 11}",
                      style="Panel.TLabel").pack(anchor="w")
            label = ttk.Label(card, style="Muted.TLabel")
            label.pack(anchor="w", pady=(2, 0))
            self.board_labels.append(label)

        controls = ttk.Frame(body)
        controls.pack(fill="x")
        controls.columnconfigure(0, weight=3)
        controls.columnconfigure(1, weight=2)
        single = ttk.Frame(controls, style="Panel.TFrame", padding=(12, 10))
        single.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        single.columnconfigure(1, weight=1)
        ttk.Label(single, text="单路调节", style="PanelTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 5))
        for row, (label, value, verb, action) in enumerate((
            ("舵机编号", self.channel, "test", "单路测试"),
            ("脉宽 (µs)", self.pulse, "servo", "设定脉宽"),
            ("映射角 (°)", self.angle, "angle", "设定角度"),
            ("用时 (ms)", self.duration, "smooth", "平滑移动"),
        ), start=1):
            ttk.Label(single, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=2)
            if verb == "test":
                field = ttk.Spinbox(single, from_=0, to=23, textvariable=value, width=8)
            else:
                field = ttk.Entry(single, textvariable=value, width=9)
            field.grid(row=row, column=1, sticky="ew", padx=8)
            self.action(single, action, lambda command=verb: self.send_single(command)).grid(
                row=row, column=2, sticky="ew", pady=2
            )
        ttk.Label(single, textvariable=self.limits, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(5, 0)
        )

        demo = ttk.Frame(controls, style="Panel.TFrame", padding=(12, 10))
        demo.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ttk.Label(demo, text="三路演示", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(demo, text="舵机 0、1、2 同时移动，\n保持 2 秒后返回 1500 µs。",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 7))
        target = ttk.Frame(demo, style="Panel.TFrame")
        target.pack(fill="x")
        ttk.Label(target, text="映射角 (°)", style="Panel.TLabel").pack(side="left")
        ttk.Entry(target, textvariable=self.demo_angle, width=8).pack(side="left", padx=8)
        self.action(demo, "运行三路演示", self.send_demo, "Primary.TButton").pack(fill="x", pady=(7, 5))
        self.action(demo, "停止输出", lambda: self.send("stop"), "Stop.TButton").pack(fill="x")

        ttk.Label(body, textvariable=self.notice, foreground=MUTED, wraplength=730).pack(anchor="w", pady=(7, 5))
        log_panel = ttk.Frame(body, style="Panel.TFrame", padding=(10, 7))
        log_panel.pack(fill="both", expand=True)
        log_header = ttk.Frame(log_panel, style="Panel.TFrame")
        log_header.pack(fill="x", pady=(0, 4))
        ttk.Label(log_header, text="串口日志", style="PanelTitle.TLabel").pack(side="left")
        ttk.Button(log_header, text="指令库", command=self.open_library).pack(side="right")
        ttk.Button(log_header, text="关节微调", command=self.open_joints).pack(side="right", padx=(0, 8))
        self.log = scrolledtext.ScrolledText(log_panel, height=6, width=48, wrap="word",
                                             font=("Consolas", 10), bg="#f6f8fb", fg=TEXT,
                                             padx=7, pady=5, relief="flat", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("send", foreground="#176383")
        self.log.tag_configure("error", foreground="#a32f40")

    def action(self, parent, label, handler, style="TButton"):
        button = ttk.Button(parent, text=label, command=handler, style=style)
        self.actions.append(button)
        return button

    def append_log(self, line, tag=""):
        follow = self.log.yview()[1] >= 0.99
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {line}\n", tag)
        lines = int(self.log.index("end-1c").split(".", 1)[0]) - 1
        if lines > 500:
            self.log.delete("1.0", f"{lines - 499}.0")
        self.log.configure(state="disabled")
        if follow:
            self.log.see("end")

    def refresh_ports(self):
        try:
            ports = sorted(list_ports.comports())
        except OSError as exc:
            self.notice.set(f"读取串口列表失败。{exc}")
            return
        self.port_options = {f"{port.device}    {port.description}": port.device for port in ports}
        self.port_menu.configure(values=list(self.port_options))
        if self.port_choice.get() not in self.port_options:
            self.port_choice.set(next(iter(self.port_options)) if len(ports) == 1 else "")
        self.notice.set("选择 XIAO 所在的 COM 口后点击连接。" if ports else "未找到串口。插入 XIAO 后点击刷新。")

    def set_connected(self, connected):
        self.max_duration = 60000
        self.limits.set("默认 1000–2300 µs，各路限位由固件校验。")
        for button in self.actions:
            button.configure(state="normal" if connected else "disabled")
        self.port_menu.configure(state="disabled" if connected else "readonly")
        self.refresh_button.configure(state="disabled" if connected else "normal")
        self.connect_button.configure(text="断开" if connected else "连接")
        self.board_updates = [None, None]
        for label in self.board_labels:
            label.configure(text="等待 I2C 日志" if connected else "等待串口连接", style="Muted.TLabel")

    def toggle_connection(self):
        if self.link is not None:
            self.disconnect("串口已断开。")
            return
        port = self.port_options.get(self.port_choice.get())
        if not port:
            self.notice.set("请先选择 XIAO 所在的 COM 口。")
            return
        link = serial.Serial(baudrate=BAUD_RATE, timeout=0, write_timeout=0.25)
        link.port = port
        link.dtr = False
        link.rts = False
        try:
            link.open()
        except (serial.SerialException, OSError) as exc:
            self.notice.set(f"无法打开 {port}。请检查连接并关闭 PlatformIO 串口监视器。")
            self.append_log(str(exc), "error")
            return
        self.link = link
        self.received.clear()
        self.connection.set(f"已连接  {port}")
        self.set_connected(True)
        self.send("help")

    def disconnect(self, message):
        if self.joints:
            self.joints.finish(message)
        if self.library and self.library.running:
            self.library.finish(message)
        if self.link is not None:
            try:
                self.link.close()
            except OSError:
                pass
            self.link = None
        self.received.clear()
        self.connection.set("串口未连接")
        self.set_connected(False)
        self.notice.set(message)
        self.append_log(message)

    def send(self, command, from_library=False):
        if self.library and self.library.running and not from_library:
            if command != "stop":
                self.notice.set("指令库正在运行。请等待完成或点击「停止输出」。")
                return False
            self.library.finish("已取消后续指令。")
        if self.link is None:
            self.notice.set("请先连接串口。")
            return False
        payload = (command + "\n").encode("ascii")
        try:
            if self.link.write(payload) != len(payload):
                raise serial.SerialTimeoutException("指令未完整发送")
        except (serial.SerialException, OSError) as exc:
            self.disconnect(f"发送失败。{exc}")
            return False
        self.append_log(f"发送  {command}", "send")
        if command == "help":
            self.notice.set("等待驱动板状态。平滑移动和单路测试前请先设定该路脉宽。")
        else:
            self.notice.set(f"已发送 {command}，请查看设备回执。")
        return True

    def send_single(self, command):
        try:
            channel = read_integer(self.channel.get(), "舵机编号", 0, 23)
            args = [command, str(channel)]
            if command in ("servo", "smooth"):
                pulse = read_integer(self.pulse.get(), "脉宽", 1, 65535)
                args.append(str(pulse))
            if command == "angle":
                args.append(read_angle(self.angle.get()))
            if command == "smooth":
                duration = read_integer(self.duration.get(), "用时", 0, self.max_duration)
                args.append(str(duration))
        except ValueError as exc:
            self.notice.set(str(exc))
            return
        self.send(" ".join(args))

    def send_demo(self):
        try:
            angle = read_angle(self.demo_angle.get())
        except ValueError as exc:
            self.notice.set(str(exc))
            return
        self.send(f"demo {angle}")

    def receive_line(self, line):
        error = line.startswith(("ERROR", "WARNING")) or "not responding" in line or "initialization failed" in line
        self.append_log(line, "error" if error else "")
        if line.startswith("ERROR"):
            self.notice.set(line)

        match = BOARD_REPORT.fullmatch(line)
        if match:
            number, address, detail = match.groups()
            index = int(number) - 1
            self.board_updates[index] = (time.monotonic(), address)
            if detail.startswith("connected"):
                label, style = "I2C 有响应", "Online.TLabel"
            elif detail.startswith("not responding"):
                label, style = "I2C 未响应", "Offline.TLabel"
            else:
                label, style = "初始化失败", "Offline.TLabel"
            self.board_labels[index].configure(text=f"{address}    {label}", style=style)

        limits = LIMIT_REPORT.fullmatch(line)
        if limits:
            minimum, maximum, duration = map(int, limits.groups())
            self.max_duration = duration
            self.limits.set(f"默认 {minimum}–{maximum} µs，各路限位由固件校验。")
        if self.library and self.library.running:
            self.library.receive(line)
        if self.joints:
            self.joints.receive(line)

    def poll_serial(self):
        if self.link is not None:
            try:
                self.received.extend(self.link.read(min(self.link.in_waiting, 8192)))
            except (serial.SerialException, OSError) as exc:
                self.disconnect(f"串口连接中断。{exc}")
            else:
                lines = self.received.split(b"\n")
                self.received = lines.pop()
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self.receive_line(line)
                if len(self.received) > 8192:
                    self.received.clear()
                now = time.monotonic()
                for index, update in enumerate(self.board_updates):
                    if update and now - update[0] > 8:
                        self.board_labels[index].configure(text=f"{update[1]}    等待状态更新", style="Muted.TLabel")
                        self.board_updates[index] = None
        self.root.after(50, self.poll_serial)

    def close(self):
        if self.link is not None:
            self.disconnect("串口已断开。")
        self.root.destroy()

    def open_library(self):
        if self.library is None:
            self.library = CommandLibrary(self)
        self.library.window.deiconify()
        self.library.window.lift()

    def open_joints(self):
        if self.joints is None:
            self.joints = JointJog(self)
        self.joints.window.deiconify()
        self.joints.window.lift()


class CommandLibrary:
    def __init__(self, app):
        self.app = app
        self.path = Path(__file__).with_name("command_library.json")
        self.window = tk.Toplevel(app.root)
        self.window.title("舵机指令库")
        self.window.geometry("620x460")
        self.window.minsize(540, 400)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.saved = {}
        self.name = tk.StringVar(self.window)
        self.status = tk.StringVar(self.window, "每行一条指令。输入名称后点击「保存」。")
        self.steps = []
        self.index = 0
        self.timer = None
        self.replies = []
        self.delay_ms = 0

        panel = ttk.Frame(self.window, padding=14)
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="序列名称").pack(side="left")
        self.names = ttk.Combobox(toolbar, textvariable=self.name)
        self.names.pack(side="left", fill="x", expand=True, padx=8)
        self.names.bind("<<ComboboxSelected>>", self.load_selected)
        ttk.Button(toolbar, text="新建", command=self.new).pack(side="left")
        ttk.Label(panel, text="舵机指令每行一条，wait 500 表示等待 500 毫秒。",
                  foreground=MUTED).pack(anchor="w", pady=(8, 4))
        self.editor = scrolledtext.ScrolledText(panel, height=9, width=46, font=("Consolas", 11), undo=True)
        self.editor.pack(fill="both", expand=True)
        self.editor.insert("1.0", "servo 0 1500\nwait 500\nsmooth 0 1550 1000\nwait 500\nsmooth 0 1500 1000")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="保存", command=self.save).pack(side="left")
        self.run_button = ttk.Button(buttons, text="运行", style="Primary.TButton", command=self.run)
        self.run_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="停止输出", style="Stop.TButton",
                   command=lambda: self.app.send("stop")).pack(side="right")
        ttk.Label(panel, textvariable=self.status, wraplength=500).pack(anchor="w")
        try:
            self.saved = self.read_saved()
        except (OSError, ValueError) as exc:
            self.status.set(f"读取指令库失败。{exc}")
        self.names.configure(values=sorted(self.saved))

    @property
    def running(self):
        return bool(self.steps)

    def read_saved(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
            raise ValueError("指令库文件格式有误。")
        return data

    def load_selected(self, _event=None):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.saved[self.name.get()])
        self.editor.edit_reset()

    def new(self):
        self.name.set("")
        self.editor.delete("1.0", "end")
        self.editor.edit_reset()
        self.editor.focus_set()

    def parse(self, text, max_duration):
        steps = []
        arg_counts = {"servo": (2,), "angle": (2,), "smooth": (3,), "test": (1,),
                      "demo": (0, 1), "wait": (1,), "center": (0,), "minall": (0,),
                      "maxall": (0,), "testall": (0,), "stop": (0,)}
        for number, line in enumerate(text.splitlines(), 1):
            fields = line.partition("#")[0].split()
            if not fields:
                continue
            verb, *args = fields
            try:
                if len(args) not in arg_counts.get(verb, ()):
                    raise ValueError("指令或参数有误。")
                if verb in ("servo", "angle", "smooth", "test"):
                    args[0] = str(read_integer(args[0], "舵机编号", 0, 23))
                if verb in ("servo", "smooth"):
                    args[1] = str(read_integer(args[1], "脉宽", 1, 65535))
                if verb == "angle" or (verb == "demo" and args):
                    args[-1] = read_angle(args[-1])
                if verb == "smooth":
                    args[2] = str(read_integer(args[2], "用时", 0, max_duration))
                if verb == "wait":
                    args[0] = str(read_integer(args[0], "等待毫秒数", 0, 600000))
            except ValueError as exc:
                raise ValueError(f"第 {number} 行。{exc}") from exc
            steps.append(" ".join((verb, *args)))
        if not steps:
            raise ValueError("请输入至少一条指令。")
        return steps

    def save(self):
        name = self.name.get().strip()
        if not name:
            self.status.set("请输入序列名称。")
            return
        text = self.editor.get("1.0", "end-1c").strip()
        try:
            self.parse(text, 60000)
            saved = self.read_saved()
            saved[name] = text
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except (OSError, ValueError) as exc:
            self.status.set(f"保存失败。{exc}")
            return
        self.saved = saved
        self.names.configure(values=sorted(saved))
        self.name.set(name)
        self.status.set(f"已保存「{name}」。")

    def run(self):
        if self.running:
            return
        if self.app.link is None:
            self.status.set("请先在主窗口连接串口。")
            return
        try:
            self.steps = self.parse(self.editor.get("1.0", "end-1c"), self.app.max_duration)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.index = 0
        self.run_button.configure(state="disabled")
        self.advance()

    def advance(self):
        self.timer = None
        if self.index == len(self.steps):
            self.finish("序列运行完成。")
            return
        command = self.steps[self.index]
        self.index += 1
        self.status.set(f"运行 {self.index}/{len(self.steps)}  {command}")
        verb, *args = command.split()
        if verb == "wait":
            self.timer = self.window.after(int(args[0]), self.advance)
            return

        self.delay_ms = 0
        if verb == "demo":
            self.replies = ["OK demo 0/1/2:", "OK demo complete,"]
        elif verb in ("test", "testall"):
            self.replies = ["OK test started", "OK test complete"]
        elif verb in ("servo", "smooth", "angle"):
            self.replies = [f"OK servo {args[0]} target "]
            if verb == "smooth" and int(args[2]) > 0:
                self.delay_ms = int(args[2]) + 40
        elif verb == "stop":
            self.replies = ["OK PWM off"]
        else:
            self.replies = ["OK all pulses written"]
        self.timer = self.window.after(10000, lambda: self.finish(f"等待回执超时。{command}"))
        self.app.send(command, from_library=True)

    def receive(self, line):
        if line.startswith("ERROR"):
            self.finish(f"序列中断。{line}")
        elif self.replies and line.startswith(self.replies[0]):
            self.replies.pop(0)
            if not self.replies:
                self.window.after_cancel(self.timer)
                self.timer = self.window.after(self.delay_ms, self.advance)

    def finish(self, message):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.steps = []
        self.replies = []
        self.run_button.configure(state="normal")
        self.status.set(message)
        self.app.notice.set(message)


class JointJog:
    def __init__(self, app):
        self.app = app
        self.path = Path(__file__).with_name("joint_mapping.json")
        self.window = tk.Toplevel(app.root)
        self.window.title("关节微调")
        self.window.geometry("720x500")
        self.window.minsize(620, 440)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.mapping = {}
        self.name = tk.StringVar(self.window)
        self.channel = tk.StringVar(self.window, "0")
        self.direction = tk.StringVar(self.window, "正向")
        self.step = tk.StringVar(self.window, "10")
        self.initial = tk.StringVar(self.window, "1500")
        self.status = tk.StringVar(self.window, "添加关节后，选择对应行进行微调。")
        self.pending = None
        self.timer = None

        panel = ttk.Frame(self.window, padding=14)
        panel.pack(fill="both", expand=True)
        table = ttk.Frame(panel)
        table.pack(fill="both", expand=True)
        self.rows = ttk.Treeview(table, columns=("name", "channel", "direction", "step"), show="headings",
                                 selectmode="browse", height=7)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.rows.pack(side="left", fill="both", expand=True)
        for column, title, width in (("name", "关节名称", 215), ("channel", "舵机编号", 100),
                                     ("direction", "方向", 90), ("step", "步长 (µs)", 100)):
            self.rows.heading(column, text=title)
            self.rows.column(column, width=width, anchor="w" if column == "name" else "center")
        self.rows.bind("<<TreeviewSelect>>", self.select_row)

        form = ttk.Frame(panel)
        form.pack(fill="x", pady=(10, 4))
        form.columnconfigure(0, weight=1)
        for index, label in enumerate(("关节名称", "舵机编号", "方向", "步长 (µs)")):
            ttk.Label(form, text=label).grid(row=0, column=index, sticky="w", padx=(0, 8))
        ttk.Entry(form, textvariable=self.name, width=18).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Spinbox(form, from_=0, to=23, textvariable=self.channel, width=6).grid(row=1, column=1, padx=(0, 8))
        ttk.Combobox(form, textvariable=self.direction, values=("正向", "反向"), state="readonly",
                     width=7).grid(row=1, column=2, padx=(0, 8))
        ttk.Spinbox(form, from_=1, to=100, textvariable=self.step, width=7).grid(row=1, column=3, padx=(0, 8))
        ttk.Button(form, text="保存分配", command=self.save_row).grid(row=1, column=4)
        ttk.Button(form, text="新建", command=self.new_row).grid(row=1, column=5, padx=(5, 0))

        controls = ttk.Frame(panel)
        controls.pack(fill="x", pady=8)
        ttk.Button(controls, text="－", width=5, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(controls, text="＋", width=5, command=lambda: self.move(1),
                   style="Primary.TButton").pack(side="left", padx=(4, 12))
        ttk.Entry(controls, textvariable=self.initial, width=7).pack(side="left")
        ttk.Button(controls, text="设定起点 (µs)", command=self.set_initial).pack(side="left", padx=4)
        ttk.Button(controls, text="移除分配", command=self.remove_row).pack(side="right")
        ttk.Button(controls, text="停止输出", style="Stop.TButton",
                   command=lambda: app.send("stop")).pack(side="right", padx=6)
        ttk.Label(panel, textvariable=self.status, wraplength=590).pack(anchor="w")

        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("分配文件格式有误。")
            for channel, row in saved.items():
                self.mapping[str(read_integer(channel, "舵机编号", 0, 23))] = self.read_row(*row)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.mapping.clear()
            self.status.set(f"读取分配失败。{exc}")
        self.refresh_rows()

    def read_row(self, name, direction, step):
        if not isinstance(name, str) or not name.strip() or direction not in ("正向", "反向"):
            raise ValueError("请输入关节名称并选择方向。")
        return name.strip(), direction, read_integer(str(step), "步长", 1, 100)

    def refresh_rows(self, selected=None):
        self.rows.delete(*self.rows.get_children())
        for channel in sorted(self.mapping, key=int):
            name, direction, step = self.mapping[channel]
            self.rows.insert("", "end", iid=channel, values=(name, channel, direction, step))
        if selected is not None:
            self.rows.selection_set(selected)
            self.rows.see(selected)

    def select_row(self, _event=None):
        selected = self.rows.selection()
        if selected:
            channel = selected[0]
            name, direction, step = self.mapping[channel]
            self.name.set(name)
            self.channel.set(channel)
            self.direction.set(direction)
            self.step.set(str(step))

    def persist(self, mapping):
        try:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            self.status.set(f"保存分配失败。{exc}")
            return False
        self.mapping = mapping
        return True

    def new_row(self):
        self.rows.selection_remove(self.rows.selection())
        self.name.set("")
        self.direction.set("正向")
        self.step.set("10")
        self.status.set("填写新关节的分配后点击「保存分配」。")

    def save_row(self):
        try:
            channel = str(read_integer(self.channel.get(), "舵机编号", 0, 23))
            row = self.read_row(self.name.get(), self.direction.get(), self.step.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        selected = self.rows.selection()
        previous = selected[0] if selected else None
        if channel in self.mapping and channel != previous:
            self.status.set("该舵机已有分配，请选中对应行编辑。")
            return
        mapping = self.mapping.copy()
        if previous is not None:
            del mapping[previous]
        mapping[channel] = row
        if self.persist(mapping):
            self.refresh_rows(channel)
            self.status.set(f"已保存「{row[0]}」到舵机 {channel}。")

    def remove_row(self):
        selected = self.rows.selection()
        if selected:
            mapping = self.mapping.copy()
            del mapping[selected[0]]
            if self.persist(mapping):
                self.refresh_rows()
                self.status.set("已移除选中的分配。")

    def move(self, sign):
        selected = self.rows.selection()
        if not selected:
            self.status.set("请选择要微调的关节。")
            return
        channel = selected[0]
        _, direction, step = self.mapping[channel]
        delta = sign * step * (-1 if direction == "反向" else 1)
        self.request(f"jog {channel} {delta}", f"OK jog {channel} pulse ")

    def set_initial(self):
        selected = self.rows.selection()
        if not selected:
            self.status.set("请选择要设置起点的关节。")
            return
        try:
            pulse = read_integer(self.initial.get(), "起点脉宽", 1, 65535)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        channel = selected[0]
        self.request(f"servo {channel} {pulse}", f"OK servo {channel} target {pulse} us, duration 0 ms")

    def request(self, command, reply):
        if self.pending is not None:
            self.status.set("正在等待上一步回执。")
            return
        if self.app.send(command):
            self.pending = reply
            self.status.set(f"已发送 {command}，等待固件回执。")
            self.timer = self.window.after(3000, lambda: self.finish("等待回执超时。"))
        else:
            self.status.set(self.app.notice.get())

    def receive(self, line):
        if self.pending is None:
            return
        if line.startswith(self.pending):
            self.finish(line)
        elif line.startswith("ERROR"):
            if "needs an initial pulse" in line:
                self.finish("请先填写并点击「设定起点」。")
            elif self.pending.startswith("OK jog ") and "command/argument count" in line:
                self.finish("固件未识别微调指令。请上传新版固件。")
            else:
                self.finish(line)

    def finish(self, message):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.pending = None
        self.status.set(message)


if __name__ == "__main__":
    window = tk.Tk()
    ServoGUI(window)
    window.mainloop()
