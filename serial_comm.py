import sys
import struct
from typing import Optional

import serial
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QSpinBox, QGroupBox,
    QMessageBox, QComboBox, QCheckBox, QSlider
)
from PySide6.QtCore import QThread, Signal, QTimer, Qt


def calc_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(cmd: int, data: bytes = b'') -> bytes:
    frame = bytes([0x55, cmd]) + data
    return frame + bytes([calc_checksum(frame)])


def parse_response(frame: bytes) -> Optional[dict]:
    if not frame or frame[0] != 0x55 or len(frame) < 3:
        return None
    if calc_checksum(frame[:-1]) != frame[-1]:
        return None
    return {'cmd': frame[1], 'data': frame[2:-1]}


class SerialThread(QThread):
    data_received = Signal(bytes)
    error_occurred = Signal(str)
    response_parsed = Signal(dict)

    def __init__(self, port: str, baudrate: int, parent=None):
        super().__init__(parent)
        self.port = port
        self.baudrate = baudrate
        self.serial = None
        self.running = False

    def run(self):
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=0.1
            )
            self.running = True
            while self.running:
                if self.serial.in_waiting:
                    raw = self.serial.read(self.serial.in_waiting)
                    if raw:
                        self.data_received.emit(raw)
                        parsed = parse_response(raw)
                        if parsed:
                            self.response_parsed.emit(parsed)
                self.msleep(10)
        except Exception as e:
            self.error_occurred.emit(str(e))
        finally:
            if self.serial and self.serial.is_open:
                self.serial.close()

    def stop(self):
        self.running = False
        self.wait()

    def send_frame(self, frame: bytes):
        if self.serial and self.serial.is_open:
            try:
                self.serial.write(frame)
            except Exception as e:
                self.error_occurred.emit(f"发送失败: {e}")


class MotorControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.serial_thread: Optional[SerialThread] = None
        self.auto_timer = QTimer()
        self.auto_timer.timeout.connect(self.send_speed)
        self.query_timer = QTimer()
        self.query_timer.timeout.connect(self.send_next_query)
        self.query_index = 0
        self.current_internal = 0
        self.init_ui()

    def init_ui(self):
        self.setMinimumSize(800, 600)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        serial_group = QGroupBox("串口设置")
        serial_layout = QHBoxLayout()
        self.port_edit = QLineEdit("COM3")
        self.baud_edit = QLineEdit("38400")
        self.connect_btn = QPushButton("打开串口")
        self.connect_btn.clicked.connect(self.toggle_serial)
        serial_layout.addWidget(QLabel("端口:"))
        serial_layout.addWidget(self.port_edit)
        serial_layout.addWidget(QLabel("波特率:"))
        serial_layout.addWidget(self.baud_edit)
        serial_layout.addWidget(self.connect_btn)
        serial_group.setLayout(serial_layout)
        layout.addWidget(serial_group)

        control_group = QGroupBox("控制参数")
        control_layout = QVBoxLayout()

        param_row = QHBoxLayout()
        self.cmd_combo = QComboBox()
        self.cmd_combo.addItems(["转速 (0x31)", "功率 (0x32)"])
        self.cmd_combo.setCurrentIndex(1)
        self.cmd_combo.currentIndexChanged.connect(self.on_mode_changed)
        param_row.addWidget(QLabel("控制模式:"))
        param_row.addWidget(self.cmd_combo)
        self.range_label = QLabel("最大功率: 300 W")
        param_row.addWidget(self.range_label)
        control_layout.addLayout(param_row)

        slider_layout = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.speed_slider.valueChanged.connect(self.on_slider_changed)
        self.target_label = QLabel("目标值: 10")
        slider_layout.addWidget(QLabel("调节:"))
        slider_layout.addWidget(self.speed_slider)
        slider_layout.addWidget(self.target_label)
        control_layout.addLayout(slider_layout)

        spin_row = QHBoxLayout()
        self.speed_spin = QSpinBox()
        self.speed_spin.valueChanged.connect(self.on_spin_changed)
        self.start_btn = QPushButton("发送")
        self.start_btn.clicked.connect(self.on_start_clicked)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_motor)
        self.reset_btn = QPushButton("复位")
        self.reset_btn.clicked.connect(self.send_reset)
        self.auto_check = QCheckBox("周期性发送 (1000ms)")
        self.auto_check.toggled.connect(self.toggle_auto_send)
        spin_row.addWidget(QLabel("精确值:"))
        spin_row.addWidget(self.speed_spin)
        spin_row.addWidget(self.start_btn)
        spin_row.addWidget(self.stop_btn)
        spin_row.addWidget(self.reset_btn)
        spin_row.addWidget(self.auto_check)
        control_layout.addLayout(spin_row)

        query_row = QHBoxLayout()
        self.query_check = QCheckBox("循环查询 (22/23/24, 500ms)")
        self.query_check.toggled.connect(self.toggle_query)
        query_row.addWidget(self.query_check)
        query_row.addStretch()
        control_layout.addLayout(query_row)

        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        self.on_mode_changed(self.cmd_combo.currentIndex())

        data_group = QGroupBox("实时数据")
        data_layout = QHBoxLayout()
        self.fault_label = QLabel("故障码: --")
        self.current_label = QLabel("电流: -- mA")
        self.speed_label_ui = QLabel("速度: -- RPM")
        self.power_label = QLabel("功率: -- W")
        self.temp_label = QLabel("温度: -- ℃")
        data_layout.addWidget(self.fault_label)
        data_layout.addWidget(self.current_label)
        data_layout.addWidget(self.speed_label_ui)
        data_layout.addWidget(self.power_label)
        data_layout.addWidget(self.temp_label)
        data_group.setLayout(data_layout)
        layout.addWidget(data_group)

        status_group = QGroupBox("响应解析")
        status_layout = QVBoxLayout()
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        status_layout.addWidget(self.status_text)
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

    def on_mode_changed(self, index: int):
        old_value = self.speed_spin.value()
        if index == 0:
            self.speed_slider.setRange(20000, 75000)
            self.speed_slider.setTickInterval(2000)
            self.speed_spin.setRange(20000, 75000)
            self.speed_spin.setSingleStep(1000)
            self.speed_spin.setSuffix(" RPM")
            self.range_label.setText("最大转速: 75000 RPM")
            new_value = old_value if 20000 <= old_value <= 75000 else 20000
        else:
            self.speed_slider.setRange(10, 300)
            self.speed_slider.setTickInterval(50)
            self.speed_spin.setRange(10, 300)
            self.speed_spin.setSingleStep(1)
            self.speed_spin.setSuffix(" W")
            self.range_label.setText("最大功率: 300 W")
            new_value = old_value if 10 <= old_value <= 300 else 10

        self.speed_slider.blockSignals(True)
        self.speed_spin.blockSignals(True)
        self.speed_slider.setValue(new_value)
        self.speed_spin.setValue(new_value)
        self.speed_slider.blockSignals(False)
        self.speed_spin.blockSignals(False)
        self.update_target_label(new_value)

    def update_target_label(self, value: int):
        mode = self.cmd_combo.currentIndex()
        unit = "RPM" if mode == 0 else "W"
        self.target_label.setText(f"目标值: {value} {unit}")

    def on_slider_changed(self, value: int):
        self.speed_spin.blockSignals(True)
        self.speed_spin.setValue(value)
        self.speed_spin.blockSignals(False)
        self.update_target_label(value)

    def on_spin_changed(self, value: int):
        self.speed_slider.blockSignals(True)
        self.speed_slider.setValue(value)
        self.speed_slider.blockSignals(False)
        self.update_target_label(value)

    def toggle_serial(self):
        if self.serial_thread and self.serial_thread.isRunning():
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self):
        port = self.port_edit.text().strip()
        try:
            baud = int(self.baud_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "错误", "波特率必须是整数")
            return
        self.serial_thread = SerialThread(port, baud)
        self.serial_thread.data_received.connect(self.on_raw_data)
        self.serial_thread.error_occurred.connect(self.on_serial_error)
        self.serial_thread.response_parsed.connect(self.on_parsed_response)
        self.serial_thread.start()
        self.connect_btn.setText("关闭串口")
        self.log(f"串口 {port} 已打开，波特率 {baud}")

    def disconnect_serial(self):
        if self.serial_thread:
            self.serial_thread.stop()
            self.serial_thread = None
        self.connect_btn.setText("打开串口")
        self.log("串口已关闭")
        self.auto_timer.stop()
        self.query_timer.stop()
        self.auto_check.setChecked(False)
        self.query_check.setChecked(False)

    def send_command(self, cmd: int, data: bytes = b'') -> bool:
        if not self.serial_thread or not self.serial_thread.isRunning():
            QMessageBox.warning(self, "警告", "串口未打开")
            return False
        if cmd == 0x33:
            data = b'\x00\x00'
        frame = build_frame(cmd, data)
        self.log(f"发送: {frame.hex().upper()}")
        self.serial_thread.send_frame(frame)
        return True

    def value_to_internal(self, user_value: int) -> int:
        mode = self.cmd_combo.currentIndex()
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
        if not (self.serial_thread and self.serial_thread.isRunning()):
            if self.auto_check.isChecked():
                self.auto_check.setChecked(False)
                self.log("串口未打开，停止自动发送")
            return

        user_value = self.speed_spin.value()
        internal = self.value_to_internal(user_value)
        mode = self.cmd_combo.currentIndex()
        cmd = 0x31 if mode == 0 else 0x32
        data = struct.pack('<h', internal)
        self.send_command(cmd, data)
        self.current_internal = internal

    def stop_motor(self):
        if self.auto_check.isChecked():
            self.auto_check.setChecked(False)
        self.send_reset()

    def send_reset(self):
        self.send_command(0x33)
        self.log("发送复位")
        self.current_internal = 0

    def toggle_query(self, checked: bool):
        if checked:
            if not (self.serial_thread and self.serial_thread.isRunning()):
                QMessageBox.warning(self, "警告", "串口未打开，无法启动循环查询")
                self.query_check.setChecked(False)
                return
            self.query_index = 0
            self.query_timer.start(500)
            self.log("循环查询已启动")
        else:
            self.query_timer.stop()
            self.log("循环查询已停止")

    def send_next_query(self):
        cmds = [0x22, 0x23, 0x24]
        cmd = cmds[self.query_index]
        self.query_index = (self.query_index + 1) % 3
        self.send_command(cmd, b'\x00\x00')

    def on_parsed_response(self, parsed: dict):
        cmd = parsed['cmd']
        data = parsed['data']

        if cmd == 0x11:
            if len(data) >= 1:
                err_code = data[0]
                if err_code == 0x0A:
                    self.status_text.append("[错误] 参数越界")
                elif err_code == 0x0B:
                    self.status_text.append("[错误] 校验和错误")
                elif err_code == 0xFF:
                    self.status_text.append("[错误] 帧头错误")
                elif err_code == 0x08:
                    self.status_text.append("[错误] 未知命令码/格式不正确")
                else:
                    self.status_text.append(f"[错误] 错误码 0x{err_code:02X}")
            else:
                self.status_text.append(f"[错误帧] {data.hex().upper()}")
            return

        if cmd == 0x22 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            current = struct.unpack('<h', data[2:4])[0]
            self.fault_label.setText(f"故障码: 0x{fault:02X}")
            self.current_label.setText(f"电流: {current} mA")
            self.temp_label.setText(f"温度: {temp} ℃")
            return

        if cmd == 0x23 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            speed_internal = struct.unpack('<h', data[2:4])[0]
            rpm = speed_internal * 1000
            self.fault_label.setText(f"故障码: 0x{fault:02X}")
            self.speed_label_ui.setText(f"速度: {rpm} RPM")
            self.temp_label.setText(f"温度: {temp} ℃")
            return

        if cmd == 0x32 and len(data) >= 4:
            fault = data[0]
            temp = data[1]
            if temp > 127:
                temp = temp - 256
            power = struct.unpack('<h', data[2:4])[0]
            self.fault_label.setText(f"故障码: 0x{fault:02X}")
            self.power_label.setText(f"功率: {power} W")
            self.temp_label.setText(f"温度: {temp} ℃")
            return

        if cmd == 0x31:
            self.status_text.append("[0x31 速度设定响应]")
        elif cmd == 0x32:
            self.status_text.append("[0x32 功率设定响应]")
        elif cmd == 0x33:
            self.status_text.append("[0x33 复位响应]")
        else:
            self.status_text.append(f"收到命令 {hex(cmd)} 数据: {data.hex().upper()}")

    def on_raw_data(self, raw: bytes):
        self.log(f"接收: {raw.hex().upper()}")

    def on_serial_error(self, err: str):
        self.log(f"错误: {err}")
        QMessageBox.critical(self, "串口错误", err)

    def log(self, msg: str):
        self.log_text.append(f"[{msg}]")

    def toggle_auto_send(self, checked: bool):
        if checked:
            if not (self.serial_thread and self.serial_thread.isRunning()):
                QMessageBox.warning(self, "警告", "串口未打开")
                self.auto_check.setChecked(False)
                return
            self.auto_timer.start(1000)
        else:
            self.auto_timer.stop()

    def on_start_clicked(self):
        if not self.auto_check.isChecked():
            self.auto_check.setChecked(True)
        self.send_speed()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MotorControlWindow()
    window.show()
    sys.exit(app.exec())