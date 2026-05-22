from typing import Literal

from core.validation.widgets.base_model import (
    CallbacksConfig,
    CustomBaseModel,
    KeybindingConfig,
)


class CallbacksLightSwitchConfig(CallbacksConfig):
    on_left: str = "toggle_light_switch"
    on_middle: str = "do_nothing"
    on_right: str = "toggle_menu"


class CustomTimeConfig(CustomBaseModel):
    start_time: str = "8:00"
    end_time: str = "20:00"


class LightSwitchOptions(CustomBaseModel):
    app: bool = True
    system: bool = True
    time_switch: Literal[None, "auto", "custom"] = None
    custom_time: CustomTimeConfig = CustomTimeConfig()
    data_path: str = ""


class LightSwitchConfig(CustomBaseModel):
    label: str = "{icon}"
    tooltip: bool = True
    callbacks: CallbacksLightSwitchConfig = CallbacksLightSwitchConfig()
    run_after: list[str] = []
    keybindings: list[KeybindingConfig] = []
    service_options: LightSwitchOptions = LightSwitchOptions()
