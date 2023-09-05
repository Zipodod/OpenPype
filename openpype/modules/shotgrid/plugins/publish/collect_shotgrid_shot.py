import pyblish.api


class CollectShotgridShot(pyblish.api.InstancePlugin):
    """Collect proper shotgrid entity according to the current asset name"""

    order = pyblish.api.CollectorOrder + 0.4999
    label = "Collect Shotgrid Shot"
    hosts = ["hiero"]
    families = ["plate", "take", "reference"]

    def process(self, instance):
        context = instance.context

        anatomy_data = instance.data.get("anatomyData", {})
        sg = context.data.get("shotgridSession")

        self.log.info("Looking for shot associated with clip name")
        sg_shot = _get_shotgrid_shot(sg, anatomy_data)

        if sg_shot:
            ### Starts Alkemy-X Override ###
            instance.data["shotgridEntity"] = sg_shot
            ### Ends Alkemy-X Override ###
            self.log.info(
                "Overriding entity with corresponding shot for clip: {}".format(sg_shot)
            )
        else:
            raise Exception(
                "No Shotgrid shot found under clip name: {}".format(
                    anatomy_data["asset"]
                )
            )


def _get_shotgrid_shot(sg, anatomy):
    shot_name = anatomy["asset"]
    # OP project name/code isn't always sg_code. This approach gives a sure fire way
    # to match to a SG project
    filters = [
        [
            "project.Project.name",
            "in",
            [anatomy["project"]["name"]],
        ],
        ["code", "is", shot_name],
    ]
    sg_shot = sg.find_one("Shot", filters, ["code"])

    return sg_shot
