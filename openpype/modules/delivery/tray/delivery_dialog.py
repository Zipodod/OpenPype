import os
import platform
import json
from qtpy import QtCore, QtWidgets, QtGui

from openpype import style
from openpype import resources
from openpype.lib import Logger
from openpype.client import get_projects
from openpype.pipeline import AvalonMongoDB
from openpype.tools.utils import lib as tools_lib
from openpype.modules.shotgrid.lib import delivery, credentials
from openpype.modules.delivery.scripts import media


logger = Logger.get_logger(__name__)


class DeliveryOutputsWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        # Create the layout
        self.layout = QtWidgets.QFormLayout(self)
        self.setLayout(self.layout)

        self.delivery_widgets = {}
        self.delivery_extensions = {}

    def update(self, outputs_name_ext):
        # Remove all existing rows
        for i in reversed(range(self.layout.count())):
            item = self.layout.itemAt(i)
            if item.widget() is not None:
                item.widget().deleteLater()
            self.layout.removeItem(item)

        self.delivery_outputs = {}
        if not outputs_name_ext:
            return

        # Add the new rows
        for name_ext in outputs_name_ext:
            name, ext = name_ext
            label = QtWidgets.QLabel(f"{name}")
            checkbox = QtWidgets.QCheckBox()
            checkbox.setChecked(True)
            self.delivery_widgets[name] = checkbox
            self.delivery_extensions[name] = ext
            self.layout.addRow(label, checkbox)

    def get_selected_outputs(self):
        return [
            (output_name, self.delivery_extensions[output_name])
            for output_name, checkbox in self.delivery_widgets.items()
            if checkbox.isChecked()
        ]


class KeyValueWidget(QtWidgets.QWidget):
    """Widget to define key value pairs of strings."""
    def __init__(self):
        super().__init__()

        # Create the layout
        self.layout = QtWidgets.QVBoxLayout(self)
        self.setLayout(self.layout)

        # Create the add button
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.clicked.connect(self.add_pair)
        self.layout.addWidget(self.add_button)

        # Create the scroll area
        self.scroll_area = QtWidgets.QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.layout.addWidget(self.scroll_area)

        # Create the scroll area widget
        self.scroll_widget = QtWidgets.QWidget(self.scroll_area)
        self.scroll_area.setWidget(self.scroll_widget)

        # Create the scroll area layout
        self.scroll_layout = QtWidgets.QVBoxLayout(self.scroll_widget)
        self.scroll_widget.setLayout(self.scroll_layout)

        # Create the key-value pairs list
        self.pairs = []

    def add_pair(self, key="", value=""):
        # Create the key-value pair widgets
        key_input = QtWidgets.QLineEdit(key)
        value_input = QtWidgets.QLineEdit(value)
        delete_button = QtWidgets.QPushButton("Delete")
        delete_button.clicked.connect(lambda: self.delete_pair(delete_button))

        # Add the key-value pair widgets to the layout
        pair_layout = QtWidgets.QHBoxLayout()
        pair_layout.addWidget(key_input)
        pair_layout.addWidget(value_input)
        pair_layout.addWidget(delete_button)
        self.scroll_layout.addLayout(pair_layout)

        # Add the key-value pair to the list
        self.pairs.append((key_input, value_input, delete_button))

    def delete_pair(self, delete_button):
        # Find the key-value pair that corresponds to the delete button
        for pair in self.pairs:
            if pair[2] == delete_button:
                key_input, value_input, delete_button = pair
                break

        # Remove the key-value pair from the layout and the list
        pair_widget = delete_button.parent()
        pair_widget.deleteLater()
        self.pairs.remove((key_input, value_input, delete_button))

    def get_pairs(self):
        # Return the key-value pairs as a dictionary
        return {
            key_input.text(): value_input.text()
            for key_input, value_input, _ in self.pairs
        }


class DeliveryDialog(QtWidgets.QDialog):
    """Interface to control SG deliveries"""

    tool_title = "Deliver SG Entities"
    tool_name = "sg_entity_delivery"

    SIZE_W = 1200
    SIZE_H = 800

    # File path to json file that contains defaults for the Delivery dialog inputs
    PROJ_DELIVERY_CONFIG = "/proj/{project_code}/config/delivery/defaults.json"

    TOKENS_HELP = """
        {project[name]}: Project's full name
        {project[code]}: Project's code
        {asset}: Name of asset or shot
        {task[name]}: Name of task
        {task[type]}: Type of task
        {task[short]}: Short name of task type (eg. 'Modeling' > 'mdl')
        {parent}: Name of hierarchical parent
        {version}: Version number
        {subset}: Subset name
        {family}: Main family name
        {ext}: File extension
        {representation}: Representation name
        {frame}: Frame number for sequence files.
    """

    def __init__(self, module, parent=None):
        super(DeliveryDialog, self).__init__(parent)

        self.setWindowTitle(self.tool_title)

        self._module = module

        icon = QtGui.QIcon(resources.get_openpype_icon_filepath())
        self.setWindowIcon(icon)

        self.setWindowFlags(
            QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.WindowCloseButtonHint
            | QtCore.Qt.WindowMinimizeButtonHint
        )

        self.setMinimumSize(QtCore.QSize(self.SIZE_W, self.SIZE_H))

        self.sg = credentials.get_shotgrid_session()

        self._first_show = True
        self._initial_refresh = False
        self._ignore_project_change = False

        # Short code name for currently selected project
        self._current_project_code = None

        dbcon = AvalonMongoDB()
        dbcon.install()
        dbcon.Session["AVALON_PROJECT"] = None
        self.dbcon = dbcon

        self.ui_init()

    def ui_init(self):

        main_layout = QtWidgets.QVBoxLayout(self)

        input_widget = QtWidgets.QWidget()

        # Common input widgets for delivery and republish features
        input_layout = QtWidgets.QFormLayout(input_widget)
        input_layout.setContentsMargins(5, 5, 5, 5)

        # Project combobox
        projects_combobox = QtWidgets.QComboBox()
        combobox_delegate = QtWidgets.QStyledItemDelegate(self)
        projects_combobox.setItemDelegate(combobox_delegate)
        projects_combobox.currentTextChanged.connect(self.on_project_change)
        input_layout.addRow("Project", projects_combobox)

        delivery_outputs = DeliveryOutputsWidget()
        input_layout.addRow("Outputs {output}", delivery_outputs)

        # TODO: validate whether version has already been generated or not
        # Add checkbox to choose whether we want to force the media to be
        # regenerated or not
        force_delivery_media_cb = QtWidgets.QCheckBox()
        force_delivery_media_cb.setChecked(False)
        force_delivery_media_cb.setToolTip(
            "Whether we want to force the generation of the delivery media "\
            "representations regardless if that version already exists or not " \
            "(i.e., need to create new slates)"
        )
        input_layout.addRow(
            "Force regeneration of media", force_delivery_media_cb
        )

        vendor_input = QtWidgets.QLineEdit(
            "ALKX"
        )
        vendor_input.setToolTip(
            "Template string used as a replacement of {vendor} on the path template."
        )
        input_layout.addRow("Vendor {vendor}", vendor_input)

        package_name_input = QtWidgets.QLineEdit(
            "{yyyy}{mm}{dd}_{vendor}_A"
        )
        package_name_input.setToolTip(
            "Template string used as a replacement of {package_name} on the path template."
        )
        input_layout.addRow("Package name {package_name}", package_name_input)

        version_input = QtWidgets.QLineEdit("")
        version_input.setToolTip(
            "Override the version number of the delivery media. If left empty, " \
            "the version will just be increased from the last existing version. "
        )
        # Set the validator for the QLineEdit to QIntValidator
        version_input.setValidator(QtGui.QIntValidator())
        input_layout.addRow(
            "Version override {version}", version_input
        )

        task_override_combo = QtWidgets.QComboBox()
        task_override_combo.addItems(
            [
                media.USE_SOURCE_VALUE,
                "blockvis",
                "previs",
                "techvis",
                "postvis",
                "color",
                "dev",
                "layout",
                "anim",
                "comp",
                "precomp",
                "prod",
                "howto",
            ]
        )
        task_override_combo.setEditable(True)
        input_layout.addRow("Task short {task[short]}", task_override_combo)

        comment_input = QtWidgets.QLineEdit("")
        comment_input.setToolTip(
            "Override the submission notes/comment of the delivery media. If left empty, " \
            "the comment will just be picked up from the SG version description. "
        )
        # Set the validator for the QLineEdit to QIntValidator
        comment_input.setValidator(QtGui.QIntValidator())
        input_layout.addRow(
            "Submission notes override {comment}", comment_input
        )

        custom_tokens = KeyValueWidget()
        custom_tokens.setToolTip(
            "Key value pairs of new tokens to create so they can be used on "
            "template path. If you prefix the key with an output name, that "
            " key will only exist for that output (i.e., 'prores422_final:suffix')"
        )
        input_layout.addRow("Custom tokens", custom_tokens)

        filename_input = QtWidgets.QLineEdit(media.FILENAME_TEMPLATE_DEFAULT)
        filename_input.setToolTip(
            "Template string used as a replacement of {filename} on the path template."
        )
        input_layout.addRow("File name {filename}", filename_input)

        template_input = QtWidgets.QLineEdit(media.DELIVERY_TEMPLATE_DEFAULT)
        template_input.setToolTip(
            "Template string used as a replacement for where the delivery media "
            "will be written to.\nAvailable tokens: {}\nTo make a token optional"
            "so it's ignored if it's not available on the entity you can just "
            "wrap it with '<' and '>' (i.e., <{{frame}}> will only be added in the "
            "case where {{frame}} doesn't exist on that output)".format(
                self.TOKENS_HELP
            )
        )

        input_layout.addRow("Path template", template_input)

        main_layout.addWidget(input_widget)

        # SG input widgets
        sg_input_widget = QtWidgets.QWidget()
        input_group = QtWidgets.QButtonGroup(sg_input_widget)
        input_group.setExclusive(True)

        # TODO: show only the available playlists

        sg_playlist_id_input = QtWidgets.QLineEdit()
        sg_playlist_id_input.setToolTip("Integer id of the SG Playlist (i.e., '3909')")
        sg_playlist_id_input.textEdited.connect(self._playlist_id_edited)
        playlist_radio_btn = QtWidgets.QRadioButton("SG Playlist Id")
        playlist_radio_btn.setChecked(True)
        input_group.addButton(playlist_radio_btn)
        input_layout.addRow(playlist_radio_btn, sg_playlist_id_input)

        sg_version_id_input = QtWidgets.QLineEdit()
        sg_version_id_input.setToolTip("Integer id of the SG Version (i.e., '314726')")
        sg_version_id_input.textEdited.connect(self._version_id_edited)
        version_radio_btn = QtWidgets.QRadioButton("SG Version Id")
        input_group.addButton(version_radio_btn)
        input_layout.addRow(version_radio_btn, sg_version_id_input)

        main_layout.addWidget(sg_input_widget)

        generate_delivery_media_btn = QtWidgets.QPushButton(
            "Generate delivery media"
        )
        generate_delivery_media_btn.setDefault(True)
        generate_delivery_media_btn.setToolTip(
            "Run the delivery media pipeline and ensure delivery media exists for all " \
            "outputs (Final Output, Review Output in ShotGrid)"
        )
        generate_delivery_media_btn.clicked.connect(
            self._on_generate_delivery_media_clicked
        )

        main_layout.addWidget(generate_delivery_media_btn)

        #### REPORT ####
        text_area = QtWidgets.QTextEdit()
        text_area.setReadOnly(True)
        text_area.setVisible(False)

        main_layout.addWidget(text_area)

        # Assign widgets we want to reuse to class instance

        self._projects_combobox = projects_combobox
        self._delivery_outputs = delivery_outputs
        self._force_delivery_media_cb = force_delivery_media_cb
        self._vendor_input = vendor_input
        self._package_name_input = package_name_input
        self._filename_input = filename_input
        self._version_input = version_input
        self._task_override_combo = task_override_combo
        self._comment_input = comment_input
        self._custom_tokens = custom_tokens
        self._template_input = template_input
        self._sg_playlist_id_input = sg_playlist_id_input
        self._sg_playlist_btn = playlist_radio_btn
        self._sg_version_id_input = sg_version_id_input
        self._sg_version_btn = version_radio_btn
        self._text_area = text_area

    def showEvent(self, event):
        super(DeliveryDialog, self).showEvent(event)
        if self._first_show:
            self._first_show = False
            self.setStyleSheet(style.load_stylesheet())
            tools_lib.center_window(self)

        if not self._initial_refresh:
            self._initial_refresh = True
            self.refresh()

    def _playlist_id_edited(self, text):
        # If there's a comma in the text, remove it and set the modified text
        text = text.replace("\t", "")
        text = text.replace(" ", "")
        text = text.replace(",", "")
        self._sg_playlist_id_input.setText(text)
        self._sg_playlist_btn.setChecked(True)

    def _version_id_edited(self, text):
        # If there's a comma in the text, remove it and set the modified text
        text = text.replace("\t", "")
        text = text.replace(" ", "")
        text = text.replace(",", "")
        self._sg_version_id_input.setText(text)
        self._sg_version_btn.setChecked(True)

    def _refresh(self):
        if not self._initial_refresh:
            self._initial_refresh = True
        self._set_projects()

    def _set_projects(self):
        # Store current project
        old_project_name = self.current_project

        self._ignore_project_change = True

        # Cleanup
        self._projects_combobox.clear()

        # Fill combobox with projects
        select_project_item = QtGui.QStandardItem("< Select project >")
        select_project_item.setData(None, QtCore.Qt.UserRole + 1)

        combobox_items = [select_project_item]

        project_names = self.get_filtered_projects()

        for project_name in sorted(project_names):
            item = QtGui.QStandardItem(project_name)
            item.setData(project_name, QtCore.Qt.UserRole + 1)
            combobox_items.append(item)

        root_item = self._projects_combobox.model().invisibleRootItem()
        root_item.appendRows(combobox_items)

        index = 0
        self._ignore_project_change = False

        if old_project_name:
            index = self._projects_combobox.findText(
                old_project_name, QtCore.Qt.MatchFixedString
            )

        self._projects_combobox.setCurrentIndex(index)

    @property
    def current_project(self):
        return self.dbcon.active_project() or None

    def get_filtered_projects(self):
        projects = list()
        for project in get_projects(fields=["name"]):
            projects.append(project["name"])

        return projects

    def on_project_change(self):
        if self._ignore_project_change:
            return

        row = self._projects_combobox.currentIndex()
        index = self._projects_combobox.model().index(row, 0)
        project_name = index.data(QtCore.Qt.UserRole + 1)

        self.dbcon.Session["AVALON_PROJECT"] = project_name

        delivery_types = ["review", "final"]
        sg_project = self.sg.find_one(
            "Project",
            [["name", "is", project_name]],
            delivery.SG_DELIVERY_OUTPUT_FIELDS + ["sg_code"]
        )
        project_overrides = delivery.get_entity_overrides(
            self.sg,
            sg_project,
            delivery_types,
            query_fields=delivery.SG_DELIVERY_OUTPUT_FIELDS,
            query_ffmpeg_args=True
        )

        logger.debug("Found project overrides: %s", project_overrides)
        # Create list of tuples of output name and its extension
        outputs_name_ext = []
        for delivery_type in delivery_types:
            out_data_types = project_overrides.get(f"sg_{delivery_type}_output_type", [])
            for data_type_name, data_type_args in out_data_types.items():
                out_name = f"{data_type_name.lower().replace(' ', '')}_{delivery_type}"
                out_extension = data_type_args["sg_extension"]
                outputs_name_ext.append((out_name, out_extension))

        logger.debug("Found outputs: %s", outputs_name_ext)
        self._delivery_outputs.update(outputs_name_ext)

        project_name = self.dbcon.active_project() or "No project selected"
        title = "{} - {}".format(self.tool_title, project_name)
        self.setWindowTitle(title)

        # Store some useful variables
        project_code = sg_project.get("sg_code")
        self._load_project_config(project_code)
        self._current_project_code = project_code

    def _save_project_config(self):
        project_code = self._current_project_code
        if not project_code:
            logger.warning("No current project selected, can't save config")
            return

        config_path = self.PROJ_DELIVERY_CONFIG.format(project_code=project_code)

        config_path_dir = os.path.dirname(config_path)
        if not os.path.exists(config_path_dir):
            os.makedirs(config_path_dir)

        delivery_data = self._get_delivery_data()
        with open(config_path, "w") as f:
            logger.info(
                "Delivery config file for project created at '%s'",
                config_path
            )
            json.dump(delivery_data, f)

    def _load_project_config(self, project_code):
        delivery_data = {}
        config_path = self.PROJ_DELIVERY_CONFIG.format(project_code=project_code)

        if not os.path.exists(config_path):
            logger.info(
                "Delivery config file for project doesn't exist at '%s'",
                config_path
            )
            return

        with open(config_path, "r") as f:
            delivery_data = json.load(f)

        # TODO: abstract this away so it's simpler to add more widgets
        # that need to get preserved across sessions
        vendor_override = delivery_data.get("vendor_override")
        if vendor_override:
            self._vendor_input.setText(vendor_override)

        package_name_override = delivery_data.get("package_name_override")
        if package_name_override:
            self._package_name_input.setText(package_name_override)

        filename_override = delivery_data.get("filename_override")
        if filename_override:
            self._filename_input.setText(filename_override)

        custom_token_pairs = delivery_data.get("custom_tokens")
        if custom_token_pairs:
            for key, value in custom_token_pairs.items():
                self._custom_tokens.add_pair(key, value)

        template_override = delivery_data.get("template_path")
        if template_override:
            self._template_input.setText(template_override)

    def _format_report(self, report_items, success):
        """Format final result and error details as html."""
        msg = "Delivery finished"
        if success:
            msg += " successfully"
        else:
            msg += " with errors"
        txt = "<h2>{}</h2>".format(msg)
        for header, data in report_items.items():
            txt += "<h3>{}</h3>".format(header)
            for item in data:
                txt += "{}<br>".format(item)

        return txt

    def _get_delivery_data(self):
        """Get all relevant data for the delivery"""
        delivery_data = {}
        delivery_data["output_names_ext"] = self._delivery_outputs.get_selected_outputs()
        delivery_data["force_delivery_media"] = self._force_delivery_media_cb.isChecked()
        delivery_data["vendor_override"] = self._vendor_input.text()
        delivery_data["package_name_override"] = self._package_name_input.text()
        delivery_data["version_override"] = self._version_input.text()
        delivery_data["task[short]_override"] = self._task_override_combo.currentText()
        delivery_data["comment_override"] = self._comment_input.text()
        delivery_data["custom_tokens"] = self._custom_tokens.get_pairs()
        delivery_data["filename_override"] = self._filename_input.text()
        delivery_data["template_path"] = self._template_input.text()

        return delivery_data

    def _on_generate_delivery_media_clicked(self):
        delivery_data = self._get_delivery_data()

        if self._sg_playlist_btn.isChecked():
            report_items, success = media.generate_delivery_media_playlist_id(
                self._sg_playlist_id_input.text(),
                delivery_data=delivery_data,
            )
        else:
            report_items, success = media.generate_delivery_media_version_id(
                self._sg_version_id_input.text(),
                delivery_data=delivery_data,
            )

        self._text_area.setText(self._format_report(report_items, success))
        self._text_area.setVisible(True)
        self._save_project_config()

    # -------------------------------
    # Delay calling blocking methods
    # -------------------------------

    def refresh(self):
        tools_lib.schedule(self._refresh, 50, channel="mongo")


def main():
    app_instance = QtWidgets.QApplication.instance()
    if app_instance is None:
        app_instance = QtWidgets.QApplication([])

    if platform.system().lower() == "windows":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sg_delivery")

    window = DeliveryDialog()
    window.show()
    app_instance.exec_()
