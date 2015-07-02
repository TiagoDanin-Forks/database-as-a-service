# -*- coding: utf-8 -*-
from __future__ import absolute_import
from celery.utils.log import get_task_logger
from celery.exceptions import SoftTimeLimitExceeded
from dbaas.celery import app
from util.decorators import only_one
from util import email_notifications
from util.providers import make_infra
from util.providers import clone_infra
from util.providers import destroy_infra
from util import get_worker_name
from util import full_stack
from django.db.models import Sum, Count
from physical.models import Plan
from physical.models import DatabaseInfra
from physical.models import Instance
from logical.models import Database
from account.models import Team
from system.models import Configuration
from simple_audit.models import AuditRequest
from .models import TaskHistory
import datetime

LOG = get_task_logger(__name__)


def get_history_for_task_id(task_id):
    try:
        return TaskHistory.objects.get(task_id=task_id)
    except Exception:
        LOG.error("could not find history for task id %s" % task_id)
        return None


def rollback_database(dest_database):
    dest_database.is_in_quarantine = True
    dest_database.save()
    dest_database.delete()


@app.task(bind=True)
def create_database(self, name, plan, environment, team, project, description, task_history=None, user=None):
    AuditRequest.new_request("create_database", user, "localhost")
    try:

        worker_name = get_worker_name()
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history.update_details(persist=True, details="Loading Process...")

        result = make_infra(plan=plan,
                            environment=environment,
                            name=name,
                            team=team,
                            project=project,
                            description=description,
                            task=task_history,
                            )

        if result['created'] == False:

            if 'exceptions' in result:
                error = "\n".join(": ".join(err)
                                  for err in result['exceptions']['error_codes'])
                traceback = "\nException Traceback\n".join(
                    result['exceptions']['traceback'])
                error = "{}\n{}\n{}".format(error, traceback, error)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(
                TaskHistory.STATUS_ERROR, details=error)
            return

        task_history.update_dbid(db=result['database'])
        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='Database created successfully')

        return

    except Exception, e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)

        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=traceback)
        return

    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def destroy_database(self, database, task_history=None, user=None):
    # register History
    AuditRequest.new_request("destroy_database", user, "localhost")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history.update_details(persist=True, details="Loading Process...")

        databaseinfra = database.databaseinfra

        destroy_infra(databaseinfra=databaseinfra, task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='Database destroyed successfully')
        return
    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def clone_database(self, origin_database, clone_name, plan, environment, task_history=None, user=None):
    AuditRequest.new_request("clone_database", user, "localhost")
    try:
        worker_name = get_worker_name()
        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        LOG.info("origin_database: %s" % origin_database)

        task_history.update_details(persist=True, details="Loading Process...")
        result = clone_infra(plan=plan,
                             environment=environment,
                             name=clone_name,
                             team=origin_database.team,
                             project=origin_database.project,
                             description=origin_database.description,
                             task=task_history,
                             clone=origin_database,
                             )

        if result['created'] == False:

            if 'exceptions' in result:
                error = "\n\n".join(": ".join(err)
                                    for err in result['exceptions']['error_codes'])
                traceback = "\n\nException Traceback\n".join(
                    result['exceptions']['traceback'])
                error = "{}\n{}".format(error, traceback)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(
                TaskHistory.STATUS_ERROR, details=error)
            return

        task_history.update_dbid(db=result['database'])
        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='\nDatabase cloned successfully')

    except SoftTimeLimitExceeded:
        LOG.error("task id %s - timeout exceeded" % self.request.id)
        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details="timeout exceeded")
        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)
            return
    except Exception, e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)

        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=traceback)

        return

    finally:
        AuditRequest.cleanup_request()


@app.task
@only_one(key="db_infra_notification_key", timeout=20)
def databaseinfra_notification(self, user=None):
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, user=user, worker_name=worker_name)
    threshold_infra_notification = Configuration.get_by_name_as_int(
        "threshold_infra_notification", default=0)
    if threshold_infra_notification <= 0:
        LOG.warning("database infra notification is disabled")
        return

    # Sum capacity per databseinfra with parameter plan, environment and engine
    infras = DatabaseInfra.objects.values('plan__name', 'environment__name', 'engine__engine_type__name',
                                          'plan__provider').annotate(capacity=Sum('capacity'))
    for infra in infras:
        # total database created in databaseinfra per plan, environment and
        # engine
        used = DatabaseInfra.objects.filter(plan__name=infra['plan__name'],
                                            environment__name=infra[
                                                'environment__name'],
                                            engine__engine_type__name=infra['engine__engine_type__name']).aggregate(
            used=Count('databases'))
        # calculate the percentage
        percent = int(used['used'] * 100 / infra['capacity'])
        if percent >= threshold_infra_notification and infra['plan__provider'] != Plan.CLOUDSTACK:
            LOG.info('Plan %s in environment %s with %s%% occupied' % (
                infra['plan__name'], infra['environment__name'], percent))
            LOG.info("Sending database infra notification...")
            context = {}
            context['plan'] = infra['plan__name']
            context['environment'] = infra['environment__name']
            context['used'] = used['used']
            context['capacity'] = infra['capacity']
            context['percent'] = percent
            email_notifications.databaseinfra_ending(context=context)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS,
                                       details='Databaseinfra Notification successfully sent to dbaas admins!')
    return


def database_notification_for_team(team=None):
    """
    Notifies teams of database usage.
    if threshold_database_notification <= 0, the notification is disabled.
    """
    LOG.info("sending database notification for team %s" % team)
    threshold_database_notification = Configuration.get_by_name_as_int(
        "threshold_database_notification", default=0)
    # if threshold_database_notification
    if threshold_database_notification <= 0:
        LOG.warning("database notification is disabled")
        return

    databases = Database.objects.filter(team=team)
    msgs = []
    for database in databases:
        used = database.used_size_in_mb
        capacity = database.total_size_in_mb
        try:
            percent_usage = (used / capacity) * 100
        except ZeroDivisionError:
            # database has no total size
            percent_usage = 0.0
        msg = "database %s => usage: %.2f | threshold: %.2f" % (
            database, percent_usage, threshold_database_notification)
        LOG.info(msg)
        msgs.append(msg)

        if not team.email:
            msgs.append(
                "team %s has no email set and therefore no database usage notification will been sent" % team)
        else:
            if percent_usage >= threshold_database_notification:
                LOG.info("Sending database notification...")
                context = {}
                context['database'] = database.name
                context['team'] = team
                context['measure_unity'] = "MB"
                context['used'] = used
                context['capacity'] = capacity
                context['percent'] = "%.2f" % percent_usage
                context['environment'] = database.environment.name
                email_notifications.database_usage(context=context)

    return msgs


@app.task(bind=True)
@only_one(key="db_notification_key", timeout=180)
def database_notification(self):
    """
    Create tasks for database notification by team
    if threshold_database_notification <= 0, the notification is disabled.
    """
    # get all teams and for each one create a new task
    LOG.info("retrieving all teams and sendind database notification")
    teams = Team.objects.all()
    msgs = {}

    for team in teams:
        ###############################################
        # create task
        ###############################################

        msgs[team] = database_notification_for_team(team=team)
        ###############################################

    try:
        LOG.info("Messages: ")
        LOG.info(msgs)

        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            str(key) + ': ' + ', '.join(value) for key, value in msgs.items()))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_databases_status", timeout=180)
def update_database_status(self):
    LOG.info("Retrieving all databases")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status.is_alive:
                database.status = Database.ALIVE

                instances_status = database.databaseinfra.check_instances_status()
                if instances_status == database.databaseinfra.ALERT:
                    database.status = Database.ALERT

            else:
                database.status = Database.DEAD

            database.save()
            msg = "\nUpdating status for database: {}, status: {}".format(
                database, database.status)
            msgs.append(msg)
            LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_databases_used_size", timeout=180)
def update_database_used_size(self):
    LOG.info("Retrieving all databases")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status:
                database.used_size_in_bytes = float(
                    database.database_status.used_size_in_bytes)
            else:
                database.used_size_in_bytes = 0.0

            database.save()
            msg = "\nUpdating used size in bytes for database: {}, used size: {}".format(
                database, database.used_size_in_bytes)
            msgs.append(msg)
            LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_instances_status", timeout=180)
def update_instances_status(self):
    LOG.info("Retrieving all databaseinfras")
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, user=None, worker_name=worker_name)

    try:
        infras = DatabaseInfra.objects.all()
        msgs = []
        for databaseinfra in infras:
            LOG.info("Retrieving all instances for {}".format(databaseinfra))

            for instance in Instance.objects.filter(databaseinfra=databaseinfra, is_arbiter=False):
                if instance.check_status():
                    instance.status = Instance.ALIVE
                else:
                    instance.status = Instance.DEAD

                instance.save()

                msg = "\nUpdating instance status, instance: {}, status: {}".format(
                    instance, instance.status)
                msgs.append(msg)
                LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="purge_task_history", timeout=600)
def purge_task_history(self):
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)

        now = datetime.datetime.now()
        retention_days = Configuration.get_by_name_as_int(
            'task_history_retention_days')

        n_days_before = now - datetime.timedelta(days=retention_days)

        tasks_to_purge = TaskHistory.objects.filter(task_name__in=['notification.tasks.database_notification',
                                                                   'notification.tasks.database_notification_for_team',
                                                                   'notification.tasks.update_database_status',
                                                                   'notification.tasks.update_database_used_size',
                                                                   'notification.tasks.update_instances_status',
                                                                   'system.tasks.set_celery_healthcheck_last_update'], ended_at__lt=n_days_before, task_status__in=["SUCCESS", "ERROR"])

        tasks_to_purge.delete()

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS,
                                       details='Purge succesfully done!')
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)


@app.task(bind=True)
def resize_database(self, database, cloudstackpack, task_history=None, user=None):

    AuditRequest.new_request("resize_database", user, "localhost")

    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)
        from util.providers import resize_database

        result = resize_database(
            database=database, cloudstackpack=cloudstackpack, task=task_history)

        if result['created'] == False:

            if 'exceptions' in result:
                error = "\n".join(": ".join(err)
                                  for err in result['exceptions']['error_codes'])
                traceback = "\nException Traceback\n".join(
                    result['exceptions']['traceback'])
                error = "{}\n{}\n{}".format(error, traceback, error)
            else:
                error = "Something went wrong."

            task_history.update_status_for(
                TaskHistory.STATUS_ERROR, details=error)
        else:
            task_history.update_status_for(
                TaskHistory.STATUS_SUCCESS, details='Resize successfully done.')

    except Exception, e:
        error = "Resize Database ERROR: {}".format(e)
        LOG.error(error)
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)

    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def volume_migration(self, database, user, task_history=None):
    from dbaas_nfsaas.models import HostAttr, PlanAttr
    from workflow.settings import VOLUME_MIGRATION
    from util import build_dict
    from workflow.workflow import start_workflow
    from time import sleep

    def switch_master(databaseinfra, instance):
        driver = databaseinfra.get_driver()
        for attempt in range(0, 21):
            if driver.is_replication_ok(instance):
                driver.switch_master()
                return
            LOG.info("Waiting 10s to check replication...")
            sleep(10)

    default_plan_size = PlanAttr.objects.get(dbaas_plan=database.plan).nfsaas_plan

    LOG.info("Migrating {} volumes".format(database))
    worker_name = get_worker_name()

    task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                        user=user, worker_name=worker_name)

    databaseinfra = database.databaseinfra
    driver = databaseinfra.get_driver()

    environment = database.environment
    plan = database.plan

    instance_types = [Instance.MYSQL, Instance.MONGODB, Instance.REDIS]

    instances = []
    master_instance = None
    for instance in databaseinfra.instances.filter(instance_type__in=instance_types):
        if driver.check_instance_is_eligible_for_backup(instance=instance):
            instances.append(instance)
        else:
            master_instance = instance

    if master_instance:
        instances.append(master_instance)

    LOG.info('Instances: {}'.format(str(instances)))

    for index, instance in enumerate(instances):

        if not driver.check_instance_is_eligible_for_backup(instance=instance):
            LOG.info('Instance is not eligible for backup {}'.format(str(instance)))
            continue
        LOG.info('Volume migration for instance {}'.format(str(instance)))
        host = instance.hostname
        old_volume = HostAttr.objects.get(host=host, is_active=True)

        if old_volume.nfsaas_size_id == default_plan_size:
            if databaseinfra.plan.is_ha:
                switch_master(databaseinfra, instance)
            continue

        workflow_dict = build_dict(databaseinfra=databaseinfra,
                                   database=database,
                                   environment=environment,
                                   plan=plan,
                                   host=host,
                                   instance=instance,
                                   old_volume=old_volume,
                                   steps=VOLUME_MIGRATION,
                                   )

        start_workflow(workflow_dict=workflow_dict, task=task_history)

        if workflow_dict['exceptions']['traceback']:
            error = "\n".join(": ".join(err) for err in workflow_dict['exceptions']['error_codes'])
            traceback = "\nException Traceback\n".join(workflow_dict['exceptions']['traceback'])
            error = "{}\n{}\n{}".format(error, traceback, error)
            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
            LOG.info("Migration finished with errors")
            return

        if databaseinfra.plan.is_ha:
            LOG.info("Waiting 60s to check continue...")
            sleep(60)
            switch_master(databaseinfra, instance)
            LOG.info("Waiting 60s to check continue...")
            sleep(60)

    task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Volumes sucessfully migrated!')

    LOG.info("Migration finished")

    return
