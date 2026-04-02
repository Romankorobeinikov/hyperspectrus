import serial
from tkinter import Text, END
import datetime
import time


class MicroSerial:
    def __init__(self, port):
        self.port = port
        self.baudrate = 9600
        self.time_out = 0.5
        self.ser = 0
        self.text_log: Text = None
        try:
            self.ser = serial.Serial(self.port, baudrate=self.baudrate)
        except serial.SerialException as se:
            print("Serial port error:", str(se))

    def send(self, string):
        self.log(f"Send to {self.port}: {string[:-1]}")
        try:
            self.ser.write(str.encode(string, 'utf-8'))
        except serial.SerialException as se:
            print("Serial port error:", str(se))

    def recieve(self):
        data = ""
        try:
            data = self.ser.read(self.ser.in_waiting)
        except serial.SerialException as se:
            print("Serial port error:", str(se))
        finally:
            if len(data) == 0:
                data_str = ""
            else:
                data_str = str(data, encoding="utf-8")[:-2]
            self.log(f'Receive from {self.port}: {data_str}')
            return data_str

    def sendRecv(self, string):
        self.send(string)
        curr_time = time.time()
        while True:
            recvData = self.recieve()
            if recvData != "":
                break
            if time.time() - curr_time > self.time_out:
                recvData = "time_out"
                break
            time.sleep(0.05)
        return recvData

    def log(self, text):
        if self.text_log:
            date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ")
            self.text_log.insert(END, date + text + "\n")
        else:
            print(text)

    def close(self):
        if self.ser.is_open:
            self.ser.close()
            print("Serial connection closed.")

    def __del__(self):
        self.close()


class STM32(MicroSerial):

    @staticmethod
    def _parse_ok(response: str) -> int:
        """Возвращает 1 если ответ 'ok', иначе 0."""
        return 1 if response.strip().lower() == "ok" else 0

    @staticmethod
    def _parse_int(response: str) -> int:
        """Парсит числовой ответ, возвращает int или -1 при ошибке."""
        try:
            return int(response.strip())
        except ValueError:
            return -1

    def getSerial(self) -> str:
        """Серийный номер платы (возвращает str)."""
        return self.sendRecv("getSerial\n")

    def getVersion(self) -> str:
        """Версия прошивки (возвращает str)."""
        return self.sendRecv("getVersion\n")

    def resetDevice(self) -> int:
        """Сброс устройства. Возвращает 1 если ok, 0 иначе."""
        return self._parse_ok(self.sendRecv("resetDevice\n"))

    def getChargingState(self) -> int:
        """Состояние зарядки: 2 — полностью заряжена, 1 — заряжается, 0 — нет."""
        return self._parse_int(self.sendRecv("getChargingState\n"))

    def getBatteryVoltage(self) -> int:
        """Напряжение батареи в мВ (возвращает int)."""
        return self._parse_int(self.sendRecv("getBatteryVoltage\n"))

    def setLedDuty(self, led: int, duty: int) -> int:
        """Установить скважность LED (led: 1-8, duty: 0-100%).
        Возвращает 1 если ok, 0 иначе."""
        return self._parse_ok(self.sendRecv(f"setLedDuty{led}{duty}\n"))

    def getLedDuty(self, led: int) -> int:
        """Получить скважность LED (led: 1-8) в процентах (возвращает int 0-100)."""
        return self._parse_int(self.sendRecv(f"getLedDuty{led}\n"))

    def setOne(self, led: int) -> int:
        """Включить один LED (led: 1-8).
        Возвращает 1 если ok, 0 иначе."""
        return self._parse_ok(self.sendRecv(f"setOne{led}\n"))

    def setStopOne(self, led: int) -> int:
        """Выключить один LED (led: 1-8).
        Возвращает 1 если ok, 0 иначе."""
        return self._parse_ok(self.sendRecv(f"setStopOne{led}\n"))