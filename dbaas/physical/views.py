# -*- coding: utf-8 -*-
import json
from django.http import HttpResponse
from django.shortcuts import render_to_response
from physical.models import Environment, Engine


def engines_by_env(self, environment_id):
    environment = Environment.objects.get(id=environment_id)
    plans = environment.active_plans()

    engines = []
    for plan in plans:
        if plan.engine.id not in engines:
            engines.append(plan.engine.id)

    response_json = json.dumps({
        "engines": engines
    })
    return HttpResponse(response_json, content_type="application/json")


def topology_by_eng(self, engine_id):
    engine = Engine.objects.get(id=engine_id)
    replication_topologies = engine.replication_topologies.all()

    replication_topologies_id = []
    for replication_topology in replication_topologies:
        replication_topologies_id.append(replication_topology.pk)

    response_json = json.dumps({
        "topology": replication_topologies_id
    })
    return HttpResponse(response_json, content_type="application/json")


def offering_by_engine(self, engine_id, environment_id):
    engine = Engine.objects.get(id=engine_id)
    environment = Environment.objects.get(id=environment_id)
    plans = engine.plans.filter(environments=environment, is_active=True
                                ).values('stronger_offering__cpus',
                                         'stronger_offering__memory_size_mb',
                                         'is_ha')
    return HttpResponse(plans, content_type="application/json")


def plans_details(self):
    context = {
        "environments" : Environment.objects.all(),
        "engines": Engine.objects.all(),
    }
    return render_to_response(
        "plans/plans_details.html",
        context
    )
