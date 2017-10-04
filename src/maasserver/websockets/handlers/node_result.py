# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The NodeResult handler for the WebSocket connection."""

__all__ = [
    "NodeResultHandler",
    ]


from django.core.exceptions import ValidationError
from maasserver.models.node import Node
from maasserver.websockets.base import (
    dehydrate_datetime,
    HandlerDoesNotExistError,
)
from maasserver.websockets.handlers.timestampedmodel import (
    TimestampedModelHandler,
)
from metadataserver.enum import HARDWARE_TYPE
from metadataserver.models import ScriptResult


class NodeResultHandler(TimestampedModelHandler):

    class Meta:
        queryset = ScriptResult.objects.all()
        pk = 'id'
        allowed_methods = [
            'get',
            'get_result_data',
            'list',
        ]
        listen_channels = ['scriptresult']
        exclude = [
            "script_set",
            "script_name",
            "output",
            "stdout",
            "stderr",
        ]
        list_fields = [
            "id",
            "updated",
            "script",
            "parameters",
            "physical_blockdevice",
            "script_version",
            "status",
            "exit_status",
            "started",
            "ended",
        ]

    def dehydrate_parameters(self, parameters):
        # Parameters is a JSONObjectField to convert it to a dictionary it must
        # be accessed.
        return parameters

    def dehydrate_started(self, started):
        return dehydrate_datetime(started)

    def dehydrate_ended(self, ended):
        return dehydrate_datetime(ended)

    def dehydrate(self, obj, data, for_list=False):
        """Add extra fields to `data`."""
        data["name"] = obj.name
        data["status_name"] = obj.status_name
        data["runtime"] = obj.runtime
        data["result_type"] = obj.script_set.result_type
        if obj.script is not None:
            data["hardware_type"] = obj.script.hardware_type
            data["tags"] = ", ".join(obj.script.tags)
        else:
            data["hardware_type"] = HARDWARE_TYPE.NODE
            data["tags"] = []
        data["history_list"] = [
            {
                "id": history.id,
                "updated": dehydrate_datetime(history.updated),
                "status": history.status,
                "status_name": history.status_name,
                "runtime": history.runtime,
            } for history in obj.history
        ]
        try:
            results = obj.read_results()
        except ValidationError as e:
            data["results"] = [{
                "name": "error",
                "title": "Error",
                "description": "An error has occured while processing.",
                "value": str(e),
                "surfaced": True,
            }]
        else:
            data["results"] = []
            for key, value in results.get("results", {}).items():
                if obj.script is not None:
                    if isinstance(obj.script.results, dict):
                        title = obj.script.results.get(key, {}).get(
                            "title", key)
                        description = obj.script.results.get(key, {}).get(
                            "description", "")
                    # Only show surfaced results for builtin scripts. Result
                    # data from the user script is only shown in on the storage
                    # or test tabs.
                    surfaced = obj.script.default
                else:
                    # Only builtin commissioning scripts don't have an
                    # associated Script object. If MAAS ever includes result
                    # data in the builtin commissioning scripts show it.
                    title = key
                    description = ''
                    surfaced = True
                data["results"].append({
                    "name": key,
                    "title": title,
                    "description": description,
                    "value": value,
                    "surfaced": surfaced,
                })
        return data

    def list(self, params):
        """List objects.

        :param system_id: `Node.system_id` for the script results.
        :param result_type: Only return results with this result type.
        :param hardware_type: Only return results with this hardware type.
        :param physical_blockdevice_id: Only return the results associated
           with the blockdevice_id.
        :param has_surfaced: Only return results if they have surfaced.
        """
        try:
            node = Node.objects.get(system_id=params["system_id"])
        except Node.DoesNotExist:
            raise HandlerDoesNotExistError(params["system_id"])
        queryset = node.get_latest_script_results

        if "result_type" in params:
            queryset = queryset.filter(
                script_set__result_type=params["result_type"])
        if "hardware_type" in params:
            queryset = queryset.filter(
                script__hardware_type=params["hardware_type"])
        if "physical_blockdevice_id" in params:
            queryset = queryset.filter(physical_blockdevice_id=params[
                "physical_blockdevice_id"])
        if "has_surfaced" in params:
            if params["has_surfaced"]:
                queryset = queryset.exclude(result='')

        return [
            self.full_dehydrate(obj, for_list=True)
            for obj in queryset
        ]

    def get_result_data(self, params):
        """Return the raw script result data."""
        id = params.get('id')
        try:
            script_result = ScriptResult.objects.get(id=id)
        except ScriptResult.DoesNotExist:
            return "Unknown ScriptResult id %s" % id
        data_type = params.get('data_type', 'combined')
        if data_type == 'combined':
            return script_result.output.decode().strip()
        elif data_type == 'stdout':
            return script_result.stdout.decode().strip()
        elif data_type == 'stderr':
            return script_result.stderr.decode().strip()
        elif data_type == 'result':
            return script_result.result.decode().strip()
        else:
            return "Unknown data_type %s" % data_type