from core.utils.tooltip import set_tooltip
from core.validation.widgets.yasb.light_switch import LightSwitchConfig
from core.widgets.base import BaseWidget
from core.widgets.services.light_switch.light_switch import LightSwitchService


class LightSwitchWidget(BaseWidget):
    validation_schema = LightSwitchConfig

    def __init__(self, config: LightSwitchConfig):
        """Initialize the LightSwitchWidget with configuration parameters"""
        super().__init__(0, class_name="light-switch-widget")
        self.config = config
        self._service = LightSwitchService()
        self._service.configure(self.config.service_options, self.config.run_after)

        self._init_container()
        self.build_widget_label(self.config.label, None)

        if self.config.tooltip:
            set_tooltip(self, "Switch Between Light and Dark Mode")

        # self._service.toggle_light_switch_signal.connect(self._on_toggle_light_switch_request)

        # Register Callbacks
        self.register_callback("toggle_light_switch", self._service.toggle_light_switch)
        self.register_callback("toggle_menu", self._toggle_menu)

        self.callback_left = self.config.callbacks.on_left
        self.callback_middle = self.config.callbacks.on_middle
        self.callback_right = self.config.callbacks.on_right

    def _toggle_menu(self):
        """Toggle Light Switch Options Menu"""
        print("hitting toggle menu")
        return
