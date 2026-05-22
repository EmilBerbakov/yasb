import asyncio
import json
import logging
import os
import subprocess
import threading
import traceback
from typing import Any
from winreg import (
    HKEY_CURRENT_USER,
    KEY_READ,
    KEY_WRITE,
    REG_DWORD,
    CloseKey,
    OpenKey,
    QueryValueEx,
    SetValueEx,
)

from PyQt6.QtCore import (  # type: ignore[reportMissingInputs]
    QDateTime,
    QObject,
    Qt,
    QTime,
    QTimer,
    QUrl,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest  # type: ignore[reportMissingInputs]
from win32con import (
    HWND_BROADCAST,
    SMTO_ABORTIFHUNG,
    WM_SETTINGCHANGE,
)
from win32gui import SendMessageTimeout
from winrt.windows.devices.geolocation import Geolocator  # type: ignore[reportMissingInputs]

from core.config import HOME_CONFIGURATION_DIR
from core.validation.widgets.yasb.light_switch import LightSwitchOptions

logger = logging.getLogger("Light_switch")


class LightSwitchService(QObject):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._initialized = True
        self._run_after = None
        self._timer: QTimer = None
        self._geo_loop = None
        self._has_startup_ran = False
        self._is_switch_running = False
        self._app = False
        self._system = False
        self._time_switch = None
        self._start_time = None
        self._start_QTime: QTime = None
        self._start_QDateTime: QDateTime = None
        self._end_time = None
        self._end_QTime: QTime = None
        self._end_QDateTime: QDateTime = None
        self._is_auto_light_mode = 0
        self._latitude = None
        self._longitude = None
        self._sunrise_and_sunset_file: str = ""
        self._sunrise_and_sunset_data: dict[str, Any] = dict()

    def configure(self, service_options: LightSwitchOptions, run_after: list[str]):
        """Configure the service."""
        self._run_after = run_after
        self._app, self._system = service_options.app, service_options.system
        self._time_switch = service_options.time_switch
        self._start_time, self._end_time = service_options.custom_time.start_time, service_options.custom_time.end_time

        if not self._time_switch or self._has_startup_ran:
            return

        self._timer = QTimer()
        self._timer.timeout.connect(self.schedule_next_toggle)

        if self._time_switch == "auto":
            self._has_startup_ran = True
            if service_options.data_path and service_options.data_path.strip():
                self._sunrise_and_sunset_file = os.path.expanduser(service_options.data_path)
            else:
                self._sunrise_and_sunset_file = os.path.join(HOME_CONFIGURATION_DIR, "sunrise_and_sunset_data.json")
            if os.path.exists(self._sunrise_and_sunset_file):
                logger.debug("Loading sunrise and sunset data from %s", self._sunrise_and_sunset_file)
                with open(self._sunrise_and_sunset_file, encoding="utf-8") as f:
                    self._sunrise_and_sunset_data = json.load(f)
                    self._latitude, self._longitude = (
                        self._sunrise_and_sunset_data.get("latitude"),
                        self._sunrise_and_sunset_data.get("longitude"),
                    )
            if self._latitude is None or self._longitude is None:
                self._geo_loop = asyncio.get_running_loop()
                task = self._geo_loop.create_task(self.get_coords())
                task.add_done_callback(self.schedule_next_toggle)
            else:
                self.schedule_next_toggle(auto=True)

        if self._time_switch == "custom":
            if self._start_time is None or self._end_time is None:
                self._has_startup_ran = True
                logger.error("Light Switch: custom timer mode requires both a start and end time")
                self._time_switch = None
                return
            self._start_QTime = QTime.fromString(self._start_time, "HH:mm")
            self._end_QTime = QTime.fromString(self._end_time, "HH:mm")
            self.schedule_next_toggle(None)

    def schedule_next_toggle(self, task: asyncio.Task | None = None, auto=False):
        now = QDateTime.currentDateTime()
        next_Qdt = None
        if task is not None or auto == True:
            self._get_sunrise_and_sunset()
            return
        self._is_auto_light_mode = 1 if self._start_QTime <= now.time() <= self._end_QTime else 0
        if self._is_auto_light_mode == 1:
            self._start_QDateTime = QDateTime(now.date(), self._start_QTime)
            self._end_QDateTime = QDateTime(now.date(), self._end_QTime)
            next_Qdt = self._end_QDateTime
        else:
            self._start_QDateTime = self._get_next_occurance(now, self._start_QTime)
            self._end_QDateTime = self._get_next_occurance(now, self._end_QTime)
            next_Qdt = self._start_QDateTime
        logger.info("Now is: %s. Start time is %s. End time is %s", str(now.time()), self._start_QTime, self._end_QTime)
        self._timer.start(now.msecsTo(next_Qdt))
        logger.info("Timer set to go off at %s", str(next_Qdt))
        self.specific_light_switch(self._is_auto_light_mode)

    def _get_next_occurance(self, now: QDateTime, target_time: QDateTime):
        target = QDateTime(now.date(), target_time)
        if target <= now:
            target = target.addDays(1)
        return target

    async def get_coords(self):
        try:
            pos = await Geolocator().get_geoposition_async()
            self._latitude, self._longitude = [pos.coordinate.latitude, pos.coordinate.longitude]
        except asyncio.CancelledError:
            pass
        except PermissionError:
            logger.error(
                "Light Switch: Location Access must be enabled in Windows Settings to use auto mode. Converting to defualt."
            )
            self._time_switch = None
            # TODO: fall back to getting info from Open Meteo if this fails, which would involve adding a location select in the menu toggle

    def _get_sunrise_and_sunset(self):
        daily = self._sunrise_and_sunset_data.get("daily", {})
        try:
            now = QDateTime.currentDateTime()
            index = daily.get("time", []).index(now.date().toString("yyyy-MM-dd"))
            self._get_daily_data(index, daily)
            now = QDateTime.currentDateTime()
            self._is_auto_light_mode = 1 if self._start_QTime <= now.time() <= self._end_QTime else 0
            next_Qdt = None
            if self._is_auto_light_mode == 1:
                next_Qdt = self._end_QDateTime
            else:
                next_date = index + 1
                if len(daily.get("time", [])) - 1 >= next_date:
                    self._get_daily_data(next_date, daily)
                    next_Qdt = self._start_QDateTime
                else:
                    raise ValueError
            logger.info(
                "Now is: %s. Start time is %s. End time is %s", str(now.time()), self._start_QTime, self._end_QTime
            )
            self._timer.start(now.msecsTo(next_Qdt))
            logger.info("Timer set to go off at %s", str(next_Qdt))
            self.specific_light_switch(self._is_auto_light_mode)
        except ValueError:
            self._query_open_meteo()
        except Exception as e:
            logger.error("Light Switch, unable to parse sunrise and sunset data: %s", e)
            self._time_switch = None

    def _get_daily_data(self, index: int, daily: Any):
        sunrise_str, sunset_str = daily.get("sunrise", [])[index] + "Z", daily.get("sunset", [])[index] + "Z"
        logger.info("Sunrise at %s. Sunset at %s", sunrise_str, sunset_str)
        self._start_QDateTime = QDateTime.fromString(sunrise_str, Qt.DateFormat.ISODate)
        self._end_QDateTime = QDateTime.fromString(sunset_str, Qt.DateFormat.ISODate)
        if self._start_QDateTime.isValid() and self._end_QDateTime.isValid():
            self._start_QTime = self._start_QDateTime.time()
            self._end_QTime = self._end_QDateTime.time()

    def _query_open_meteo(self):

        header = (b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0")
        cache_control = (b"Cache-Control", b"no-cache")
        url = f"https://api.open-meteo.com/v1/forecast?latitude={str(self._latitude)}&longitude={self._longitude}&daily=sunrise,sunset&timezone=auto&past_days=1"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(*header)
        request.setRawHeader(*cache_control)
        manager = QNetworkAccessManager(self)
        manager.get(request)
        manager.finished.connect(self._handle_response)

    def _handle_response(self, reply: QNetworkReply):
        try:
            error = reply.error()
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if error == QNetworkReply.NetworkError.NoError:
                data = json.loads(reply.readAll().data().decode())
                with open(self._sunrise_and_sunset_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                self._sunrise_and_sunset_data = data
                self._get_sunrise_and_sunset()
            elif error == QNetworkReply.NetworkError.HostNotFoundError:
                logger.error("No internet connection or host not found. Unable to fetch sunrise and sunset.")
            elif status in {400, 401, 403}:
                data = json.loads(reply.readAll().data().decode())
                logger.error("Open-Meteo API error %s: %s", status, data.get("reason", "Unknown"))
            else:
                logger.error("Open-Meteo response error %s: %s %s", status, error.name, error.value)
        except json.JSONDecodeError as e:
            logger.error("Open-Meteo invalid JSON response: %s", e)
        except Exception as e:
            logger.error("Open-Meteo fetch error: %s\n%s", e, traceback.format_exc())
        finally:
            reply.deleteLater()

    def specific_light_switch(self, isLightMode: int):
        try:
            self._is_switch_running = True
            with OpenKey(
                HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0,
                KEY_READ | KEY_WRITE,
            ) as key:
                if self._app:
                    SetValueEx(key, "AppsUseLightTheme", 0, REG_DWORD, isLightMode)
                if self._system:
                    SetValueEx(key, "SystemUsesLightTheme", 0, REG_DWORD, isLightMode)
                CloseKey(key)
            self._broadcast_color_change()
        except Exception as e:
            logger.error("Failed to set Light/Dark mode: %s", e)
            raise
        self._run_after_thread(1 - isLightMode)

    def toggle_light_switch(self):
        system_switch = 0
        try:
            self._has_startup_ran = True
            with OpenKey(
                HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0,
                KEY_READ | KEY_WRITE,
            ) as key:
                if self._app:
                    current_val, _ = QueryValueEx(key, "AppsUseLightTheme")
                    new_val = 0 if current_val else 1
                    SetValueEx(key, "AppsUseLightTheme", 0, REG_DWORD, new_val)
                if self._system:
                    system_switch, _ = QueryValueEx(key, "SystemUsesLightTheme")
                    new_val = 0 if system_switch else 1
                    SetValueEx(key, "SystemUsesLightTheme", 0, REG_DWORD, new_val)
                CloseKey(key)
            self._broadcast_color_change()
        except Exception as e:
            logger.error("Failed to switch between light and dark mode: %s", e)
            raise
        self._run_after_thread(system_switch)

    def _broadcast_color_change(self):
        """Announces to everything that is using Automatic themeing that we have changed color modes"""
        SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 5000)  # type: ignore[reportArgumentType]

    def _run_after_thread(self, system_switch: int):
        if self._run_after:
            threading.Thread(target=self._run_after_command, args=(system_switch,)).start()
        else:
            self._is_switch_running = False

    def _run_after_command(self, system_switch):
        if self._run_after:
            for command in self._run_after:
                formatted_command = command.replace("{isLightMode}", f"{str(1 - system_switch)}")
                logger.debug(formatted_command)
                result = subprocess.run(
                    formatted_command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                if result.stderr:
                    logger.error("error: %s", result.stderr)
        self._is_switch_running = False
