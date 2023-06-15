
import nuke
import pyblish.api


class IncrementScriptVersion(pyblish.api.ContextPlugin):
    """Increment current script version."""

    order = pyblish.api.IntegratorOrder + 0.9
    label = "Increment Script Version"
    optional = True
    ### Starts Alkemy-X Override ###
    # Add 'render' family as well so script version gets incremented also
    # when doing a publish of a 'render'
    families = ["workfile", "render", "render.farm"]
    ### Ends Alkemy-X Override ###
    hosts = ['nuke']

    def process(self, context):

        assert all(result["success"] for result in context.data["results"]), (
            "Publishing not successful so version is not increased.")

        from openpype.lib import version_up
        path = context.data["currentFile"]
        nuke.scriptSaveAs(version_up(path))
        self.log.info('Incrementing script version')
