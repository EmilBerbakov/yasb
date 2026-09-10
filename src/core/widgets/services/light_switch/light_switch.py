import asyncio
import ctypes
import gc
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
    OpenKey,
    QueryValueEx,
    SetValueEx,
)

from PyQt6.QtCore import (
    QDateTime,
    QObject,
    Qt,
    QTime,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import QPixmapCache
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from win32con import (
    HWND_BROADCAST,
    SMTO_ABORTIFHUNG,
    WM_SETTINGCHANGE,
)
from winrt.windows.devices.geolocation import Geolocator

from core.config import HOME_CONFIGURATION_DIR
from core.utils.win32.bindings import SendMessageTimeoutW
from core.validation.widgets.yasb.light_switch import LightSwitchOptions

logger = logging.getLogger("light_switch")

HEADER = (b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0")
CACHE_CONTROL = (b"Cache-Control", b"no-cache")


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
        self._timer: QTimer | None = None
        self._geo_loop = None
        self._has_startup_ran = False
        self._is_switch_running = False
        self._app = False
        self._system = False
        self._time_switch = None
        self._start_QDateTime: QDateTime | None = None
        self._end_QDateTime: QDateTime | None = None
        self._is_auto_light_mode = 0
        self._latitude = None
        self._longitude = None
        self._sunrise_and_sunset_file: str = ""
        self._sunrise_and_sunset_data: dict[str, Any] = dict()
        self._cache_theme_handles = None
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._execute_toggle)

    def configure(self, service_options: LightSwitchOptions, run_after: list[str]):
        """Configure the service."""
        self._run_after = run_after
        self._app, self._system = service_options.app, service_options.system
        self._time_switch = service_options.time_switch
        self._cache_theme_handles = service_options.cache_theme_handles

        if not self._time_switch or self._has_startup_ran:
            return

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.schedule_next_toggle)
        self._has_startup_ran = True

        if self._time_switch == "auto":
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
            st, et = service_options.custom_time.start_time, service_options.custom_time.end_time
            if st is None or et is None:
                logger.error("Light Switch: custom timer mode requires both a start and end time")
                self._time_switch = None
                return
            today = QDateTime.currentDateTime().date()
            self._start_QDateTime = QDateTime(today, QTime.fromString(st, "HH:mm"))
            self._end_QDateTime = QDateTime(today, QTime.fromString(et, "HH:mm"))
            self.schedule_next_toggle(None)

    def schedule_next_toggle(self, task: asyncio.Task | None = None, auto=False):
        now = QDateTime.currentDateTime()
        next_Qdt = None
        if task is not None or auto == True:
            self._get_sunrise_and_sunset()
            return
        assert self._start_QDateTime is not None
        assert self._end_QDateTime is not None
        self._is_auto_light_mode = 1 if self._start_QDateTime <= now <= self._end_QDateTime else 0
        if self._is_auto_light_mode == 1:
            next_Qdt = self._end_QDateTime
        else:
            self._start_QDateTime = self._start_QDateTime.addDays(1)
            self._end_QDateTime = self._end_QDateTime.addDays(1)
            next_Qdt = self._start_QDateTime
        assert self._timer is not None
        self._timer.start(now.msecsTo(next_Qdt))
        logger.info("Timer set to go off at %s", str(next_Qdt))
        self.specific_light_switch(self._is_auto_light_mode)

    async def get_coords(self):
        try:
            logger.info("Getting Geolocation")
            pos = await Geolocator().get_geoposition_async()
            self._latitude, self._longitude = [pos.coordinate.latitude, pos.coordinate.longitude]
        except asyncio.CancelledError:
            pass
        except PermissionError:
            logger.error("Location Access disabled. Trying to get location using ip-api.com...")
            success = await self._query_ip_api()
            if not success:
                logger.error("Failed to get coordinates via IP API")
                self._time_switch = None

    def _get_sunrise_and_sunset(self):
        daily_data = self._sunrise_and_sunset_data.get("daily", {})
        try:
            now = QDateTime.currentDateTime()
            index = daily_data.get("time", []).index(now.date().toString("yyyy-MM-dd"))
            self._get_daily_data(index, daily_data)
            now = QDateTime.currentDateTime()
            assert self._start_QDateTime is not None
            assert self._end_QDateTime is not None
            self._is_auto_light_mode = 1 if self._start_QDateTime <= now <= self._end_QDateTime else 0
            next_Qdt = None
            if self._is_auto_light_mode == 1:
                next_Qdt = self._end_QDateTime
            else:
                next_date = index + 1
                if len(daily_data.get("time", [])) - 1 >= next_date:
                    self._get_daily_data(next_date, daily_data)
                    next_Qdt = self._start_QDateTime
                else:
                    raise ValueError
            assert self._timer is not None
            self._timer.start(now.msecsTo(next_Qdt))
            logger.info("Timer set to go off at %s", str(next_Qdt))
            self.specific_light_switch(self._is_auto_light_mode)
        except ValueError:
            self._query_open_meteo()
        except Exception as e:
            logger.error("Light Switch, unable to parse sunrise and sunset data: %s", e)
            self._time_switch = None

    def _get_daily_data(self, index: int, daily_data: Any):
        sunrise_str, sunset_str = daily_data.get("sunrise", [])[index], daily_data.get("sunset", [])[index]
        logger.info("Sunrise at %s. Sunset at %s", sunrise_str, sunset_str)
        self._start_QDateTime = QDateTime.fromString(sunrise_str, Qt.DateFormat.ISODate)
        self._end_QDateTime = QDateTime.fromString(sunset_str, Qt.DateFormat.ISODate)

    def _query_open_meteo(self):
        logger.info("meteo long: %s", self._longitude)
        logger.info("meteo lat: %s", self._latitude)
        url = f"https://api.open-meteo.com/v1/forecast?latitude={str(self._latitude)}&longitude={self._longitude}&daily=sunrise,sunset&timezone=auto&past_days=1"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(*HEADER)
        request.setRawHeader(*CACHE_CONTROL)
        manager = QNetworkAccessManager(self)
        manager.get(request)
        manager.finished.connect(self._handle_response)

    async def _query_ip_api(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        url = "http://ip-api.com/json/"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(*HEADER)
        request.setRawHeader(*CACHE_CONTROL)
        manager = QNetworkAccessManager(self)
        reply = manager.get(request)
        assert reply is not None

        def _handle_ip_api_response():
            try:
                error = reply.error()
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if error == QNetworkReply.NetworkError.NoError:
                    data = json.loads(reply.readAll().data().decode())
                    logger.info(data)
                    self._latitude = data["lat"]
                    self._longitude = data["lon"]
                    if not future.done():
                        future.set_result(True)
                elif error == QNetworkReply.NetworkError.HostNotFoundError:
                    logger.error("No internet connection or host not found. Unable to fetch sunrise and sunset.")
                    if not future.done():
                        future.set_result(False)
                elif status in {400, 401, 403}:
                    data = json.loads(reply.readAll().data().decode())
                    logger.error("IP API error %s: %s", status, data.get("reason", "Unknown"))
                    if not future.done():
                        future.set_result(False)
                else:
                    logger.error("IP response error %s: %s %s", status, error.name, error.value)
                    if not future.done():
                        future.set_result(False)
            except json.JSONDecodeError as e:
                logger.error("IP API invalid JSON response: %s", e)
                if not future.done():
                    future.set_result(False)
            except Exception as e:
                logger.error("IP API fetch error: %s\n%s", e, traceback.format_exc())
                if not future.done():
                    future.set_result(False)
            finally:
                reply.deleteLater()
                manager.deleteLater()

        reply.finished.connect(_handle_ip_api_response)
        return await future

    def _handle_response(self, reply: QNetworkReply):
        manager = reply.manager()
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
            if manager:
                manager.deleteLater()

    def specific_light_switch(self, isLightMode: int):
        system_switch = None
        app_switch = None
        try:
            self._is_switch_running = True

            with OpenKey(
                HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0,
                KEY_READ | KEY_WRITE,
            ) as key:
                app_switch, _ = isLightMode, None if self._app else QueryValueEx(key, "AppsUseLightTheme")
                system_switch, _ = isLightMode, None if self._system else QueryValueEx(key, "SystemUsesLightTheme")
                if self._app:
                    SetValueEx(key, "AppsUseLightTheme", 0, REG_DWORD, isLightMode)
                if self._system:
                    SetValueEx(key, "SystemUsesLightTheme", 0, REG_DWORD, isLightMode)
                QPixmapCache.clear()
                gc.collect()

        except Exception as e:
            logger.error("Failed to set Light/Dark mode: %s", e)
            raise
        self._run_after_thread(system_switch, app_switch)

    def toggle_light_switch(self):
        self._debounce_timer.start(300)

    def _execute_toggle(self):
        new_app = None
        new_system = None
        try:
            self._has_startup_ran = True
            with OpenKey(
                HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0,
                KEY_READ | KEY_WRITE,
            ) as key:
                app_switch, _ = QueryValueEx(key, "AppsUseLightTheme")
                system_switch, _ = QueryValueEx(key, "SystemUsesLightTheme")
                new_app = app_switch
                new_system = system_switch
                if self._app:
                    new_app = 0 if app_switch else 1
                    SetValueEx(key, "AppsUseLightTheme", 0, REG_DWORD, new_app)
                if self._system:
                    new_system = 0 if system_switch else 1
                    SetValueEx(key, "SystemUsesLightTheme", 0, REG_DWORD, new_system)
                QPixmapCache.clear()
                gc.collect()
        except Exception as e:
            logger.error("Failed to switch between light and dark mode: %s", e)
            raise
        self._run_after_thread(new_system, new_app)

    def _broadcast_color_change(self):
        """Announces to everything that is using Automatic themeing that we have changed color modes"""
        immersive_buf = ctypes.c_wchar_p("ImmersiveColorSet")
        policy_buf = ctypes.c_wchar_p("Policy")
        lparam_immersive = int(ctypes.cast(immersive_buf, ctypes.c_void_p).value or 0)
        lparam_policy = int(ctypes.cast(policy_buf, ctypes.c_void_p).value or 0)
        logger.info("immersive: %s, policy: %s", lparam_immersive, lparam_policy)
        SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, lparam_immersive, SMTO_ABORTIFHUNG, 5000, None)
        SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, lparam_policy, SMTO_ABORTIFHUNG, 5000, None)

    def _run_after_thread(self, system_switch: int, app_switch: int):
        self._broadcast_color_change()
        if self._run_after:
            threading.Thread(
                target=self._run_after_command,
                args=(
                    system_switch,
                    app_switch,
                ),
            ).start()
        else:
            self._is_switch_running = False

    def _run_after_command(self, system_switch, app_switch):
        if self._run_after:
            sys = str(system_switch)
            app = str(app_switch)

            for command in self._run_after:
                formatted_command = command.replace("{isSystemLight}", f"{sys}").replace("{areAppsLight}", f"{app}")
                logger.debug(formatted_command)
                result = subprocess.run(
                    formatted_command, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                if result.stderr:
                    logger.error("error: %s", result.stderr)
                del result
        self._is_switch_running = False
