import functools
import uuid

from operator import __or__ as OR

from django.conf import settings
from django.contrib.postgres.fields import JSONField
from django.contrib.postgres.fields.jsonb import KeyTransform
from django.db import models
from django.db.models import Q
from django.utils.functional import cached_property

from experiment_groups import iteration_managers, schemas, search_managers
from experiments.statuses import ExperimentLifeCycle
from libs.models import DescribableModel, DiffModel
from libs.spec_validation import validate_group_params_config, validate_group_spec_content
from polyaxon_schemas.polyaxonfile.specification import GroupSpecification
from polyaxon_schemas.settings import SettingsConfig
from polyaxon_schemas.utils import Optimization
from projects.models import Project


class ExperimentGroup(DiffModel, DescribableModel):
    """A model that saves Specification/Polyaxonfiles."""
    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        null=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='experiment_groups')
    sequence = models.PositiveSmallIntegerField(
        editable=False,
        null=False,
        help_text='The sequence number of this group within the project.', )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='experiment_groups',
        help_text='The project this polyaxonfile belongs to.')
    content = models.TextField(
        null=True,
        blank=True,
        help_text='The yaml content of the polyaxonfile/specification.',
        validators=[validate_group_spec_content])
    params = JSONField(
        help_text='The experiment group hyper params config.',
        null=True,
        blank=True,
        validators=[validate_group_params_config])
    code_reference = models.ForeignKey(
        'repos.CodeReference',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+')

    class Meta:
        ordering = ['sequence']
        unique_together = (('project', 'sequence'),)

    def save(self, *args, **kwargs):  # pylint:disable=arguments-differ
        if self.pk is None:
            last = ExperimentGroup.objects.filter(project=self.project).last()
            self.sequence = 1
            if last:
                self.sequence = last.sequence + 1

        super(ExperimentGroup, self).save(*args, **kwargs)

    def __str__(self):
        return self.unique_name

    @property
    def unique_name(self):
        return '{}.{}'.format(self.project.unique_name, self.sequence)

    @cached_property
    def params_config(self):
        return SettingsConfig.from_dict(self.params) if self.params else None

    @cached_property
    def specification(self):
        return GroupSpecification.read(self.content) if self.content else None

    @cached_property
    def concurrency(self):
        if not self.params_config:
            return None
        return self.params_config.concurrency

    @cached_property
    def search_algorithm(self):
        if not self.params_config:
            return None
        return self.params_config.search_algorithm

    @cached_property
    def early_stopping(self):
        if not self.params_config:
            return None
        return self.params_config.early_stopping or []

    @property
    def scheduled_experiments(self):
        return self.experiments.filter(
            experiment_status__status=ExperimentLifeCycle.SCHEDULED).distinct()

    @property
    def succeeded_experiments(self):
        return self.experiments.filter(
            experiment_status__status=ExperimentLifeCycle.SUCCEEDED).distinct()

    @property
    def failed_experiments(self):
        return self.experiments.filter(
            experiment_status__status=ExperimentLifeCycle.FAILED).distinct()

    @property
    def stopped_experiments(self):
        return self.experiments.filter(
            experiment_status__status=ExperimentLifeCycle.STOPPED).distinct()

    @property
    def pending_experiments(self):
        return self.experiments.filter(
            experiment_status__status__in=ExperimentLifeCycle.PENDING_STATUS).distinct()

    @property
    def running_experiments(self):
        return self.experiments.filter(
            experiment_status__status__in=ExperimentLifeCycle.RUNNING_STATUS).distinct()

    @property
    def done_experiments(self):
        return self.experiments.filter(
            experiment_status__status__in=ExperimentLifeCycle.DONE_STATUS).distinct()

    @property
    def non_done_experiments(self):
        return self.experiments.exclude(
            experiment_status__status__in=ExperimentLifeCycle.DONE_STATUS).distinct()

    @property
    def n_experiments_to_start(self):
        """We need to check if we are allowed to start the experiment
        If the polyaxonfile has concurrency we need to check how many experiments are running.
        """
        return self.concurrency - self.running_experiments.count()

    @property
    def iteration(self):
        return self.iterations.last()

    @property
    def iteration_data(self):
        return self.iteration.data if self.iteration else None

    def should_stop_early(self):
        filters = []
        for early_stopping_metric in self.early_stopping:
            comparison = (
                'gte' if Optimization.maximize(early_stopping_metric.optimization) else 'lte')
            metric_filter = 'experiment_metric__values__{}__{}'.format(
                early_stopping_metric.metric, comparison)
            filters.append({metric_filter: early_stopping_metric.value})
        if filters:
            return self.experiments.filter(functools.reduce(OR, [Q(**f) for f in filters])).exists()
        return False

    def get_annotated_experiments_with_metric(self, metric, experiment_ids=None):
        query = self.experiments
        if experiment_ids:
            query = query.filter(id__in=experiment_ids)
        annotation = {
            metric: KeyTransform(metric, 'experiment_metric__values')
        }
        return query.annotate(**annotation)

    def get_ordered_experiments_by_metric(self, experiment_ids, metric, optimization):
        query = self.get_annotated_experiments_with_metric(
            metric=metric,
            experiment_ids=experiment_ids)

        metric_order_by = '{}{}'.format(
            '-' if Optimization.maximize(optimization) else '',
            metric)
        return query.order_by(metric_order_by)

    def get_experiments_metrics(self, metric, experiment_ids=None):
        query = self.get_annotated_experiments_with_metric(
            metric=metric,
            experiment_ids=experiment_ids)
        return query.values_list('id', metric)

    @cached_property
    def search_manager(self):
        return search_managers.get_search_algorithm_manager(params_config=self.params_config)

    @cached_property
    def iteration_manager(self):
        return iteration_managers.get_search_iteration_manager(experiment_group=self)

    @property
    def iteration_config(self):
        if self.iteration_data and self.search_algorithm:
            return schemas.get_iteration_config(
                search_algorithm=self.search_algorithm,
                iteration=self.iteration_data)
        return None

    def get_suggestions(self):
        iteration_config = self.iteration_config
        if iteration_config:
            return self.search_manager.get_suggestions(iteration_config=iteration_config)
        return self.search_manager.get_suggestions()


class ExperimentGroupIteration(DiffModel):
    experiment_group = models.ForeignKey(
        ExperimentGroup,
        on_delete=models.CASCADE,
        related_name='iterations',
        help_text='The experiment group.')
    data = JSONField(
        help_text='The experiment group iteration meta data.')

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return '{} <{}>'.format(self.experiment_group, self.created_at)
