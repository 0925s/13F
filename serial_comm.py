import sys
import struct
import threading
import time
import serial
import tkinter as tk
from tkinter import ttk, messagebox


def calc_checksum(data):
    return sum(data) & 0xFF


def build_frame(cmd, data=b''):
    frame = bytes([0x55, cmd]) + data
    return frame + bytes([calc_checksum(frame)])


def parse_response(frame):
    if not frame or frame[0] != 0x55 or len(frame) < 3:
        return None
    if calc_checksum(frame[:-1]) != frame[-1]:
        return None
    return {'cmd': frame[1], 'data': frame[2:-1]}


class SerialThread(threading.Thread):
    def __init__(self, port, baudrate, on_data, on_error, on_parsed):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.on_data = on_data
        self.on_error = on_error
        self.on_parsed = on_parsed
        self.ser = None
        self.running = False
        self.lock = threading.Lock()

    def run(self):
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=0.1
            )
            self.running = True
            while self.running:
                if self.ser.in_waiting:
                    raw = self.ser.read(self.ser.in_waiting)
                    if raw:
                        self.on_data(raw)
                        parsed = parse_response(raw)
                        if parsed:
                            self.on_parsed(parsed)
                time.sleep(0.01)
        except Exception as e:
            self.on_error(str(e))
        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()

    def stop(self):
        self.running = False
        self.join(timeout=1)

    def send_frame(self, frame):
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(frame)
                except Exception as e:
                    self.on_error(f"发送失败: {e}")


class MotorControlApp:
    def __init__(self, root):
        self.root = root
        self.root.geometry("850x700")
        self.root.minsize(800, 600)
        self.serial_thread = None
        self.auto_timer_id = None
        self.query_timer_id = None
        self.query_index = 0
        self.current_internal = 0
        self.init_ui()

    def init_ui(self):
        serial_frame = ttk.LabelFrame(self.root, text="串口设置")
        serial_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(serial_frame, text="端口:").pack(side=tk.LEFT, padx=5)
        self.port_entry = tk.Entry(serial_frame, width=10)
        self.port_entry.insert(0, "COM3")
        self.port_entry.pack(side=tk.LEFT, padx=5)
        tk.Label(serial_frame, text="波特率:").pack(side=tk.LEFT, padx=5)
        self.baud_entry = tk.Entry(serial_frame, width=10)
        self.baud_entry.insert(0, "38400")
        self.baud_entry.pack(side=tk.LEFT, padx=5)
        self.connect_btn = tk.Button(serial_frame, text="打开串口", command=self.toggle_serial)
        self.connect_btn.pack(side=tk.LEFT, padx=10)

        control_frame = ttk.LabelFrame(self.root, text="控制参数")
        control_frame.pack(fill=tk.X, padx=10, pady=5)

        mode_row = tk.Frame(control_frame)
        mode_row.pack(fill=tk.X, pady=2)
        tk.Label(mode_row, text="控制模式:").pack(side=tk.LEFT, padx=5)
        self.cmd_combo = ttk.Combobox(mode_row, values=["转速 (0x31)", "功率 (0x32)"], state="readonly", width=15)
        self.cmd_combo.current(1)
        self.cmd_combo.bind("<<ComboboxSelected>>", self.on_mode_changed)
        self.cmd_combo.pack(side=tk.LEFT, padx=5)
        self.range_label = tk.Label(mode_row, text="最大功率: 300 W")
        self.range_label.pack(side=tk.LEFT, padx=20)

        slider_row = tk.Frame(control_frame)
        slider_row.pack(fill=tk.X, pady=2)
        tk.Label(slider_row, text="调节:").pack(side=tk.LEFT, padx=5)
        self.speed_slider = tk.Scale(slider_row, from_=10, to=300, orient=tk.HORIZONTAL, length=400, command=self.on_slider_changed)
        self.speed_slider.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.target_label = tk.Label(slider_row, text="目标值: 10")
        self.target_label.pack(side=tk.LEFT, padx=10)

        spin_row = tk.Frame(control_frame)
        spin_row.pack(fill=tk.X, pady=2)
        tk.Label(spin_row, text="精确值:").pack(side=tk.LEFT, padx=5)
        self.speed_spin = tk.Spinbox(spin_row, from_=10, to=300, width=8, command=self.on_spin_changed)
        self.speed_spin.delete(0, tk.END)
        self.speed_spin.insert(0, "10")
        self.speed_spin.bind("<Return>", lambda e: self.on_spin_changed())
        self.speed_spin.bind("<FocusOut>", lambda e: self.on_spin_changed())
        self.speed_spin.pack(side=tk.LEFT, padx=5)
        self.start_btn = tk.Button(spin_row, text="发送", command=self.on_start_clicked)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = tk.Button(spin_row, text="停止", command=self.stop_motor)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.reset_btn = tk.Button(spin_row, text="复位", command=self.send_reset)
        self.reset_btn.pack(side=tk.LEFT, padx=5)
        self.auto_check = tk.BooleanVar()
        self.auto_cb = tk.Checkbutton(spin_row, text="周期性发送 (1000ms)", variable=self.auto_check, command=self.toggle_auto_send)
        self.auto_cb.pack(side=tk.LEFT, padx=10)

        query_row = tk.Frame(control_frame)
        query_row.pack(fill=tk.X, pady=2)
        self.query_check = tk.BooleanVar()
        self.query_cb = tk.Checkbutton(query_row, text="循环查询 (22/23/24, 500ms)", variable=self.query_check, command=self.toggle_query)
        self.query_cb.pack(side=tk.LEFT, padx=5)

        data_frame = ttk.LabelFrame(self.root, text="实时数据")
        data_frame.pack(fill=tk.X, padx=10, pady=5)
        self.fault_label = tk.Label(data_frame, text="故障码: --")
        self.fault_label.pack(side=tk.LEFT, padx=15)
        self.current_label = tk.Label(data_frame, text="电流: -- mA")
        self.current_label.pack(side=tk.LEFT, padx=15)
        self.speed_label_ui = tk.Label(data_frame, text="速度: -- RPM")
        self.speed_label_ui.pack(side=tk.LEFT, padx=15)
        self.power_label = tk.Label(data_frame, text="功率: -- W")
        self.power_label.pack(side=tk.LEFT, padx=15)
        self.temp_label = tk.Label(data_frame, text="温度: -- ℃")
        self.temp_label.pack(side=tk.LEFT, padx=15)

        status_frame = ttk.LabelFrame(self.root, text="响应解析")
        status_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.status_text = tk.Text(status_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.status_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        status_scroll = tk.Scrollbar(self.status_text, command=self.status_text.yview)
        self.status_text.configure(yscrollcommand=status_scroll.set)
        status_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        log_frame = ttk.LabelFrame(self.root, text="通信日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        log_scroll = tk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.on_mode_changed(None)

    def on_mode_changed(self, event):
        try:
            old_value = int(self.speed_spin.get())
        except ValueError:
            old_value = 10
        index = self.cmd_combo.current()
        if index == 0:
            self.speed_slider.config(from_=20000, to=75000, tickinterval=2000, resolution=1000)
            self.range_label.config(text="最大转速: 75000 RPM")
            new_value = old_value if 20000 <= old_value <= 75000 else 20000
            self.speed_spin.config(from_=20000, to=75000, increment=1000)
        else:
            self.speed_slider.config(from_=10, to=300, tickinterval=50, resolution=1)
            self.range_label.config(text="最大功率: 300 W")
            new_value = old_value if 10 <= old_value <= 300 else 10
            self.speed_spin.config(from_=10, to=300, increment=1)
        self.speed_slider.set(new_value)
        self.speed_spin.delete(0, tk.END)
        self.speed_spin.insert(0, str(new_value))
        self.update_target_label(new_value)

    def update_target_label(self, value):
        mode = self.cmd_combo.current()
        unit = "RPM" if mode == 0 else "W"
        self.target_label.config(text=f"目标值: {value} {unit}")

    def on_slider_changed(self, value):
        v = int(float(value))
        self.speed_spin.delete(0, tk.END)
        self.speed_spin.insert(0, str(v))
        self.update_target_label(v)

    def on_spin_changed(self, event=None):
        try:
            v = int(self.speed_spin.get())
        except ValueError:
            return
        mode = self.cmd_combo.current()
        lo, hi = (20000, 75000) if mode == 0 else (10, 300)
        if v < lo:
            v = lo
        elif v > hi:
            v = hi
        self.speed_slider.set(v)
        self.update_target_label(v)

    def toggle_serial(self):
        if self.serial_thread and self.serial_thread.is_alive():
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self):
        port = self.port_entry.get().strip()
        try:
            baud = int(self.baud_entry.get().strip())
        except ValueError:
            messagebox.showwarning("错误", "波特率必须是整数")
            return
        self.serial_thread = SerialThread(port, baud, self.on_raw_data, self.on_serial_error, self.on_parsed_response)
        self.serial_thread.start()
        self.connect_btn.config(text="关闭串口")
        self.log(f"串口 {port} 已打开，波特率 {baud}")

    def disconnect_serial(self):
        if self.serial_thread:
            self.serial_thread.stop()
            self.serial_thread = None
        self.connect_btn.config(text="打开串口")
        self.log("串口已关闭")
        self.cancel_auto_timer()
        self.cancel_query_timer()
        self.auto_check.set(False)
        self.query_check.set(False)

    def send_command(self, cmd, data=b''):
        if not self.serial_thread or not self.serial_thread.is_alive():
            messagebox.showwarning("警告", "串口未打开")
            return False
        if cmd == 0x33:
            data = b'\x00\x00'
        frame = build_frame(cmd, data)
        self.log(f"发送: {frame.hex().upper()}")
        self.serial_thread.send_frame(frame)
        return True

    def value_to_internal(self, user_value):
        mode = self.cmd_combo.current()
        if mode == 0:
            internal = int(round(user_value / 1000.0))
            if internal < 20:
                internal = 20
            elif internal > 75:
                internal = 75
            return internal
        else:
            if user_value < 10:
                user_value = 10
            elif user_value > 300:
                user_value = 300
            return user_value

    def send_speed(self):
        if not (self.serial_thread and self.serial_thread.is_alive()):
            if self.auto_check.get():
                self.auto_check.set(False)
                self.log("串口未打开，停止自动发送")
            return
        try:
            user_value = int(self.speed_spin.get())
        except ValueError:
            return
        internal = self.value_to_internal(user_value)
        mode = self.cmd_combo.current()
        cmd = 0x31 if mode == 0 else 0x32
        data = struct.pack('<h', internal)
        self.send_command(cmd, data)
        self.current_internal = internal

    def stop_motor(self):
        if self.auto_check.get():
            self.auto_check.set(False)
            self.cancel_auto_timer()
        self.send_reset()

    def send_reset(self):
        self.send_command(0x33)
        self.log("发送复位")
        self.current_internal = 0

    def toggle_query(self):
        checked = self.query_check.get()
        if checked:
            if not (self.serial_thread and self.serial_thread.is_alive()):
                messagebox.showwarning("警告", "串口未打开，无法启动循环查询")
                self.query_check.set(False)
                return
            self.query_index = 0
            self.schedule_query()
            self.log("循环查询已启动")
        else:
            self.cancel_query_timer()
            self.log("循环查询已停止")

    def schedule_query(self):
        if not self.query_check.get():
            return
        self.send_next_query()
        self.query_timer_id = self.root.after(500, self.schedule_query)

    def cancel_query_timer(self):
        if self.query_timer_id:
            self.root.after_cancel(self.query_timer_id)
            self.query_timer_id = None

    def send_next_query(self):
        cmds = [0x22, 0x23, 0x24]
        cmd = cmds[self.query_index]
        self.query_index = (self.query_index + 1) % 3
        self.send_command(cmd, b'\x00\x00')

    def on_parsed_response(self, parsed):
        cmd = parsed['cmd']
        data = parsed['data']
        if cmd == 0x11:
            if len(data) >= 1:
                err_code = data[0]
                if err_code == 0x0A:
                    self.append_status("[错误] 参数越界")
                elif err_code == 0x0B:
                    self.append_status("[错误] 校验和错误")
                elif err_code == 0xFF:
                    self.append_status("[错误] 帧头错误")
                elif err_code == 0x08:
                    self.append_status("[错误] 未知命令码/格式不正确")
                else:
                    self.append_status(f"[错误] 错误码 0x{err_code:02X}")
            else:
                self.append_status(f"[错误帧] {data.hex().upper()}")
            return
        if cmd == 0x22 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            current = struct.unpack('<h', data[2:4])[0]
            self.fault_label.config(text=f"故障码: 0x{fault:02X}")
            self.current_label.config(text=f"电流: {current} mA")
            self.temp_label.config(text=f"温度: {temp} ℃")
            return
        if cmd == 0x23 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            speed_internal = struct.unpack('<h', data[2:4])[0]
            rpm = speed_internal * 1000
            self.fault_label.config(text=f"故障码: 0x{fault:02X}")
            self.speed_label_ui.config(text=f"速度: {rpm} RPM")
            self.temp_label.config(text=f"温度: {temp} ℃")
            return
        if cmd == 0x32 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            power = struct.unpack('<h', data[2:4])[0]
            self.fault_label.config(text=f"故障码: 0x{fault:02X}")
            self.power_label.config(text=f"功率: {power} W")
            self.temp_label.config(text=f"温度: {temp} ℃")
            return
        if cmd == 0x31:
            self.append_status("[0x31 速度设定响应]")
        elif cmd == 0x32:
            self.append_status("[0x32 功率设定响应]")
        elif cmd == 0x33:
            self.append_status("[0x33 复位响应]")
        else:
            self.append_status(f"收到命令 {hex(cmd)} 数据: {data.hex().upper()}")

    def on_raw_data(self, raw):
        self.log(f"接收: {raw.hex().upper()}")

    def on_serial_error(self, err):
        self.log(f"错误: {err}")
        self.root.after(0, lambda: messagebox.showerror("串口错误", err))

    def log(self, msg):
        self.append_text(self.log_text, f"[{msg}]")

    def append_status(self, msg):
        self.append_text(self.status_text, msg)

    def append_text(self, widget, msg):
        widget.config(state=tk.NORMAL)
        widget.insert(tk.END, msg + "\n")
        widget.see(tk.END)
        widget.config(state=tk.DISABLED)

    def toggle_auto_send(self):
        checked = self.auto_check.get()
        if checked:
            if not (self.serial_thread and self.serial_thread.is_alive()):
                messagebox.showwarning("警告", "串口未打开")
                self.auto_check.set(False)
                return
            self.schedule_auto()
        else:
            self.cancel_auto_timer()

    def schedule_auto(self):
        if not self.auto_check.get():
            return
        self.send_speed()
        self.auto_timer_id = self.root.after(1000, self.schedule_auto)

    def cancel_auto_timer(self):
        if self.auto_timer_id:
            self.root.after_cancel(self.auto_timer_id)
            self.auto_timer_id = None

    def on_start_clicked(self):
        if not self.auto_check.get():
            self.auto_check.set(True)
            self.toggle_auto_send()
        else:
            self.send_speed()


if __name__ == '__main__':
    root = tk.Tk()
    app = MotorControlApp(root)
    root.mainloop()