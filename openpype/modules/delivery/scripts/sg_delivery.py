"""Module for handling OP delivery of Shotgrid playlists"""
import os
import re
import copy
import collections
import click
import getpass
import json

from openpype.client import (
    get_project,
    get_version_by_id,
    get_representations,
    get_representation_by_name,
    get_subset_by_id,
    get_last_version_by_subset_name,
)
from openpype.lib import Logger, collect_frames, get_datetime_data
from openpype.lib.file_transaction import FileTransaction
from openpype.pipeline import Anatomy, legacy_io, context_tools
from openpype.pipeline.load import get_representation_path_with_anatomy
from openpype.pipeline.delivery import (
    check_destination_path,
    deliver_single_file,
)
from openpype.settings import get_system_settings
from openpype.modules.shotgrid.lib import credentials, delivery
from openpype.modules.delivery.scripts import utils


logger = Logger.get_logger(__name__)

# List of SG fields from context entities (i.e., Project, Shot) that we care to
# query for delivery purposes
SG_DELIVERY_FIELDS = [
    "sg_delivery_name",
    "sg_final_output_type",
    "sg_review_output_type",
]


def deliver_playlist_id(
    playlist_id,
    delivery_types,
    representation_names=None,
    delivery_templates=None,
):
    """Given a SG playlist id, deliver all the versions associated to it.

    Args:
        playlist_id (int): Shotgrid playlist id to deliver.
        representation_names (list): List of representation names to deliver.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        delivery_templates (dict[str, str]): Dictionary that maps different
            delivery types (i.e., 'single_file', 'sequence') to the corresponding
            templated string to use for delivery.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the delivery was successful.
    """
    report_items = collections.defaultdict(list)

    sg = credentials.get_shotgrid_session()

    sg_playlist = sg.find_one(
        "Playlist",
        [
            ["id", "is", int(playlist_id)],
        ],
        ["project"],
    )

    # Get the project name associated with the selected entities
    project_name = sg_playlist["project"]["name"]

    project_doc = get_project(project_name, fields=["name"])
    if not project_doc:
        return report_items[f"Didn't find project '{project_name}' in avalon."], False

    # Get all the SG versions associated to the playlist
    sg_versions = sg.find(
        "Version",
        [["playlists", "in", sg_playlist]],
        ["sg_op_instance_id", "entity", "code"],
    )

    # Iterate over each SG version and deliver it
    success = True
    for sg_version in sg_versions:
        new_report_items, new_success = deliver_version(
            sg_version,
            project_name,
            delivery_types,
            representation_names,
            delivery_templates,
        )
        if new_report_items:
            report_items.update(new_report_items)

        if not new_success:
            success = False

    click.echo(report_items)
    return report_items, success


def deliver_version_id(
    version_id,
    delivery_types,
    representation_names=None,
    delivery_templates=None,
):
    """Util function to deliver a single SG version given its id.

    Args:
        version_id (str): Shotgrid Version id to deliver.
        project_name (str): Name of the project corresponding to the version being
            delivered.
        delivery_data (dict[str, str]): Dictionary of relevant data for delivery.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names to deliver.
        delivery_templates (dict[str, str]): Dictionary that maps different
            delivery types (i.e., 'single_file', 'sequence') to the corresponding
            templated string to use for delivery.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the delivery was successful.
    """
    report_items = collections.defaultdict(list)

    sg = credentials.get_shotgrid_session()

    # Get all the SG versions associated to the playlist
    sg_version = sg.find_one(
        "Version",
        [["id", "is", int(version_id)]],
        ["sg_op_instance_id", "entity", "code", "project"],
    )

    if not sg_version:
        report_items["SG Version not found"].append(version_id)
        return report_items, False


    return deliver_version(
        sg_version,
        sg_version["project"]["name"],
        delivery_types,
        representation_names,
        delivery_templates,
    )


def deliver_version(
    sg_version,
    project_name,
    delivery_types,
    representation_names=None,
    delivery_templates=None,
):
    """Deliver a single SG version.

    Args:
        sg_version (): Shotgrid Version object to deliver.
        project_name (str): Name of the project corresponding to the version being
            delivered.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names to deliver.
        delivery_templates (dict[str, str]): Dictionary that maps different
            delivery types (i.e., 'single_file', 'sequence') to the corresponding
            templated string to use for delivery.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the delivery was successful.
    """
    report_items = collections.defaultdict(list)

    # Grab the OP's id corresponding to the SG version
    op_version_id = sg_version["sg_op_instance_id"]
    if not op_version_id or op_version_id == "-":
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        msg = "Missing 'sg_op_instance_id' field on SG Versions"
        report_items[msg].append(sub_msg)
        logger.error("%s: %s", msg, sub_msg)
        return report_items, False

    anatomy = Anatomy(project_name)

    sg = credentials.get_shotgrid_session()

    # Get the dictionary of relevant overrides on the hierarchy
    # of SG entities
    # NOTE: We only query representation names if they aren't given
    delivery_overrides = delivery.get_entity_hierarchy_overrides(
        sg,
        sg_version["id"],
        "Version",
        delivery_types,
        query_representation_names=bool(representation_names),
        query_delivery_names=True,
    )
    entity = None
    if not representation_names:
        # Add representation names for the current SG Version
        representation_names, entity = delivery.get_representation_names_from_overrides(
            delivery_overrides, delivery_types
        )
        logger.debug(
            "%s representation names found at '%s': %s",
            sg_version['code'],
            entity,
            representation_names
        )

    if representation_names:
        if entity != "Project":
            msg = (
                f"Override of outputs for '{sg_version['code']}' "
                f"(id: {sg_version['id']}) at the {entity} level"
            )
            logger.info("%s: %s", msg, representation_names)
            report_items[msg] = representation_names
        else:
            msg = "Project delivery representation names"
            logger.info("%s: %s", msg, representation_names)
            report_items[msg] = representation_names
    else:
        msg = "No representation names specified"
        sub_msg = "All representations will be delivered."
        logger.info("%s: %s", msg, sub_msg)
        report_items[msg] = [sub_msg]

    # Hard-code the addition of thumbnail to deliver
    representation_names.append["thumbnail"]

    # Find the OP representations we want to deliver
    repres_to_deliver = list(
        get_representations(
            project_name,
            representation_names=representation_names,
            version_ids=[op_version_id],
        )
    )
    if not repres_to_deliver:
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        msg = "None of the representations requested found on SG Versions"
        report_items[msg].append(sub_msg)
        logger.error("%s: %s", msg, sub_msg)
        return report_items, False

    for repre in repres_to_deliver:
        source_path = repre.get("data", {}).get("path")
        debug_msg = "Processing representation {}".format(repre["_id"])
        if source_path:
            debug_msg += " with published path {}.".format(source_path)
        click.echo(debug_msg)

        # Get source repre path
        frame = repre["context"].get("frame")

        if frame:
            repre["context"]["frame"] = len(str(frame)) * "#"

        # If delivery templates dictionary is passed as an argument, use that to set the
        # template token for the representation.
        delivery_template = None
        delivery_template_name = None
        if delivery_templates:
            if frame:
                template_name = "{}Sequence".format(
                    "V0 " if repre["context"]["version"] == 0 else ""
                )
            else:
                template_name = "{}Single File".format(
                    "V0 " if repre["context"]["version"] == 0 else ""
                )

            logger.info(
                "Using template name '%s' for representation '%s'",
                template_name,
                repre["data"]["path"],
            )
            delivery_template = delivery_templates[template_name]

            # Make sure we prefix the template with the io folder for the project
            if delivery_template:
                delivery_template = (
                    f"/proj/{anatomy.project_code}/io/out/{delivery_template}"
                )

        else:  # Otherwise, set it based on whether it's a sequence or a single file
            if frame:
                delivery_template_name = "sequence"
            else:
                delivery_template_name = "single_file"

        anatomy_data = copy.deepcopy(repre["context"])

        # Set overrides if passed
        # TODO: Create a function that given a delivery overrides dictionary
        # and anatomy_data it replaces the corresponding entity name
        delivery_project_name = delivery_overrides.get("Project", {}).get(
            "delivery_name"
        )
        if delivery_project_name:
            msg = "Project name overridden"
            sub_msg = "{} -> {}".format(
                anatomy_data["project"]["name"],
                delivery_project_name,
            )
            logger.info("%s: %s", msg, sub_msg)
            report_items[msg].append(sub_msg)
            anatomy_data["project"]["name"] = delivery_project_name

        delivery_shot_name = delivery_overrides.get("Shot", {}).get(
            "delivery_name"
        )
        if delivery_shot_name:
            msg = "Shot name overridden"
            sub_msg = "{} -> {}".format(
                anatomy_data["asset"],
                delivery_shot_name,
            )
            logger.info("%s: %s", msg, sub_msg)
            report_items[msg].append(sub_msg)
            anatomy_data["asset"] = delivery_shot_name

        logger.debug("Anatomy data: %s" % anatomy_data)

        repre_report_items, dest_path = check_destination_path(
            repre["_id"],
            anatomy,
            anatomy_data,
            get_datetime_data(),
            delivery_template_name,
            delivery_template,
            return_dest_path=True,
        )

        if repre_report_items:
            return repre_report_items, False

        repre_path = get_representation_path_with_anatomy(repre, anatomy)

        args = [
            repre_path,
            repre,
            anatomy,
            delivery_template_name,
            anatomy_data,
            None,
            report_items,
            logger,
            delivery_template,
        ]
        src_paths = []
        for repre_file in repre["files"]:
            src_path = anatomy.fill_root(repre_file["path"])
            src_paths.append(src_path)
        sources_and_frames = collect_frames(src_paths)

        for src_path, frame in sources_and_frames.items():
            args[0] = src_path
            if frame:
                anatomy_data["frame"] = frame
            new_report_items, _ = deliver_single_file(*args)
            # If not new report items it means the delivery was successful
            # so we append it to the list of successful delivers
            if not new_report_items:
                msg = "Successful delivered representations"
                sub_msg = f"{repre_path} -> {dest_path}<br>"
                report_items[msg].append(sub_msg)
                logger.info("%s: %s", msg, sub_msg)
            report_items.update(new_report_items)

    return report_items, True


def republish_playlist_id(
    playlist_id, delivery_types, representation_names=None, force=False
):
    """Given a SG playlist id, deliver all the versions associated to it.

    Args:
        playlist_id (int): Shotgrid playlist id to republish.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    report_items = collections.defaultdict(list)

    sg = credentials.get_shotgrid_session()

    sg_playlist = sg.find_one(
        "Playlist",
        [
            ["id", "is", int(playlist_id)],
        ],
        ["project"],
    )

    # Get the project name associated with the selected entities
    project_name = sg_playlist["project"]["name"]

    project_doc = get_project(project_name, fields=["name"])
    if not project_doc:
        return report_items[f"Didn't find project '{project_name}' in avalon."], False

    # Get all the SG versions associated to the playlist
    sg_versions = sg.find(
        "Version",
        [["playlists", "in", sg_playlist]],
        ["project", "code", "entity", "sg_op_instance_id"],
    )

    success = True
    for sg_version in sg_versions:
        new_report_items, new_success = republish_version(
            sg_version,
            project_name,
            delivery_types,
            representation_names,
            force,
        )
        if new_report_items:
            report_items.update(new_report_items)

        if not new_success:
            success = False

    click.echo(report_items)
    return report_items, success


def republish_version_id(
    version_id,
    delivery_types,
    representation_names=None,
    force=False,
):
    """Given a SG version id, republish it so it triggers the OP publish pipeline again.

    Args:
        version_id (int): Shotgrid version id to republish.
        delivery_types (list[str]): What type(s) of delivery it is so we
            regenerate those representations.
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    sg = credentials.get_shotgrid_session()

    sg_version = sg.find_one(
        "Version",
        [
            ["id", "is", int(version_id)],
        ],
        ["project", "code", "entity", "sg_op_instance_id"],
    )
    return republish_version(
        sg_version,
        sg_version["project"]["name"],
        delivery_types,
        representation_names,
        force,
    )


def republish_version(
    sg_version, project_name, delivery_types, representation_names=None, force=False
):
    """
    Republishes the given SG version by creating new review and/or final outputs.

    Args:
        sg_version (dict): The Shotgrid version to republish.
        project_name (str): The name of the Shotgrid project.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or
            not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    report_items = collections.defaultdict(list)

    # Grab the OP's id corresponding to the SG version
    op_version_id = sg_version["sg_op_instance_id"]
    if not op_version_id or op_version_id == "-":
        msg = "Missing 'sg_op_instance_id' field on SG Versions"
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # Get OP version corresponding to the SG version
    version_doc = get_version_by_id(project_name, op_version_id)
    if not version_doc:
        msg = "No OP version found for SG versions"
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # Find the OP representations we want to deliver
    exr_repre_doc = get_representation_by_name(
        project_name,
        "exr",
        version_id=op_version_id,
    )
    if not exr_repre_doc:
        msg = "No 'exr' representation found on SG versions"
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # If we are not forcing the creation of representations we validate whether the
    # representations requested already exist
    if not force:
        if not representation_names:
            sg = credentials.get_shotgrid_session()
            representation_names, entity = delivery.get_representation_names(
                sg, sg_version["id"], "Version", delivery_types
            )
            logger.debug(
                "%s representation names found at '%s': %s",
                sg_version['code'],
                entity,
                representation_names
            )

        representations = get_representations(
            project_name,
            version_ids=[op_version_id],
        )
        existing_rep_names = {rep["name"] for rep in representations}
        missing_rep_names = set(representation_names) - existing_rep_names
        if not missing_rep_names:
            msg = f"Requested '{delivery_types}' representations already exist"
            sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
            report_items[msg].append(sub_msg)
            logger.info("%s: %s", msg, sub_msg)
            return report_items, True

    exr_path = exr_repre_doc["data"]["path"]
    render_path = os.path.dirname(exr_path)

    families = version_doc["data"]["families"]
    families.append("review")

    # Add family for each delivery type to control which publish plugins
    # get executed
    for delivery_type in delivery_types:
        families.append(f"client_{delivery_type}")

    instance_data = {
        "project": project_name,
        "family": exr_repre_doc["context"]["family"],
        "subset": exr_repre_doc["context"]["subset"],
        "families": families,
        "asset": exr_repre_doc["context"]["asset"],
        "task": exr_repre_doc["context"]["task"]["name"],
        "frameStart": version_doc["data"]["frameStart"],
        "frameEnd": version_doc["data"]["frameEnd"],
        "handleStart": version_doc["data"]["handleStart"],
        "handleEnd": version_doc["data"]["handleEnd"],
        "frameStartHandle": int(
            version_doc["data"]["frameStart"] - version_doc["data"]["handleStart"]
        ),
        "frameEndHandle": int(
            version_doc["data"]["frameEnd"] + version_doc["data"]["handleEnd"]
        ),
        "comment": version_doc["data"]["comment"],
        "fps": version_doc["data"]["fps"],
        "source": version_doc["data"]["source"],
        "overrideExistingFrame": False,
        "jobBatchName": "Republish - {} - {}".format(
            sg_version["code"],
            version_doc["name"]
        ),
        "useSequenceForReview": True,
        "colorspace": version_doc["data"].get("colorspace"),
        "version": version_doc["name"],
        "outputDir": render_path,
    }

    # Inject variables into session
    legacy_io.Session["AVALON_ASSET"] = instance_data["asset"]
    legacy_io.Session["AVALON_TASK"] = instance_data.get("task")
    legacy_io.Session["AVALON_WORKDIR"] = render_path
    legacy_io.Session["AVALON_PROJECT"] = project_name
    legacy_io.Session["AVALON_APP"] = "traypublisher"

    # Replace frame number with #'s for expected_files function
    hashes_path = re.sub(
        r"\d+(?=\.\w+$)", lambda m: "#" * len(m.group()) if m.group() else "#", exr_path
    )

    expected_files = utils.expected_files(
        hashes_path,
        instance_data["frameStartHandle"],
        instance_data["frameEndHandle"],
    )
    logger.debug("__ expectedFiles: `{}`".format(expected_files))

    representations = utils.get_representations(
        instance_data,
        expected_files,
        False,
    )

    # inject colorspace data
    for rep in representations:
        source_colorspace = instance_data["colorspace"] or "scene_linear"
        logger.debug("Setting colorspace '%s' to representation", source_colorspace)
        utils.set_representation_colorspace(
            rep, project_name, colorspace=source_colorspace
        )

    if "representations" not in instance_data.keys():
        instance_data["representations"] = []

    # add representation
    instance_data["representations"] += representations
    instances = [instance_data]

    render_job = {}
    render_job["Props"] = {}
    # Render job doesn't exist because we do not have prior submission.
    # We still use data from it so lets fake it.
    #
    # Batch name reflect original scene name

    render_job["Props"]["Batch"] = instance_data.get("jobBatchName")

    # User is deadline user
    render_job["Props"]["User"] = getpass.getuser()

    # get default deadline webservice url from deadline module
    deadline_url = get_system_settings()["modules"]["deadline"]["deadline_urls"][
        "default"
    ]

    metadata_path = utils.create_metadata_path(instance_data)
    logger.info("Metadata path: %s", metadata_path)

    deadline_publish_job_id = utils.submit_deadline_post_job(
        instance_data, render_job, render_path, deadline_url, metadata_path
    )

    report_items["Submitted republish job to Deadline"].append(deadline_publish_job_id)

    # Inject deadline url to instances.
    for inst in instances:
        inst["deadlineUrl"] = deadline_url

    # publish job file
    publish_job = {
        "asset": instance_data["asset"],
        "frameStart": instance_data["frameStartHandle"],
        "frameEnd": instance_data["frameEndHandle"],
        "fps": instance_data["fps"],
        "source": instance_data["source"],
        "user": getpass.getuser(),
        "version": None,  # this is workfile version
        "intent": None,
        "comment": instance_data["comment"],
        "job": render_job or None,
        "session": legacy_io.Session.copy(),
        "instances": instances,
    }

    if deadline_publish_job_id:
        publish_job["deadline_publish_job_id"] = deadline_publish_job_id

    logger.info("Writing json file: {}".format(metadata_path))
    with open(metadata_path, "w") as f:
        json.dump(publish_job, f, indent=4, sort_keys=True)

    click.echo(report_items)
    return report_items, True


def generate_delivery_media_playlist_id(
    playlist_id,
    delivery_types,
    representation_names=None,
    force=False,
    description=None,
    override_version=None,
):
    """Given a SG playlist id, deliver all the versions associated to it.

    Args:
        playlist_id (int): Shotgrid playlist id to republish.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    report_items = collections.defaultdict(list)

    sg = credentials.get_shotgrid_session()

    sg_playlist = sg.find_one(
        "Playlist",
        [
            ["id", "is", int(playlist_id)],
        ],
        ["project"],
    )

    # Get the project name associated with the selected entities
    project_name = sg_playlist["project"]["name"]

    project_doc = get_project(project_name, fields=["name"])
    if not project_doc:
        return report_items[f"Didn't find project '{project_name}' in avalon."], False

    # Get all the SG versions associated to the playlist
    sg_versions = sg.find(
        "Version",
        [["playlists", "in", sg_playlist]],
        ["project", "code", "entity", "sg_op_instance_id"],
    )

    success = True
    for sg_version in sg_versions:
        new_report_items, new_success = generate_delivery_media_version(
            sg_version,
            project_name,
            delivery_types,
            representation_names,
            force,
            description,
            override_version,
        )
        if new_report_items:
            report_items.update(new_report_items)

        if not new_success:
            success = False

    click.echo(report_items)
    return report_items, success


def generate_delivery_media_version_id(
    version_id,
    delivery_types,
    representation_names=None,
    force=False,
    description=None,
    override_version=None,
):
    """Given a SG version id, generate its corresponding delivery so it
        triggers the OP publish pipeline again.

    Args:
        version_id (int): Shotgrid version id to republish.
        delivery_types (list[str]): What type(s) of delivery it is so we
            regenerate those representations.
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    sg = credentials.get_shotgrid_session()

    sg_version = sg.find_one(
        "Version",
        [
            ["id", "is", int(version_id)],
        ],
        ["project", "code", "entity", "sg_op_instance_id"],
    )
    return generate_delivery_media_version(
        sg_version,
        sg_version["project"]["name"],
        delivery_types,
        representation_names,
        force,
        description,
        override_version,
    )


def generate_delivery_media_version(
    sg_version,
    project_name,
    delivery_types,
    representation_names=None,
    force=False,
    description=None,
    override_version=None,
):
    """
    Generate the corresponding delivery version given SG version by creating a new
        subset with review and/or final outputs.

    Args:
        sg_version (dict): The Shotgrid version to republish.
        project_name (str): The name of the Shotgrid project.
        delivery_types (list[str]): What type(s) of delivery it is
            (i.e., ["final", "review"])
        representation_names (list): List of representation names that should exist on
            the representations being published.
        force (bool): Whether to force the creation of the delivery representations or
            not.

    Returns:
        tuple: A tuple containing a dictionary of report items and a boolean indicating
            whether the republish was successful.
    """
    report_items = collections.defaultdict(list)

    # Grab the OP's id corresponding to the SG version
    op_version_id = sg_version["sg_op_instance_id"]
    if not op_version_id or op_version_id == "-":
        msg = "Missing 'sg_op_instance_id' field on SG Versions"
        sub_msg = f"{project_name} - {sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # Get OP version corresponding to the SG version
    version_doc = get_version_by_id(project_name, op_version_id)
    if not version_doc:
        msg = "No OP version found for SG versions"
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # Find the OP representations we want to deliver
    exr_repre_doc = get_representation_by_name(
        project_name,
        "exr",
        version_id=op_version_id,
    )
    if not exr_repre_doc:
        msg = "No 'exr' representation found on SG versions"
        sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
        logger.error("%s: %s", msg, sub_msg)
        report_items[msg].append(sub_msg)
        return report_items, False

    # Query subset of the version so we can construct its equivalent delivery
    # subset
    subset_doc = get_subset_by_id(project_name, version_doc["parent"], fields=["name"])

    delivery_subset_name = "delivery_{}".format(subset_doc["name"])
    if description:
        delivery_subset_name = "{}_{}".format(
            delivery_subset_name, description
        )

    # If we are not forcing the creation of representations we validate whether
    # the representations requested already exist
    if not force:
        if not representation_names:
            sg = credentials.get_shotgrid_session()
            representation_names, entity = delivery.get_representation_names(
                sg, sg_version["id"], "Version", delivery_types
            )
            logger.debug(
                "%s representation names found at '%s': %s",
                sg_version['code'],
                entity,
                representation_names
            )

        last_delivery_version = get_last_version_by_subset_name(
            project_name,
            delivery_subset_name
        )
        if last_delivery_version:
            representations = get_representations(
                project_name,
                version_ids=[last_delivery_version["_id"]],
            )
        else:
            representations = []

        existing_rep_names = {rep["name"] for rep in representations}
        missing_rep_names = set(representation_names) - existing_rep_names
        if not missing_rep_names:
            msg = f"Requested '{delivery_types}' representations already exist"
            sub_msg = f"{sg_version['code']} - id: {sg_version['id']}<br>"
            report_items[msg].append(sub_msg)
            logger.info("%s: %s", msg, sub_msg)
            return report_items, True

    # Add family for each delivery type to control which publish plugins
    # get executed
    families = []
    for delivery_type in delivery_types:
        families.append(f"client_{delivery_type}")

    frame_start_handle = int(
        version_doc["data"]["frameStart"] - version_doc["data"]["handleStart"]
    )
    frame_end_handle = int(
        version_doc["data"]["frameEnd"] + version_doc["data"]["handleEnd"]
    )
    logger.debug("Frame start handle: %s", frame_start_handle)
    logger.debug("Frame end handle: %s", frame_end_handle)

    instance_data = {
        "project": project_name,
        "family": exr_repre_doc["context"]["family"],
        "subset": delivery_subset_name,
        "families": families,
        "asset": exr_repre_doc["context"]["asset"],
        "task": exr_repre_doc["context"]["task"]["name"],
        "frameStart": version_doc["data"]["frameStart"],
        "frameEnd": version_doc["data"]["frameEnd"],
        "handleStart": version_doc["data"]["handleStart"],
        "handleEnd": version_doc["data"]["handleEnd"],
        "frameStartHandle": frame_start_handle,
        "frameEndHandle": frame_end_handle,
        "comment": version_doc["data"]["comment"],
        "fps": version_doc["data"]["fps"],
        "source": version_doc["data"]["source"],
        "overrideExistingFrame": False,
        "jobBatchName": "Generate delivery media - {} - {}".format(
            sg_version["code"],
            delivery_subset_name
        ),
        "useSequenceForReview": True,
        "colorspace": version_doc["data"].get("colorspace"),
        "customData": {"description": description}
    }

    # If we are specifying the version to generate we set it on the instance
    if override_version:
        instance_data["version"] = override_version

    # Copy source files from original version to a temporary location which will be used
    # for staging
    exr_path = exr_repre_doc["data"]["path"]
    # Replace frame number with #'s for expected_files function
    hashes_path = re.sub(
        r"\d+(?=\.\w+$)", lambda m: "#" * len(m.group()) if m.group() else "#", exr_path
    )
    expected_files = utils.expected_files(
        hashes_path,
        frame_start_handle,
        frame_end_handle,
    )
    logger.debug("__ Source expectedFiles: `{}`".format(expected_files))

    # Inject variables into session
    legacy_io.Session["AVALON_ASSET"] = instance_data["asset"]
    legacy_io.Session["AVALON_TASK"] = instance_data.get("task")
    legacy_io.Session["AVALON_PROJECT"] = project_name
    legacy_io.Session["AVALON_APP"] = "traypublisher"

    # Calculate temporary directory where we will copy the source files to
    # and use as the delivery media staging directory while publishing
    temp_delivery_dir = os.path.join(
        context_tools.get_workdir_from_session(), "temp_delivery"
    )
    legacy_io.Session["AVALON_WORKDIR"] = temp_delivery_dir
    # Set outputDir on instance data as that's used to define where
    # to save the metadata path
    instance_data["outputDir"] = temp_delivery_dir

    # file_transactions = FileTransaction(
    #     log=logger,
    #     # Enforce unique transfers
    #     allow_queue_replacements=False
    # )
    # expected_files = []
    # for src_file in src_expected_files:
    #     filename = os.path.basename(src_file)
    #     dst_file = os.path.join(temp_delivery_dir, filename)
    #     file_transactions.add(src_file, dst_file)
    #     expected_files.append(dst_file)
    # logger.debug("Copying source files to destination ...")
    # file_transactions.process()
    # logger.debug("Backed up existing files: {}".format(file_transactions.backups))
    # logger.debug("Transferred files: {}".format(file_transactions.transferred))

    logger.debug("__ expectedFiles: `{}`".format(expected_files))

    # TODO: do we need the publish directory in advance?
    # I think it's only required for Deadline to check output
    # output_dir = self._get_publish_folder(
    #     anatomy,
    #     deepcopy(instance.data["anatomyData"]),
    #     instance.data.get("asset"),
    #     instances[0]["subset"],
    #     instance.context,
    #     instances[0]["family"],
    #     override_version
    # )

    representations = utils.get_representations(
        instance_data,
        expected_files,
        do_not_add_review=True,
    )

    # inject colorspace data
    for rep in representations:
        source_colorspace = instance_data["colorspace"] or "scene_linear"
        logger.debug("Setting colorspace '%s' to representation", source_colorspace)
        utils.set_representation_colorspace(
            rep, project_name, colorspace=source_colorspace
        )

    if "representations" not in instance_data.keys():
        instance_data["representations"] = []

    # add representation
    instance_data["representations"] += representations
    instances = [instance_data]

    render_job = {}
    render_job["Props"] = {}
    # Render job doesn't exist because we do not have prior submission.
    # We still use data from it so lets fake it.
    #
    # Batch name reflect original scene name

    render_job["Props"]["Batch"] = instance_data.get("jobBatchName")

    # User is deadline user
    render_job["Props"]["User"] = getpass.getuser()

    # get default deadline webservice url from deadline module
    deadline_url = get_system_settings()["modules"]["deadline"]["deadline_urls"][
        "default"
    ]

    metadata_path = utils.create_metadata_path(instance_data)
    logger.info("Metadata path: %s", metadata_path)

    deadline_publish_job_id = utils.submit_deadline_post_job(
        instance_data, render_job, temp_delivery_dir, deadline_url, metadata_path
    )

    report_items["Submitted generate delivery media job to Deadline"].append(
        deadline_publish_job_id
    )

    # Inject deadline url to instances.
    for inst in instances:
        inst["deadlineUrl"] = deadline_url

    # publish job file
    publish_job = {
        "asset": instance_data["asset"],
        "frameStart": instance_data["frameStartHandle"],
        "frameEnd": instance_data["frameEndHandle"],
        "fps": instance_data["fps"],
        "source": instance_data["source"],
        "user": getpass.getuser(),
        "version": None,  # this is workfile version
        "intent": None,
        "comment": instance_data["comment"],
        "job": render_job or None,
        "session": legacy_io.Session.copy(),
        "instances": instances,
    }

    if deadline_publish_job_id:
        publish_job["deadline_publish_job_id"] = deadline_publish_job_id

    logger.info("Writing json file: {}".format(metadata_path))
    with open(metadata_path, "w") as f:
        json.dump(publish_job, f, indent=4, sort_keys=True)

    click.echo(report_items)
    return report_items, True
