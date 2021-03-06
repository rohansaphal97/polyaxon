import json
import logging
import random

from django.conf import settings

from projects.paths import get_project_outputs_path
from runner.spawners.base import get_pod_volumes
from runner.spawners.project_spawner import ProjectSpawner
from runner.spawners.templates import constants, deployments, ingresses, services

logger = logging.getLogger('polyaxon.spawners.tensorboard')


class TensorboardSpawner(ProjectSpawner):
    TENSORBOARD_JOB_NAME = 'tensorboard'
    PORT = 6006

    def get_tensorboard_url(self):
        return self._get_service_url(self.TENSORBOARD_JOB_NAME)

    def request_tensorboard_port(self):
        if not self._use_ingress():
            return self.PORT

        labels = 'app={},role={}'.format(settings.APP_LABELS_TENSORBOARD,
                                         settings.ROLE_LABELS_DASHBOARD)
        ports = [service.spec.ports[0].port for service in self.list_services(labels)]
        port = random.randint(*settings.TENSORBOARD_PORT_RANGE)
        while port in ports:
            port = random.randint(*settings.TENSORBOARD_PORT_RANGE)
        return port

    def start_tensorboard(self, image, resources=None):
        ports = [self.request_tensorboard_port()]
        target_ports = [self.PORT]
        volumes, volume_mounts = get_pod_volumes()
        outputs_path = get_project_outputs_path(project_name=self.project_name)
        deployment = deployments.get_deployment(
            namespace=self.namespace,
            app=settings.APP_LABELS_TENSORBOARD,
            name=self.TENSORBOARD_JOB_NAME,
            project_name=self.project_name,
            project_uuid=self.project_uuid,
            volume_mounts=volume_mounts,
            volumes=volumes,
            image=image,
            command=["/bin/sh", "-c"],
            args=["tensorboard --logdir={} --port={}".format(outputs_path, self.PORT)],
            ports=target_ports,
            container_name=settings.CONTAINER_NAME_PLUGIN_JOB,
            resources=resources,
            role=settings.ROLE_LABELS_DASHBOARD,
            type=settings.TYPE_LABELS_EXPERIMENT)
        deployment_name = constants.DEPLOYMENT_NAME.format(
            project_uuid=self.project_uuid, name=self.TENSORBOARD_JOB_NAME)
        deployment_labels = deployments.get_labels(app=settings.APP_LABELS_TENSORBOARD,
                                                   project_name=self.project_name,
                                                   project_uuid=self.project_uuid,
                                                   role=settings.ROLE_LABELS_DASHBOARD,
                                                   type=settings.TYPE_LABELS_EXPERIMENT)

        self.create_or_update_deployment(name=deployment_name, data=deployment)
        service = services.get_service(
            namespace=self.namespace,
            name=deployment_name,
            labels=deployment_labels,
            ports=ports,
            target_ports=target_ports,
            service_type=self._get_service_type())

        self.create_or_update_service(name=deployment_name, data=service)

        if self._use_ingress():
            annotations = json.loads(settings.K8S_INGRESS_ANNOTATIONS)
            paths = [{
                'path': '/tensorboard/{}'.format(self.project_name.replace('.', '/')),
                'backend': {
                    'serviceName': deployment_name,
                    'servicePort': ports[0]
                }
            }]
            ingress = ingresses.get_ingress(namespace=self.namespace,
                                            name=deployment_name,
                                            labels=deployment_labels,
                                            annotations=annotations,
                                            paths=paths)
            self.create_or_update_ingress(name=deployment_name, data=ingress)

    def stop_tensorboard(self):
        deployment_name = constants.DEPLOYMENT_NAME.format(project_uuid=self.project_uuid,
                                                           name=self.TENSORBOARD_JOB_NAME)
        self.delete_deployment(name=deployment_name)
        self.delete_service(name=deployment_name)
        if self._use_ingress():
            self.delete_ingress(name=deployment_name)
