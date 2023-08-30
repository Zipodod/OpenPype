from openpype.lib import Logger
from openpype.modules.shotgrid.lib import credentials


logger = Logger.get_logger(__name__)


def add_tasks_to_sg_entities(project, sg_entities, entity_type, tasks):
    """Add given tasks to the SG entities of the specified entity type.

    Args:
        project (dict): A dictionary representing the SG project to which the
            tasks will be added.
        sg_entities (list): A list of dictionaries representing the SG entities
            to which the tasks will be added.
        entity_type (str): A string representing the type of SG entity to which
            the tasks will be added.
    """
    sg = credentials.get_shotgrid_session()

    # Create list of dictionaries with the common data we will be using to
    # create all tasks
    # NOTE: we do this outside of the other for loop as we don't want to query
    # the pipeline step for each single entity
    tasks_data = []
    for task_name, step_name in tasks.items():
        step = sg.find_one(
            "Step",
            [["code", "is", step_name], ["entity_type", "is", entity_type]]
        )
        # Create a task for this entity
        task_data = {
            "project": project,
            "content": task_name,
            "step": step,
        }
        tasks_data.append(task_data)

    # Loop through each entity and create the task
    for sg_entity in sg_entities:
        for task_data in tasks_data:
            task_data["entity"] = sg_entity
            sg.create("Task", task_data)
            logger.info(
                "Task '%s' created at '%s'", task_data["content"], sg_entity["code"]
            )


def populate_tasks(project_code):
    """Populate default tasks for all episodes, sequences, shots and assets in the
        given SG project.

    Args:
        project_code (str): A string representing the code name of the SG
            project to which the tasks will be added.
    """
    sg = credentials.get_shotgrid_session()

    # Dictionary of tasks -> pipeline step that we want created on all
    # entities of a project
    # NOTE: Currently the task names and the pipeline step names are
    # matching but that wouldn't necessarily be the case for all
    default_tasks = {
        "edit": "Edit",
        "generic": "Generic",
    }

    # Find the project with the given code
    project = sg.find_one("Project", [["sg_code", "is", project_code]])

    # Try add tasks to all Episodes
    episodes = sg.find("Episode", [["project", "is", project]], ["id", "code"])
    if episodes:
        add_tasks_to_sg_entities(project, episodes, "Episode", default_tasks)

    # Try add tasks to all Sequences
    sequences = sg.find("Sequence", [["project", "is", project]], ["id", "code"])
    if sequences:
        add_tasks_to_sg_entities(project, sequences, "Sequence", default_tasks)

    # For child entities we ignore "generic" task
    default_tasks.pop("generic")

    # Try add tasks to all Shots
    shots = sg.find("Shot", [["project", "is", project]], ["id", "code"])
    if shots:
        add_tasks_to_sg_entities(project, shots, "Shot", default_tasks)

    # Try add tasks to all Assets
    assets = sg.find("Asset", [["project", "is", project]], ["id", "code"])
    if assets:
        add_tasks_to_sg_entities(project, assets, "Asset", default_tasks)
