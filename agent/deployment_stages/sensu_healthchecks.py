# Copyright (c) Trainline Limited, 2016-2017. All rights reserved. See LICENSE.txt in the project root for license information.

import json, os, re, sys
from jsonschema import Draft4Validator
from .common import DeploymentError, DeploymentStage, find_healthchecks, get_previous_deployment_appspec
from .schemas import SensuHealthCheckSchema
from .healthcheck_utils import HealthcheckTypes, HealthcheckUtils
from .health_check import HealthCheck

def create_sensu_check_definition_filename(service_id, check_id, slice='none'):
    return '{0}-{1}-{2}.json'.format(service_id, check_id, slice)

class DeregisterOldSensuHealthChecks(DeploymentStage):
    def __init__(self):
        DeploymentStage.__init__(self, name='DeregisterOldSensuHealthChecks')
    def _run(self, deployment):
        if deployment.last_id is None:
            deployment.logger.info('Skipping {0} stage as there is no previous deployment.'.format(self.name))
        else:
            deployment.logger.info('Deregistering Sensu healthchecks from previous deployment.')
            previous_appspec = get_previous_deployment_appspec(deployment)
            if previous_appspec is None:
                deployment.logger.warning('Previous deployment directory not found, id: {0}'.format(deployment.last_id))
            else:
                (healthchecks, scripts_base_dir) = find_healthchecks('sensu', deployment.last_archive_dir, previous_appspec, deployment.logger)
                deployment.logger.debug('Sensu healthchecks to remove: {0}'.format(healthchecks))
                if healthchecks is None:
                    deployment.logger.warning('No sensu checks will be removed')
                    return
                for check_id, check in healthchecks.iteritems():
                    deployment.logger.debug('Looking for sensu check: {0}'.format(check_id))
                    check_definition_absolute_path = os.path.join(deployment.sensu['sensu_check_path'], create_sensu_check_definition_filename(deployment.service.id, check_id, deployment.service.slice))
                    if os.path.exists(check_definition_absolute_path):
                        deployment.logger.info('Removing healthcheck: {0}'.format(check_definition_absolute_path))
                        os.remove(check_definition_absolute_path)
                    else:
                        deployment.logger.warning('Could not find file: {0}'.format(check_definition_absolute_path))

class RegisterSensuHealthChecks(DeploymentStage):
    def __init__(self):
        DeploymentStage.__init__(self, name='RegisterSensuHealthChecks')

    def _run(self, deployment):
        deployment.logger.info('Registering Sensu checks.')
        (sensu_checks, scripts_base_dir) = find_healthchecks('sensu', deployment.archive_dir, deployment.appspec, deployment.logger)
        if sensu_checks is None:
            deployment.logger.info('No Sensu checks to register.')
            return
        RegisterSensuHealthChecks.validate_checks(sensu_checks, scripts_base_dir, deployment)
        for check_id, check in sensu_checks.iteritems():
            RegisterSensuHealthChecks.register_check(check_id, check, deployment)

    @staticmethod
    def find_sensu_plugin(plugin_paths, script_filename):
        for plugin_path in plugin_paths:
            script_filepath = os.path.join(plugin_path, script_filename)
            if os.path.exists(script_filepath):
                return '{0}'.format(script_filepath)
        return None

    @staticmethod
    def generate_check_definition(check_data, deployment):
        instance_tags = deployment.instance_tags
        deployment_slice = deployment.service.slice
        if deployment_slice is not None and deployment_slice.lower() == 'none':
            deployment_slice = None

        check = HealthCheck.create(check_data, deployment, deployment_slice, deployment.logger)
        check_definition = { 'checks': { check.name: check.get_definition() } }
        
        custom_instance_tags = {k:v for k, v in instance_tags.iteritems() if not k.startswith('aws:')}
        for key, value in custom_instance_tags.iteritems():
            check_definition['checks'][check.name]['ttl_' + key.lower()] = value

        return check_definition

    @staticmethod
    def register_check(check_id, check, deployment):
        check_definition = RegisterSensuHealthChecks.generate_check_definition(check, deployment)
        check_definition_filename = create_sensu_check_definition_filename(deployment.service.id, check_id, deployment.service.slice)
        check_definition_absolute_path = os.path.join(deployment.sensu['sensu_check_path'], check_definition_filename)
        is_success = RegisterSensuHealthChecks.write_check_definition_file(check_definition, check_definition_absolute_path, deployment)
        if not is_success:
            raise DeploymentError('Failed to register Sensu check \'{0}\''.format(check_id))

    @staticmethod
    def validate_checks(checks, scripts_base_dir, deployment):
        for check_id, check in checks.iteritems():
            RegisterSensuHealthChecks.validate_check_properties(check_id, check)
            RegisterSensuHealthChecks.validate_check_script(check, scripts_base_dir, deployment)
        RegisterSensuHealthChecks.validate_unique_ids(checks)
        RegisterSensuHealthChecks.validate_unique_names(checks)

    @staticmethod
    def validate_check_properties(check_id, check):
        Draft4Validator(SensuHealthCheckSchema).validate(check)
        check_type = HealthcheckUtils.get_type(check)
        
        if not re.match(r'^[\w\.-]+$', check['name']):
            raise DeploymentError('Health check name \'{0}\' doesn\'t match required Sensu name expression {1}'.format(check['name'], '/^[\w\.-]+$/'))
        if 'local_script' in check and 'server_script' in check:
            raise DeploymentError('Failed to register health check \'{0}\', you can use either \'local_script\' or \'server_script\', but not both.'.format(check_id))
        if not ('local_script' in check or 'server_script' in check or check_type == HealthcheckTypes.HTTP):
            raise DeploymentError('Failed to register health check \'{0}\', you need at least one of: \'local_script\' or \'server_script\''.format(check_id))
        if 'standalone' in check and 'aggregate' in check:
            if check['standalone'] is True and check['aggregate'] is True:
                raise DeploymentError('Either standalone or aggregate can be True at the same time')
            if check['standalone'] is False and check['aggregate'] is False:
                raise DeploymentError('Either standalone or aggregate can be False at the same time')

    @staticmethod
    def validate_check_script(check, local_scripts_base_dir, deployment):
        if 'script' in check:
            if check['script'].startswith('/'):
                check['script'] = check['script'][1:]
            absolute_file_path = os.path.join(deployment.archive_dir, local_scripts_base_dir, check['script'])
            if not os.path.exists(absolute_file_path):
                raise DeploymentError('Couldn\'t find Sensu check script in package with path: {0}'.format(os.path.join(local_scripts_base_dir, check['script'])))
            check['script'] = absolute_file_path
        elif 'plugin' in check:
            absolute_file_path = RegisterSensuHealthChecks.find_sensu_plugin(deployment.sensu['healthcheck_search_paths'], check['plugin'])
            if absolute_file_path is None:
                raise DeploymentError('Couldn\'t find Sensu plugin script: {0}\nPaths searched: {1}'.format(check['plugin'], deployment.sensu['healthcheck_search_paths']))
            check['plugin'] = absolute_file_path

    @staticmethod
    def validate_unique_ids(checks):
        check_ids = [check_id.lower() for check_id in checks.keys()]
        if len(check_ids) != len(set(check_ids)):
            raise DeploymentError('Sensu check definitions require unique ids (case insensitive)')

    @staticmethod
    def validate_unique_names(checks):
        check_names = [check['name'] for check in checks.values()]
        if len(check_names) != len(set(check_names)):
            raise DeploymentError('Sensu check definitions require unique names (case insensitive)')

    @staticmethod
    def write_check_definition_file(check_definition, check_definition_absolute_path, deployment):
        try:
            with open(check_definition_absolute_path, 'w') as check_definition_file:
                check_definition_file.write(json.dumps(check_definition, sort_keys=True, indent=4, separators=(',', ': ')))
            deployment.logger.info('Created Sensu check definition: {0}'.format(check_definition_absolute_path))
            return True
        except:
            deployment.logger.exception(sys.exc_info()[1])
            return False
