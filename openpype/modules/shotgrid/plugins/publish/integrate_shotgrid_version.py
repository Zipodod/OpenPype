import re
import pyblish.api

from openpype.pipeline.publish import get_publish_repre_path


class IntegrateShotgridVersion(pyblish.api.InstancePlugin):
    """Integrate Shotgrid Version"""

    order = pyblish.api.IntegratorOrder + 0.497
    label = "Shotgrid Version"
    ### Starts Alkemy-X Override ###
    fields_to_add = {
        "comment": (str, "description"),
        "family": (str, "sg_version_type"),
    }
    families = [
        "plate",
        "render",
        "reference",
        "arnold_rop",
        "mantra_rop",
        "karma_rop"
    ]
    ### Ends Alkemy-X Override ###

    sg = None

    def process(self, instance):
        ### Starts Alkemy-X Override ###
        # Skip execution if instance is marked to be processed in the farm
        if instance.data.get("farm"):
            self.log.info(
                "Instance is marked to be processed on farm. Skipping")
            return
        ### Ends Alkemy-X Override ###

        context = instance.context
        self.sg = context.data.get("shotgridSession")

        # TODO: Use path template solver to build version code from settings
        anatomy = instance.data.get("anatomyData", {})
        ### Starts Alkemy-X Override ###
        code = "{}_{}_{}".format(
            anatomy["asset"],
            instance.data["subset"],
            "v{:03}".format(int(anatomy["version"]))
        )
        self.log.info("Integrating Shotgrid version with code: {}".format(code))
        ### Ends Alkemy-X Override ###

        ### Starts Alkemy-X Override ###
        version = self._find_existing_version(code, context, instance)
        ### Ends Alkemy-X Override ###

        if not version:
            ### Starts Alkemy-X Override ###
            version = self._create_version(code, context, instance)
            ### Ends Alkemy-X Override ###
            self.log.info("Create Shotgrid version: {}".format(version))
        else:
            self.log.info("Use existing Shotgrid version: {}".format(version))

        data_to_update = {}
        intent = context.data.get("intent")
        if intent:
            data_to_update["sg_status_list"] = intent["value"]

        ### Starts Alkemy-X Override ###
        frame_start = instance.data.get("frameStart") or context.data.get("frameStart")
        frame_end = instance.data.get("frameEnd") or context.data.get("frameEnd")
        handle_start = instance.data.get("handleStart") or context.data.get("handleStart")
        handle_end = instance.data.get("handleEnd") or context.data.get("handleEnd")
        if frame_start != None and handle_start != None:
            data_to_update["sg_first_frame"] = frame_start - handle_start
            self.log.info("Adding field '{}' to SG as '{}':'{}'".format(
                    "frameStart", "sg_first_frame", frame_start - handle_start)
                )
        if frame_end != None and handle_end != None:
            data_to_update["sg_last_frame"] = frame_end + handle_end
            self.log.info("Adding field '{}' to SG as '{}':'{}'".format(
                    "frameEnd", "sg_last_frame", frame_end + handle_end)
                )
        # Add a few extra fields from OP to SG version
        for op_field, sg_field in self.fields_to_add.items():
            field_value = instance.data.get(op_field) or context.data.get(op_field)
            if field_value:
                # Break sg_field tuple into whatever type of data it is and its name
                type_, field_name = sg_field
                self.log.info("Adding field '{}' to SG as '{}':'{}'".format(
                    op_field, field_name, field_value)
                )
                data_to_update[field_name] = type_(field_value)

        # Add version objectId to "sg_op_instance_id" so we can keep a link
        # between both
        version_entity = instance.data.get("versionEntity", {}).get("_id")
        if not version_entity:
            self.log.warning(
                "Instance doesn't have a 'versionEntity' to extract the id."
            )
            version_entity = "-"
        data_to_update["sg_op_instance_id"] = str(version_entity)
        ### Ends Alkemy-X Override ###

        for representation in instance.data.get("representations", []):
            local_path = get_publish_repre_path(
                instance, representation, False
            )
            self.log.debug(
                "Checking whether to integrate representation '%s'.", representation
            )
            if "shotgridreview" in representation.get("tags", []):
                self.log.debug("Integrating representation")
                if representation["ext"] in ["mov", "avi", "mp4"]:
                    self.log.info(
                        "Upload review: {} for version shotgrid {}".format(
                            local_path, version.get("id")
                        )
                    )
                    self.sg.upload(
                        "Version",
                        version.get("id"),
                        local_path,
                        field_name="sg_uploaded_movie",
                    )

                    data_to_update["sg_path_to_movie"] = local_path
                    ### Starts Alkemy-X Override ###
                    if (
                        "slate" in instance.data["families"]
                        and "slate-frame" in representation["tags"]
                    ):
                        data_to_update["sg_movie_has_slate"] = True
                    ### Ends Alkemy-X Override ###

                elif representation["ext"] in ["jpg", "png", "exr", "tga"]:
                    # Define the pattern to match the frame number
                    padding_pattern = r"\.\d+\."
                    # Replace the frame number with '%04d'
                    path_to_frame = re.sub(padding_pattern, ".%04d.", local_path)

                    data_to_update["sg_path_to_frames"] = path_to_frame
                    ### Starts Alkemy-X Override ###
                    if "slate" in instance.data["families"]:
                        data_to_update["sg_frames_have_slate"] = True
                    ### Ends Alkemy-X Override ###

        self.log.info("Updating Shotgrid version with {}".format(data_to_update))
        self.sg.update("Version", version["id"], data_to_update)

        instance.data["shotgridVersion"] = version

    ### Starts Alkemy-X Override ###
    def _find_existing_version(self, code, context, instance):
    ### Ends Alkemy-X Override ###
        filters = [
            ["project", "is", context.data.get("shotgridProject")],
            ["sg_task", "is", context.data.get("shotgridTask")],
            ### Starts Alkemy-X Override ###
            ["entity", "is", instance.data.get("shotgridEntity")],
            ### Ends Alkemy-X Override ###
            ["code", "is", code],
        ]
        return self.sg.find_one("Version", filters, ["entity"])

    ### Starts Alkemy-X Override ###
    def _create_version(self, code, context, instance):
    ### Ends Alkemy-X Override ###
        version_data = {
            "project": context.data.get("shotgridProject"),
            "sg_task": context.data.get("shotgridTask"),
            ### Starts Alkemy-X Override ###
            "entity": instance.data.get("shotgridEntity"),
            ### Ends Alkemy-X Override ###
            "code": code,
        }
        return self.sg.create("Version", version_data)
