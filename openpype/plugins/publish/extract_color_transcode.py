import os
import copy
import clique
import pyblish.api

from openpype.pipeline import publish
from openpype.lib import (
    is_oiio_supported,
)

from openpype.lib.transcoding import (
    convert_colorspace,
    get_transcode_temp_directory,
)

from openpype.lib.profiles_filtering import filter_profiles


class ExtractOIIOTranscode(publish.Extractor):
    """
    Extractor to convert colors from one colorspace to different.

    Expects "colorspaceData" on representation. This dictionary is collected
    previously and denotes that representation files should be converted.
    This dict contains source colorspace information, collected by hosts.

    Target colorspace is selected by profiles in the Settings, based on:
    - families
    - host
    - task types
    - task names
    - subset names

    Can produce one or more representations (with different extensions) based
    on output definition in format:
        "output_name: {
            "extension": "png",
            "colorspace": "ACES - ACEScg",
            "display": "",
            "view": "",
            "tags": [],
            "custom_tags": []
        }

    If 'extension' is empty original representation extension is used.
    'output_name' will be used as name of new representation. In case of value
        'passthrough' name of original representation will be used.

    'colorspace' denotes target colorspace to be transcoded into. Could be
    empty if transcoding should be only into display and viewer colorspace.
    (In that case both 'display' and 'view' must be filled.)
    """

    label = "Transcode color spaces"
    order = pyblish.api.ExtractorOrder + 0.019
    ### Starts Alkemy-X Override ###
    # Filter plugin so it only gets executed for `client_review` family, which
    # we are currently controlling with a Client Review toggle on the publisher
    # In the future we might want to run the transcode for other cases but
    # for now this simplifies our pipeline so we can have more control over
    # when the transcoding happens.
    families = ["client_review", "client_final"]

    # Skeleton of an output definition of a profile
    profile_output_skeleton = {
        "extension": "",
        "transcoding_type": "colorspace",
        "colorspace": "",
        "display": "",
        "view": "",
        "oiiotool_args": {"additional_command_args": []},
        "tags": [],
        "custom_tags": [],
    }
    ### Ends Alkemy-X Override ###
    optional = True

    # Supported extensions
    supported_exts = ["exr", "jpg", "jpeg", "png", "dpx"]

    # Configurable by Settings
    profiles = None
    options = None

    def process(self, instance):
        if not self.profiles:
            self.log.debug("No profiles present for color transcode")
            return

        if "representations" not in instance.data:
            self.log.debug("No representations, skipping.")
            return

        if not is_oiio_supported():
            self.log.warning("OIIO not supported, no transcoding possible.")
            return

        profile = self._get_profile(instance)
        if not profile:
            return

        ### Starts Alkemy-X Override ###
        # Adds support to define review profiles from SG instead of OP settings
        sg_outputs, lut_colorspace_review = self.get_sg_output_profiles(instance)
        if sg_outputs:
            self.log.info(
                "Found some profiles on the Shotgrid instance: %s", sg_outputs
            )
            profile["outputs"].update(sg_outputs)

        # If 'Review Lut' on the SG entity is not enabled we override it so the
        # colorspace is not the default "input_process"
        if not lut_colorspace_review:
            profile["outputs"]["review"]["colorspace"] = "delivery_frame"

        if "client_review" not in instance.families:
            self.log.debug("Removing 'review' from profile because 'client_review' is not part of the families.")
            profile["outputs"].pop("review")

        if "client_final" not in instance.families:
            self.log.debug("Removing 'final' from profile because 'client_final' is not part of the families.")
            profile["outputs"].pop("final")

        self.log.debug("Profile: %s", profile)
        ### Ends Alkemy-X Override ###

        new_representations = []
        repres = instance.data["representations"]
        for idx, repre in enumerate(list(repres)):
            self.log.debug("repre ({}): `{}`".format(idx + 1, repre["name"]))
            if not self._repre_is_valid(repre):
                continue

            added_representations = False
            added_review = False

            colorspace_data = repre["colorspaceData"]
            source_colorspace = colorspace_data["colorspace"]
            config_path = colorspace_data.get("config", {}).get("path")
            if not config_path or not os.path.exists(config_path):
                self.log.warning("Config file doesn't exist, skipping")
                continue

            for output_name, output_def in profile.get("outputs", {}).items():
                new_repre = copy.deepcopy(repre)

                original_staging_dir = new_repre["stagingDir"]
                new_staging_dir = get_transcode_temp_directory()
                new_repre["stagingDir"] = new_staging_dir

                if isinstance(new_repre["files"], list):
                    files_to_convert = copy.deepcopy(new_repre["files"])
                else:
                    files_to_convert = [new_repre["files"]]

                output_extension = output_def["extension"]
                output_extension = output_extension.replace('.', '')
                self._rename_in_representation(new_repre,
                                               files_to_convert,
                                               output_name,
                                               output_extension)

                transcoding_type = output_def["transcoding_type"]

                target_colorspace = view = display = None
                if transcoding_type == "colorspace":
                    target_colorspace = (output_def["colorspace"] or
                                         colorspace_data.get("colorspace"))
                else:
                    view = output_def["view"] or colorspace_data.get("view")
                    display = (output_def["display"] or
                               colorspace_data.get("display"))

                # both could be already collected by DCC,
                # but could be overwritten when transcoding
                if view:
                    new_repre["colorspaceData"]["view"] = view
                if display:
                    new_repre["colorspaceData"]["display"] = display
                if target_colorspace:
                    new_repre["colorspaceData"]["colorspace"] = \
                        target_colorspace

                additional_command_args = (output_def["oiiotool_args"]
                                           ["additional_command_args"])

                files_to_convert = self._translate_to_sequence(
                    files_to_convert)
                for file_name in files_to_convert:
                    input_path = os.path.join(original_staging_dir,
                                              file_name)
                    output_path = self._get_output_file_path(input_path,
                                                             new_staging_dir,
                                                             output_extension)
                    convert_colorspace(
                        input_path,
                        output_path,
                        config_path,
                        source_colorspace,
                        target_colorspace,
                        view,
                        display,
                        additional_command_args,
                        self.log
                    )
                    self.log.info(
                        "Converted '%s' from colorspace '%s' to colorspace '%s' and saved to '%s'",
                        input_path,
                        source_colorspace,
                        target_colorspace,
                        output_path,
                    )

                # cleanup temporary transcoded files
                for file_name in new_repre["files"]:
                    transcoded_file_path = os.path.join(new_staging_dir,
                                                        file_name)
                    instance.context.data["cleanupFullPaths"].append(
                        transcoded_file_path)

                custom_tags = output_def.get("custom_tags")
                if custom_tags:
                    if new_repre.get("custom_tags") is None:
                        new_repre["custom_tags"] = []
                    new_repre["custom_tags"].extend(custom_tags)

                # Add additional tags from output definition to representation
                if new_repre.get("tags") is None:
                    new_repre["tags"] = []

                # Remove shotgridreview from tags of new representations
                if "shotgridreview" in new_repre["tags"]:
                    new_repre["tags"].remove("shotgridreview")

                for tag in output_def["tags"]:
                    if tag not in new_repre["tags"]:
                        new_repre["tags"].append(tag)

                    if tag == "review":
                        added_review = True

                # If there is only 1 file outputted then convert list to
                # string, cause that'll indicate that its not a sequence.
                if len(new_repre["files"]) == 1:
                    new_repre["files"] = new_repre["files"][0]

                new_representations.append(new_repre)
                added_representations = True

            if added_representations:
                self._mark_original_repre_for_deletion(repre, profile,
                                                       added_review)

        for repre in tuple(instance.data["representations"]):
            tags = repre.get("tags") or []
            if "delete" in tags and "thumbnail" not in tags:
                instance.data["representations"].remove(repre)

        instance.data["representations"].extend(new_representations)

    ### Starts Alkemy-X Override ###
    def get_sg_output_profiles(self, instance):
        """Returns a dictionary of profiles based on delivery overrides set on
        the SG instance.

        If there are delivery overrides set on the Shotgrid instance, this
        method returns a dictionary of output profiles that matches what OP
        profiles expect based on those overrides. Otherwise, it returns None.

        Args:
            instance (Instance): The instance to get Shotgrid output profiles
                for.

        Returns:
            dict: A dictionary of Shotgrid output profiles, or None if there
                are no delivery overrides.
        """
        # Check if there's any delivery overrides set on the SG instance
        # and use that instead of the profile output definitions if that's
        # the case
        delivery_overrides_dict = instance.context.data.get("shotgridDeliveryOverrides")
        if not delivery_overrides_dict:
            return None

        # Boolean to figure out whether we need to override the default 'review' profile
        # colorspace based on if 'Review LUT' field is enabled
        lut_colorspace_review = False

        sg_profiles = {}
        for hierarchy_level, override_entity in enumerate(["project", "shot"]):
            ent_overrides = delivery_overrides_dict[override_entity]

            # Whether we need to override the review colorspace
            lut_colorspace_review = ent_overrides["sg_review_lut"]

            for delivery_type in ["review", "final"]:
                delivery_outputs = ent_overrides[f"sg_{delivery_type}_output_type"]
                # If on the next run of the hierarchy loop there delivery
                # outputs it means these should override the prior entity
                # so we clear the sg_profiles entries for that delivery type
                # i.e., if there's different output types on the shot than
                # the project
                if hierarchy_level > 0 and delivery_outputs:
                    self.log.info(
                        "There's '%s' delivery overrides on the SG entity '%s', " \
                        "clearing '%s' overrides from parent entity.",
                        delivery_type, override_entity, delivery_type
                    )
                    sg_profiles = {
                        k: v for k, v in sg_profiles.items() if not k.endswith(delivery_type)
                    }

                for out_name, out_fields in delivery_outputs.items():
                    # Only run extract review for the output types that are image
                    # extensions
                    if out_fields["sg_extension"] not in self.supported_exts:
                        self.log.debug(
                            "Skipping profile '%s' because it's not a supported extension",
                            out_name
                        )
                        continue
                    sg_profiles[out_name] = self.profile_output_skeleton.copy()
                    sg_profiles[out_name]["extension"] = out_fields["sg_extension"]
                    sg_profiles[out_name]["tags"] = [
                        tag["name"] for tag in ent_overrides[f"sg_{delivery_type}_tags"]
                    ]

                    dest_colorspace = "delivery_frame"
                    if delivery_type == "review" and lut_colorspace_review:
                        dest_colorspace = "input_process"

                    sg_profiles[out_name]["colorspace"] = dest_colorspace

        return sg_profiles, lut_colorspace_review
    ### Ends Alkemy-X Override ###

    def _rename_in_representation(self, new_repre, files_to_convert,
                                  output_name, output_extension):
        """Replace old extension with new one everywhere in representation.

        Args:
            new_repre (dict)
            files_to_convert (list): of filenames from repre["files"],
                standardized to always list
            output_name (str): key of output definition from Settings,
                if "<passthrough>" token used, keep original repre name
            output_extension (str): extension from output definition
        """
        if output_name != "passthrough":
            new_repre["name"] = output_name
        if not output_extension:
            return

        new_repre["ext"] = output_extension

        renamed_files = []
        for file_name in files_to_convert:
            file_name, _ = os.path.splitext(file_name)
            file_name = '{}.{}'.format(file_name,
                                       output_extension)
            renamed_files.append(file_name)
        new_repre["files"] = renamed_files

    def _rename_in_representation(self, new_repre, files_to_convert,
                                  output_name, output_extension):
        """Replace old extension with new one everywhere in representation.

        Args:
            new_repre (dict)
            files_to_convert (list): of filenames from repre["files"],
                standardized to always list
            output_name (str): key of output definition from Settings,
                if "<passthrough>" token used, keep original repre name
            output_extension (str): extension from output definition
        """
        if output_name != "passthrough":
            new_repre["name"] = output_name
        if not output_extension:
            return

        new_repre["ext"] = output_extension

        renamed_files = []
        for file_name in files_to_convert:
            file_name, _ = os.path.splitext(file_name)
            file_name = '{}.{}'.format(file_name,
                                       output_extension)
            renamed_files.append(file_name)
        new_repre["files"] = renamed_files

    def _translate_to_sequence(self, files_to_convert):
        """Returns original list or list with filename formatted in single
        sequence format.

        Uses clique to find frame sequence, in this case it merges all frames
        into sequence format (FRAMESTART-FRAMEEND#) and returns it.
        If sequence not found, it returns original list

        Args:
            files_to_convert (list): list of file names
        Returns:
            (list) of [file.1001-1010#.exr] or [fileA.exr, fileB.exr]
        """
        pattern = [clique.PATTERNS["frames"]]
        collections, remainder = clique.assemble(
            files_to_convert, patterns=pattern,
            assume_padded_when_ambiguous=True)

        if collections:
            if len(collections) > 1:
                raise ValueError(
                    "Too many collections {}".format(collections))

            collection = collections[0]
            frames = list(collection.indexes)
            frame_str = "{}-{}#".format(frames[0], frames[-1])
            file_name = "{}{}{}".format(collection.head, frame_str,
                                        collection.tail)

            files_to_convert = [file_name]

        return files_to_convert

    def _get_output_file_path(self, input_path, output_dir,
                              output_extension):
        """Create output file name path."""
        file_name = os.path.basename(input_path)
        file_name, input_extension = os.path.splitext(file_name)
        if not output_extension:
            output_extension = input_extension.replace(".", "")
        new_file_name = '{}.{}'.format(file_name,
                                       output_extension)
        return os.path.join(output_dir, new_file_name)

    def _get_profile(self, instance):
        """Returns profile if and how repre should be color transcoded."""
        host_name = instance.context.data["hostName"]
        family = instance.data["family"]
        task_data = instance.data["anatomyData"].get("task", {})
        task_name = task_data.get("name")
        task_type = task_data.get("type")
        subset = instance.data["subset"]
        filtering_criteria = {
            "hosts": host_name,
            "families": family,
            "task_names": task_name,
            "task_types": task_type,
            "subsets": subset
        }
        profile = filter_profiles(self.profiles, filtering_criteria,
                                  logger=self.log)

        if not profile:
            self.log.info((
              "Skipped instance. None of profiles in presets are for"
              " Host: \"{}\" | Families: \"{}\" | Task \"{}\""
              " | Task type \"{}\" | Subset \"{}\" "
            ).format(host_name, family, task_name, task_type, subset))

        self.log.debug("profile: {}".format(profile))
        return profile

    def _repre_is_valid(self, repre):
        """Validation if representation should be processed.

        Args:
            repre (dict): Representation which should be checked.

        Returns:
            bool: False if can't be processed else True.
        """

        if repre.get("ext") not in self.supported_exts:
            self.log.debug((
                "Representation '{}' has unsupported extension: '{}'. Skipped."
            ).format(repre["name"], repre.get("ext")))
            return False

        if not repre.get("files"):
            self.log.debug((
                "Representation '{}' has empty files. Skipped."
            ).format(repre["name"]))
            return False

        if not repre.get("colorspaceData"):
            self.log.debug("Representation '{}' has no colorspace data. "
                           "Skipped.")
            return False

        return True

    def _mark_original_repre_for_deletion(self, repre, profile, added_review):
        """If new transcoded representation created, delete old."""
        if not repre.get("tags"):
            repre["tags"] = []

        delete_original = profile["delete_original"]

        if delete_original:
            if "delete" not in repre["tags"]:
                repre["tags"].append("delete")

        # if added_review and "review" in repre["tags"]:
            # repre["tags"].remove("review")
