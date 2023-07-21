# import os

from qtpy import QtWidgets

from openpype.lib import Logger
from openpype.modules.delivery.tray.delivery_dialog import DeliveryDialog


class DeliveryTrayWrapper:
    def __init__(self, module):
        self.module = module
        self.log = Logger.get_logger(self.__class__.__name__)

        self.delivery_dialog = DeliveryDialog(module)

    def show_delivery_dialog(self):
        self.delivery_dialog.show()
        self.delivery_dialog.activateWindow()
        self.delivery_dialog.raise_()

    def tray_menu(self, parent_menu):
        tray_menu = QtWidgets.QMenu("Delivery", parent_menu)

        show_delivery_action = QtWidgets.QAction("Deliver SG Playlist", tray_menu)
        show_delivery_action.triggered.connect(self.show_delivery_dialog)
        tray_menu.addAction(show_delivery_action)

        parent_menu.addMenu(tray_menu)
