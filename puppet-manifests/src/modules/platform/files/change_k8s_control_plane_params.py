# Copyright (c) 2021-2022 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from contextlib import contextmanager
import json
import logging
import os
import re
import requests
import ruamel.yaml as yaml
import shutil
import signal
import subprocess
from subprocess import CalledProcessError
import sys
import time

from sysinv.common import kubernetes  # pylint: disable=import-error
from sysinv.common import service_parameter as sp  # pylint: disable=import-error

# pylint: disable-msg=broad-except

kube_operator = kubernetes.KubeOperator()

# Logging
LOGGER_FORMAT = "%(asctime)s.%(msecs)03d %(process)d [%(levelname)s] %(message)s"
LOGGER_NAME = 'k8s_control_plane_update'
LOG = logging.getLogger(LOGGER_NAME)
LOG.setLevel(logging.DEBUG)
root_logs = '/var/log/puppet/latest/'
if not os.path.exists(root_logs):
    os.makedirs(root_logs)
log_format = logging.Formatter(LOGGER_FORMAT)
fullname = os.path.join(root_logs, 'k8s_update.log')
fileHandler = logging.FileHandler(fullname)
fileHandler.setFormatter(log_format)
LOG.addHandler(fileHandler)
LOG.debug('Starting k8s update process.')

post_k8s_tasks = []

DEFAULT_TAG = 'platform::kubernetes::params::'
KUBE_APISERVER_TAG = 'platform::kubernetes::kube_apiserver::params::'
CONTROLLER_MANAGER_TAG = 'platform::kubernetes::kube_controller_manager::params::'
SCHEDULER_TAG = 'platform::kubernetes::kube_scheduler::params::'
ETCD_TAG = 'platform::kubernetes::params::etcd_'
CONFIG_TAG = 'platform::kubernetes::config::params::'
KUBELET_TAG = 'platform::kubernetes::kubelet::params::'
KUBE_APISERVER_CONFIG = '/etc/kubernetes/manifests/kube-apiserver.yaml'

KUBE_APISERVER_VOLUMES_TAG = 'platform::kubernetes::kube_apiserver_volumes::params::'
CONTROLLER_MANAGER_VOLUMES_TAG = 'platform::kubernetes::kube_controller_manager_volumes::params::'
SCHEDULER_VOLUMES_TAG = 'platform::kubernetes::kube_scheduler_volumes::params::'

REGEXPR_ADVERTISE_ADDRESS = r"advertise-address=(.*)\s"
APISERVER_READYZ_ENDPOINT = 'https://localhost:6443/readyz'
SCHEDULER_HEALTHZ_ENDPOINT = "https://127.0.0.1:10259/healthz"
CONTROLLER_MANAGER_HEALTHZ_ENDPOINT = "https://127.0.0.1:10257/healthz"
KUBELET_HEALTHZ_ENDPOINT = "http://localhost:10248/healthz"

RECOVERY_TIMEOUT = 5
RECOVERY_TRIES = 30
RECOVERY_TRY_SLEEP = 5


class TimeoutException(Exception):
    pass


@contextmanager
def time_limit(seconds):
    """Auxiliary function to limit execution time of a block of code."""
    def signal_handler(signum, frame):
        raise TimeoutException("TIMEOUT")
    signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)


def _exec_cmd(cmd, stdout=None, stderr=None):
    """Auxiliary function to executes CLI commands.
    Return:
     - rc = 0, command was executed successfully.
     - rc = returncode, command failed.
    """
    rc = 0
    kwargs = {}
    if stdout is not None:
        kwargs["stdout"] = stdout
    if stderr is not None:
        kwargs["stderr"] = stderr
    try:
        subprocess.check_call(cmd, **kwargs)
    except CalledProcessError as e:
        LOG.error("[return code: %s] %s", e.returncode, e)
        rc = e.returncode
    return rc


def _log_file_content(log_file):
    """Auxiliary function to log the content of file."""
    try:
        with open(log_file, 'r') as file:
            LOG.error(file.read())
    except Exception:
        pass


def update_k8s_control_plane_components(config_filename,
                                        target_component='apiserver'):
    """The function updates a k8s control-plane component."""
    LOG.debug('Updating %s ...', target_component)
    cmd = ["kubeadm", "init", "phase", "control-plane",
           target_component, "--config", config_filename]
    rc = _exec_cmd(cmd)
    return rc


def update_k8s_kubelet(config_filename, error_log_file):
    """The function updates k8s kubelet.
    Return:
     - rc = 0, update process successful.
     - rc = 1, update process failed.
    """
    LOG.debug('Applying new configuration to Kubelet ...')
    try:
        if os.path.isfile(error_log_file):
            os.remove(error_log_file)

        with open(error_log_file, "w") as err_file:
            cmd = ["kubeadm", "init", "phase", "kubelet-start", "--config",
                   config_filename]
            rc = _exec_cmd(cmd, stderr=err_file)

        # When an invalid kubelet parameter name is added to the
        # KubeletConfiguration, the kubeadm validation process prints an error
        # message and ignores the parameter, then the remaining parameters are
        # applied and the process returns '0' (successful return code) if no
        # other error occurs. Therefore, we need to look for an error message
        # to ensure that the applied settings match the settings set by the
        # user.
        if rc == 0 and os.stat(error_log_file).st_size == 0:
            return 0

        # include error_log_file into LOG
        _log_file_content(error_log_file)
        return 1

    except Exception as e:
        LOG.error(e)
        # include error_log_file into LOG
        _log_file_content(error_log_file)
        return 1


def patch_k8s_kubeadm_configmap(configmap_filename):
    """The function patches the kubeadm-config configmap."""
    LOG.debug('Patching k8s kubeadm configmap.')
    cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "-n", "kube-system",
           "patch", "configmap", "kubeadm-config", "--patch-file", configmap_filename]
    rc = _exec_cmd(cmd)
    return rc


def export_k8s_cluster_configuration(target_filename):
    """The function extracts from k8s kubeadm-config configmap the
    cluster configuration section and save it to a file.
    Return:
     - rc = 0, export process successful.
     - rc = returncode or 1, export process failed.
    """
    LOG.debug('Exporting k8s cluster configuration.')
    try:
        with open(target_filename, "w") as f:
            cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
                   "get", "cm", "-n", "kube-system", "kubeadm-config",
                   "-o=jsonpath={.data.ClusterConfiguration}"]
            return _exec_cmd(cmd, stdout=f)
    except Exception as e:
        LOG.error(e)
        return 1


def _export_k8s_configmap(target_filename, cmd):
    """The function exports a k8s configmap to a file.
    Return:
     - rc = 0, export process successful.
     - rc = returncode or 1, export process failed.
    """
    try:
        with open(target_filename, "w") as f:
            return _exec_cmd(cmd, stdout=f)
    except Exception as e:
        LOG.error(e)
        return 1


def export_k8s_kubeadm_configmap(target_filename):
    """The function exports k8s kubeadm-config configmap to a file.
    Return:
     - rc = 0, export process successful.
     - rc = returncode or 1, export process failed.
    """
    LOG.debug('Exporting k8s kubeadm configmap.')
    cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "get",
           "configmap", "kubeadm-config", "-o=yaml", "-n", "kube-system"]
    return _export_k8s_configmap(target_filename, cmd)


def export_configmap_from_volume(volume_dict, section):
    """The function exports a configmap data to a file.
    Return:
     - rc = 0, export process successful.
     - rc = returncode or 1, export process failed.
    """
    # only export configmap in case of volume with 'File' type
    if volume_dict['pathType'] != 'File':
        return 0
    _vol = volume_dict.copy()
    _vol['section'] = section
    configmap_name = sp.get_k8s_configmap_name(_vol)
    target_filename = volume_dict['hostPath']

    LOG.debug("Exporting k8s configmap '%s'.", configmap_name)
    cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "get",
           "cm", "-n", "kube-system", configmap_name, "-o=jsonpath={.data.*}"]
    return _export_k8s_configmap(target_filename, cmd)


def k8s_health_check(timeout, tries, try_sleep, healthz_endpoint):
    """The function checks a k8s control-plane component health.
    It uses the health endpoints provided by the control-plane pods.
    Return:
     - rc = True, k8s component health check ok.
     - rc = False, k8s component health check failed.
    """
    # pylint: disable-msg=broad-except
    rc = False
    _tries = tries

    valid_endpoints = {
        APISERVER_READYZ_ENDPOINT: 'apiserver',
        SCHEDULER_HEALTHZ_ENDPOINT: 'scheduler',
        CONTROLLER_MANAGER_HEALTHZ_ENDPOINT: 'controller_manager',
        KUBELET_HEALTHZ_ENDPOINT: 'kubelet'}

    if healthz_endpoint not in valid_endpoints:
        msg = "Invalid endpoint: {}".format(healthz_endpoint)
        LOG.error(msg)
        return rc
    endpoint_name = valid_endpoints.get(healthz_endpoint)

    while _tries:
        time.sleep(try_sleep)
        msg = "Checking {} healthz (Remaining tries: {}".format(endpoint_name, _tries)
        LOG.debug(msg)

        try:
            with time_limit(timeout):
                try:
                    kwargs = {"verify": False, "timeout": 15}
                    r = requests.get(healthz_endpoint, **kwargs)
                    if r.status_code == 200:
                        rc = True
                        break
                except Exception:
                    rc = False
        except TimeoutException:
            LOG.error('Timeout while checking k8s control-plane component health')
            rc = False
        _tries -= 1
    return rc


def merge_configmap_files(lastest_configmap_file, bak_configmap_file,
                          new_configmap_file):
    """This function merges two kubeadmin-config configmap files and generates
    a new one as result. The first configmap is taken as reference and the
    cluster config section is replaced using the info of the second configmap.
    """
    # To patch the kubeadm-config configmap is neccesary to
    # start the mods from the last saved configmap (it is saved with a
    # version number), so we will take as source the last saved
    # configmap and we will replace in it only the cluster config section taken
    # from the backup kubeadm-config configmap.
    LOG.debug('Merging configmap files.')
    try:
        with open(lastest_configmap_file, 'r') as file:
            lastest_configmap = yaml.load(file, Loader=yaml.RoundTripLoader)

        with open(bak_configmap_file, 'r') as file:
            bak_configmap = yaml.load(file, Loader=yaml.RoundTripLoader)
            bak_cluster_config = yaml.load(
                bak_configmap['data']['ClusterConfiguration'],
                Loader=yaml.RoundTripLoader)
    except Exception as e:
        LOG.error('ERROR loading configmap file. %s ', e)
        raise

    cluster_cfg_str = yaml.dump(
        bak_cluster_config, Dumper=yaml.RoundTripDumper,
        default_flow_style=False)
    # ensure the yaml is constructed with proper formatting and tabbing
    cluster_cfg_str = yaml.scalarstring.PreservedScalarString(cluster_cfg_str)

    lastest_configmap['data']['ClusterConfiguration'] = cluster_cfg_str

    try:
        with open(new_configmap_file, 'w') as file:
            yaml.dump(lastest_configmap, file, Dumper=yaml.RoundTripDumper,
                      default_flow_style=False)
    except Exception as e:
        LOG.error('ERROR saving configmap file. %s', e)
        raise


def pre_k8s_updating_tasks(post_tasks=None):
    """The function execute a group of tasks that are needed before the
    k8s cluster is updated.
    Args:
        post_tasks: is anarray that contains callable object to be ejecuted
        in post_k8s_updating_tasks method
    Return:
     - rc = 0, task completed successful.
     - rc = 1, failed task.
    """
    # pylint: disable-msg=broad-except
    rc = 0
    LOG.debug('Running mandatory tasks before update proccess start.')
    try:
        with open(KUBE_APISERVER_CONFIG) as f:
            lines = f.read()
    except Exception as e:
        LOG.error('Loading kube_apiserver config [Detail %s].', e)
        return 1

    m = re.search(REGEXPR_ADVERTISE_ADDRESS, lines)
    if m:
        advertise_address = m.group(1)
        LOG.debug('  advertise_address = %s', advertise_address)

    def _post_task_update_advertise_address():
        """This method will be executed in right after control plane has been initialized and it
        will update advertise_address in manifests/kube-apiserver.yaml to use mgmt address
        instead of oam address due to https://bugs.launchpad.net/starlingx/+bug/1900153
        """
        default_network_interface = None

        with open(KUBE_APISERVER_CONFIG) as f:
            lines = f.read()
        m = re.search(REGEXPR_ADVERTISE_ADDRESS, lines)
        if m:
            default_network_interface = m.group(1)
            LOG.debug('  default_network_interface = %s', default_network_interface)

        if advertise_address and default_network_interface \
           and advertise_address != default_network_interface:
            cmd = ["sed", "-i", "/oidc-issuer-url/! s/{}/{}/g".format(default_network_interface, advertise_address),
                   KUBE_APISERVER_CONFIG]
            _ = _exec_cmd(cmd)

    def _post_task_security_context():
        cmd = ["sed", "-i", "/securityContext:/,/type: RuntimeDefault/d", KUBE_APISERVER_CONFIG]
        _ = _exec_cmd(cmd)

    post_tasks.append(_post_task_update_advertise_address)
    post_tasks.append(_post_task_security_context)

    return rc


def post_k8s_updating_tasks(post_tasks=None):
    """The function executes tasks that are needed after the
    k8s cluster is updated.
    """
    if post_tasks:
        for task in post_tasks:
            if callable(task):
                task()
    LOG.debug('Running mandatory tasks after updating proccess has finished.')


def restore_k8s_control_plane_config(kubeadm_cm_bak_file, cluster_config_bak_file,
                                     configmap_patched_file, **kwargs):
    """The function restores the k8s control-plane configuration and updates the kubeadm
    configmap with the backup configuration to keep it sync.
    Return:
     - 1, Backup configuration has been restored successfully.
     - 2, Restore process has failed.
    """
    # pylint: disable-msg=too-many-return-statements
    LOG.debug('Initializing control-plane restore')

    configmap_latest_file = kwargs.get(
        'configmap_latest_file', '/tmp/cluster_configmap_latest.yaml')
    tries = kwargs.get('tries')
    try_sleep = kwargs.get('try_sleep')
    timeout = kwargs.get('timeout')

    # -------------------------------------------------------------------------
    # Restore kube-apiserver with backup configuration
    # -------------------------------------------------------------------------
    # First we need to restore apiserver with saved cluster_configuration
    update_k8s_control_plane_components(
        cluster_config_bak_file, target_component='apiserver')

    # Run mandatory tasks after the update proccess has finished
    post_k8s_updating_tasks(post_k8s_tasks)

    # It is necessary to restart kubelet because when a wrong configuration is
    # set in any of the k8s control plane components, kubelet
    # attempts to restart the erroneous component a maximum of 5 times. If we
    # reach the limit, no matter if we correct the configuration during
    # automatic recovery, the container does not start again.
    if restart_kubelet_service() != 0:
        return 2

    # Wait for kube-apiserver to be up before executing next steps
    k8s_apiserver_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=APISERVER_READYZ_ENDPOINT)
    if not k8s_apiserver_healthy:
        return 2

    # Restore controller_manager
    update_k8s_control_plane_components(
        cluster_config_bak_file, target_component='controller-manager')

    if restart_kubelet_service() != 0:
        return 2

    k8s_component_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=CONTROLLER_MANAGER_HEALTHZ_ENDPOINT)
    if not k8s_component_healthy:
        return 2

    # Restore scheduler
    update_k8s_control_plane_components(
        cluster_config_bak_file, target_component='scheduler')

    if restart_kubelet_service() != 0:
        return 2

    k8s_component_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=SCHEDULER_HEALTHZ_ENDPOINT)
    if not k8s_component_healthy:
        return 2

    # Patch kubeadm configmap to keep it consistent with the applied config.
    LOG.debug('k8s control-plane is healthy: initializing configmap patching.')
    export_k8s_kubeadm_configmap(configmap_latest_file)

    merge_configmap_files(configmap_latest_file, kubeadm_cm_bak_file,
                          configmap_patched_file)

    patch_k8s_kubeadm_configmap(configmap_patched_file)

    LOG.debug("Automatic k8s control-plane recovery completed successfully.")
    return 1


def restore_k8s_kubelet_config(config_bak_file, **kwargs):
    """The function restores the k8s kubelet configuration and updates the
    kubelet configmap with the backup configuration to keep it sync.
    Return:
     - 1, Backup configuration has been restored successfully.
     - 2, Restore process has failed.
    """
    tries = kwargs.get('tries')
    try_sleep = kwargs.get('try_sleep')
    timeout = kwargs.get('timeout')
    error_log_file = kwargs.get('error_log_file')

    # Restore kubelet from backup configuration
    if update_k8s_kubelet(config_bak_file, error_log_file=error_log_file) != 0:
        is_k8s_kubelet_healthy = False
    else:
        LOG.debug('Waiting for kubelet be online.')
        is_k8s_kubelet_healthy = k8s_health_check(
            timeout=timeout, try_sleep=try_sleep, tries=tries,
            healthz_endpoint=KUBELET_HEALTHZ_ENDPOINT)
    if not is_k8s_kubelet_healthy:
        LOG.error("Automatic Kubelet recovery failed.")
        return 2

    LOG.debug("Automatic Kubelet recovery completed successfully.")
    return 1


def generates_kubeadm_config_file(
        kubeadm_config_file, kubelet_bak_config_file,
        new_kubelet_cfg=None, cluster_cfg=None,
        only_kubelet_section=False):
    """The function generates a valid kubeadm config file
    The kubeadmin config file must contain ClusterConfiguration and
    KubeletConfiguration sections.
    * ClusterConfiguration could be empty
    * KubeletConfiguration section is built from service-parameters kubelet section.
    For example:
    kind: ClusterConfiguration
    apiVersion: kubeadm.k8s.io/v1beta2
    kubernetesVersion: v1.23.1
    ---
    kind: KubeletConfiguration
    apiVersion: kubelet.config.k8s.io/v1beta1
    <parameters ...>

    Return:
     - rc = 0, kubeadm config file generated successful.
     - rc = 1, kubeadm config file generation failed.
    """
    LOG.debug('Generating new kubeadm config file ...')
    if not only_kubelet_section:
        if cluster_cfg is None:
            try:
                _cluster_cm_aux = get_k8s_configmap(
                    'kubeadm-config', namespace='kube-system')
                cluster_cfg = yaml.load(
                    _cluster_cm_aux.data['ClusterConfiguration'],
                    Loader=yaml.RoundTripLoader)
            except Exception as e:
                LOG.error('Getting cluster-config configmap: %s', e)
                return 1
        # Initialize kubeadm config file with ClusterConfiguration
        base_cluster_cfg = {
            'kind': 'ClusterConfiguration',
            'apiVersion': cluster_cfg['apiVersion'],
            'kubernetesVersion': cluster_cfg['kubernetesVersion']}
        try:
            with open(kubeadm_config_file, 'w') as file:
                for key, value in base_cluster_cfg.items():
                    file.write(f"{key}: {value}\n")
                file.write("---\n")
        except Exception as e:
            LOG.error('Initializing kubeadm config file with '
                      'ClusterConfiguration: %s', e)
            return 1

    # Intialize KubeletConfiguration
    try:
        with open(kubelet_bak_config_file, 'r') as file:
            bak_kubelet_cfg = yaml.load(file, Loader=yaml.RoundTripLoader)
    except FileNotFoundError:
        LOG.error('Kubelet bak config file not found.')
        return 1
    except Exception as e:
        LOG.error('Loading kubelet config from kubelet_bak_config_file: %s', e)
        return 1

    # Updating KubeletConfiguration
    if new_kubelet_cfg is not None:
        # the new kubelet configuration is provided from the service parameter
        # entries. Since 'kind' and 'apiVersion' parameters are not stored as
        # service-parameters, both are taken from the backup kubelet
        # configuration file
        _kubelet_cfg = {
            'kind': 'KubeletConfiguration',
            'apiVersion': bak_kubelet_cfg['apiVersion']}
        _kubelet_cfg.update(new_kubelet_cfg)
    else:
        _kubelet_cfg = bak_kubelet_cfg

    # Updating kubeadm config file
    try:
        with open(kubeadm_config_file, 'a') as file:
            yaml.dump(_kubelet_cfg, file, Dumper=yaml.RoundTripDumper,
                      default_flow_style=False)
        return 0
    except Exception as e:
        LOG.error('Updating kubeadm config file with KubeletConfiguration: %s', e)
        return 1


def get_service_parameters_from_hieradata(
        hieradata_file, apiserver_schema, controller_manager_schema,
        scheduler_schema, kubelet_schema, etcd_schema):
    """The function gets the k8s service parameters from hieradata.

    Also apiserver, controller_manager, scheduler and etc schemas are used to
    translate from legacy to k8s valid names.

    Return:
     - service_params : dict
       Dictionary with k8s service parameters.
    """
    # pylint: disable-msg=too-many-arguments
    # pylint: disable-msg=too-many-branches
    try:
        with open(hieradata_file, 'r') as _hieradata:
            hieradata = yaml.load(_hieradata, Loader=yaml.Loader)
    except Exception as e:
        LOG.error('ERROR loading hieradata. %s', e)
        raise

    service_params = {'apiServer': {}, 'controllerManager': {},
                      'scheduler': {}, 'etcd': {},
                      'config': {}, 'kubelet': {},
                      'apiServerVolumes': {}, 'controllerManagerVolumes': {},
                      'schedulerVolumes': {}, 'kubeletVolumes': {}}

    for param_key, value in hieradata.items():
        if param_key.startswith(KUBE_APISERVER_TAG):
            param_name = param_key.split(KUBE_APISERVER_TAG)[1]
            # translate from legacy to valid k8s format
            for sect in apiserver_schema.keys():
                if param_name in apiserver_schema[sect].keys():
                    param_name = apiserver_schema[sect][param_name]
            service_params['apiServer'][param_name] = value

        elif param_key.startswith(KUBE_APISERVER_VOLUMES_TAG):
            param_name = param_key.split(KUBE_APISERVER_VOLUMES_TAG)[1]
            service_params['apiServerVolumes'][param_name] = value

        elif param_key.startswith(CONTROLLER_MANAGER_TAG):
            param_name = param_key.split(CONTROLLER_MANAGER_TAG)[1]
            # translate from legacy to valid k8s format
            for sect in controller_manager_schema.keys():
                if param_name in controller_manager_schema[sect].keys():
                    param_name = controller_manager_schema[sect][param_name]
            service_params['controllerManager'][param_name] = value

        elif param_key.startswith(CONTROLLER_MANAGER_VOLUMES_TAG):
            param_name = param_key.split(CONTROLLER_MANAGER_VOLUMES_TAG)[1]
            service_params['controllerManagerVolumes'][param_name] = value

        elif param_key.startswith(SCHEDULER_TAG):
            param_name = param_key.split(SCHEDULER_TAG)[1]
            # translate from legacy to valid k8s format
            for sect in scheduler_schema.keys():
                if param_name in scheduler_schema[sect].keys():
                    param_name = scheduler_schema[sect][param_name]
            service_params['scheduler'][param_name] = value

        elif param_key.startswith(SCHEDULER_VOLUMES_TAG):
            param_name = param_key.split(SCHEDULER_VOLUMES_TAG)[1]
            service_params['schedulerVolumes'][param_name] = value

        elif param_key.startswith(ETCD_TAG):
            param_name = param_key.split(DEFAULT_TAG)[1]
            # translate from legacy to valid k8s format
            for sect in etcd_schema.keys():
                if param_name in etcd_schema[sect].keys():
                    param_name = etcd_schema[sect][param_name]
            service_params['etcd'][param_name] = value

        elif param_key.startswith(CONFIG_TAG):
            param_name = param_key.split(CONFIG_TAG)[1]
            service_params['config'][param_name] = value

        elif param_key.startswith(KUBELET_TAG):
            param_name = param_key.split(KUBELET_TAG)[1]
            # translate from legacy to valid k8s format
            for sect in kubelet_schema.keys():
                if param_name in kubelet_schema[sect].keys():
                    param_name = kubelet_schema[sect][param_name]
            service_params['kubelet'][param_name] = value

    return service_params


def get_kubelet_cfg_from_service_parameters(service_params):
    """Building kubelet_cfg from service-parameters (hieradata) dict
    Due parameters and values are loaded from hieradata, the value must be
    casted to the expected format of KubeletConfiguration API.

    Supported Types Kubelet Configuration (v1beta1):
    * string: no cast required.
    * []string: no cast required.
    * map[string]string:
      - The values must be in json format.
      - Cast to python dict type.
    * int32, int64: cast to python int type.
    * float: cast to python float type.

    Return:
     - kubelet_cfg : dict
       Dictionary with kubelet configuration parameters.
    """
    kubelet_cfg = {}
    for param, value in service_params['kubelet'].items():
        # map[string]string
        if value.startswith('{') and value.endswith('}'):
            try:
                value = json.loads(value.replace('True', 'true').replace('False', 'false').replace("'", '"'))
            except Exception as e:
                LOG.error('Parsing kubelet value: %s', e)
                return 3
        # bool
        elif value in ['False', 'false'] or value in ['True', 'true']:
            value = True if value in ['True', 'true'] else False  # pylint: disable-msg=simplifiable-if-expression
        # float
        elif '.' in value:
            try:
                value = float(value)
            except Exception:
                pass
        # int32, int64
        else:
            try:
                value = int(value)
            except Exception:
                pass

        kubelet_cfg[param] = value
    return kubelet_cfg


def get_k8s_version(timeout=None, tries=None, try_sleep=None):
    """The function gets the k8s version from kubeadm-config configmap.
    Return:
     - k8s_version : str or False.
       str: returning k8s_version value.
       False: k8s version get process failed.
    """
    timeout = RECOVERY_TIMEOUT if timeout is None else timeout
    tries = RECOVERY_TRIES if tries is None else tries
    try_sleep = RECOVERY_TRY_SLEEP if try_sleep is None else try_sleep
    try:
        return kube_operator.kube_get_kubernetes_version()
    except Exception:
        _tries = tries
        LOG.debug('Retrying to get k8s version ...')
        while _tries:
            time.sleep(try_sleep)
            try:
                with time_limit(timeout):
                    try:
                        return kube_operator.kube_get_kubernetes_version()
                    except Exception:
                        pass
            except TimeoutException:
                pass
            _tries -= 1
            LOG.debug("Remaining tries: %s.", _tries)
        LOG.error('Getting k8s version.')
        return False


def get_k8s_configmap(configmap, namespace='kube-system',
                      timeout=None, tries=None, try_sleep=None):
    """The function gets a configmap from k8s API.
    Return:
     - k8s configmap : str or False.
       str: returning k8s configmap.
       False: k8s version get process failed.
    """
    timeout = RECOVERY_TIMEOUT if timeout is None else timeout
    tries = RECOVERY_TRIES if tries is None else tries
    try_sleep = RECOVERY_TRY_SLEEP if try_sleep is None else try_sleep
    try:
        return kube_operator.kube_read_config_map(
            name=configmap, namespace=namespace)
    except Exception:
        _tries = tries
        LOG.debug('Retrying to get k8s configmap %s', configmap)
        while _tries:
            time.sleep(try_sleep)
            try:
                with time_limit(timeout):
                    try:
                        k8s_configmap = kube_operator.kube_read_config_map(
                            name=configmap, namespace=namespace)
                        return k8s_configmap
                    except Exception:
                        pass
            except TimeoutException:
                pass
            _tries -= 1
            LOG.debug("Remaining tries: %s.", _tries)
        LOG.error('Getting k8s configmap %s', configmap)
        return False


def update_kubelet_configmap(latest_config):
    """The function updates the k8s configmap for kubelet component.
    Return:
     - rc = 0, update process successful.
     - rc = 1, update process failed.
    """
    LOG.debug('Updating kubelet configmap')

    namespace = 'kube-system'
    k8s_version = get_k8s_version()
    if not k8s_version:
        return 1
    k8s_version = '.'.join(k8s_version.replace('v', '').split('.')[:2])
    configmap_name = 'kubelet-config-' + k8s_version

    # delete current kubelet configmap
    try:
        current_kubelet_configmap = get_k8s_configmap(
            configmap_name, namespace=namespace)
        if current_kubelet_configmap:
            kube_operator.kube_delete_config_map(
                name=configmap_name, namespace=namespace)
    except Exception as e:
        LOG.error('Deleting current kubelet confimap: %s', e)
        return 1

    # create new kubelet configmap from latest applied config
    try:
        kube_operator.kube_create_config_map_from_file(
            namespace, configmap_name, latest_config,
            data_section_name='kubelet')
    except Exception as e:
        LOG.error('Creating new kubelet confimap: %s', e)
        return 1

    return 0


def update_kubelet_bak_config_files(
        kubeadm_kubelet_config_file, kubeadm_kubelet_config_bak_file,
        kubelet_latest_config_file, kubelet_bak_config_file):
    """The function updates the k8s configmap for kubelet component.
    Return:
     - rc = 0, update process successful.
     - rc = 1, update process failed.
    """
    LOG.debug("Updating kubelet backup config files.")
    try:
        shutil.copyfile(kubeadm_kubelet_config_file, kubeadm_kubelet_config_bak_file)
    except Exception as e:
        LOG.error('Updating kubeadm with kubelet bak config file. %s', e)
        return 1
    try:
        shutil.copyfile(kubelet_latest_config_file, kubelet_bak_config_file)
    except Exception as e:
        LOG.error('Updating kubelet bak config file. %s', e)
        return 1
    return 0


def restart_kubelet_service():
    """Restart Kubelet Service
    Return:
     - rc = 0, restart process successful.
     - rc = 1, restart process failed.
    """
    LOG.debug('Restarting Kubelet')
    cmd = ["systemctl", "restart", "kubelet.service"]
    if _exec_cmd(cmd) != 0:
        return 1
    return 0


def _validate_admission_plugins(custom_plugins):
    """The function complements the plugins set by user with those required by
    the system.
    """
    # There are some plugins required by the system
    # if the plugins is specified manually, these ones might
    # be missed. We will add these automatically so the user
    # does not need to keep track of them
    required_plugins = ['NodeRestriction']
    for plugin in required_plugins:
        if plugin not in custom_plugins:
            custom_plugins = custom_plugins + "," + plugin
    return custom_plugins


def initialize_k8s_configmaps(
        hieradata_file, k8s_configmaps_init_flag,
        apiserver_schema, controller_manager_schema,
        scheduler_schema, kubelet_schema, etcd_schema):
    """The function ensures the k8s configmap exists for all the
    extra-volumes service parameters.
    """
    # pylint: disable-msg=too-many-locals
    # pylint: disable-msg=too-many-arguments
    # pylint: disable-msg=logging-not-lazy

    sysinv_k8s_sections = {
        'apiServerVolumes': 'kube-apiserver-volumes',
        'controllerManagerVolumes': 'kube-controller-manager-volumes',
        'schedulerVolumes': 'kube-scheduler-volumes'}

    # Load k8s service-parameters from hieradata
    service_params = get_service_parameters_from_hieradata(
        hieradata_file, apiserver_schema, controller_manager_schema,
        scheduler_schema, kubelet_schema, etcd_schema)

    for kubeadm_section in sysinv_k8s_sections:
        for param_name, value in service_params[kubeadm_section].items():
            volume, _ = sp.parse_volume_string_to_dict({'name': param_name, 'value': value})

            # only create configmaps for 'File' type
            # 'DirectoryorCreate' type has no associated configmaps
            pathType = volume.get('pathType')
            if pathType != 'File':
                LOG.debug('Directory, skipping: %s' % (param_name))
                continue

            mounthPath = volume.get('mounthPath')
            # hostPath is an optional value in 22.06
            hostPath = volume.get('hostPath', mounthPath)
            volume['section'] = sysinv_k8s_sections.get(kubeadm_section)
            configmap_name = sp.get_k8s_configmap_name(volume)

            # verify if configmap exists
            LOG.debug('Checking if configmap exists [%s].' % (configmap_name))
            try:
                cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
                       "get", "configmap", "-n", "kube-system", configmap_name]
                configmap_exists = subprocess.check_output(cmd)
                if configmap_exists:
                    LOG.debug('Configmap exists, skipping.')
                    continue
            except Exception:
                pass

            # verifying configuration file
            if not os.path.isfile(hostPath):
                msg = ("File not found: %s" % (hostPath))
                LOG.error(msg)
                raise ValueError(msg)

            # Updating kubeadm config file
            try:
                with open(hostPath, 'r'):
                    pass
            except Exception as e:
                LOG.error('Loading config file: %s. %s' % (hostPath, e))
                raise

            # create configmap
            LOG.debug('Creating configmap ...')
            try:
                cmd = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
                       "create", "configmap", "-n", "kube-system",
                       configmap_name, "--from-file", hostPath]
                _ = subprocess.check_output(cmd)
            except Exception as exc:
                LOG.error('Creating configmap: %s' % (exc))
                raise

            # create completed flag
            try:
                cmd = ["touch", k8s_configmaps_init_flag]
                _ = subprocess.check_output(cmd)
            except Exception as exc:
                LOG.error('Creating k8s configmaps initialization flag: %s' % (exc))
                raise


def main():
    """This script updates the k8s control-plane components configuration
    with the paramaters set by the user through sysinv service-parameters.
    If a failure is detected during the update process a full restore is
    applied using the latest valid configuration.

    Sections
    ---------
    The service-parameter 'kubernetes' service sections are:
    - 'kube_apiserver'
    - 'kube_apiserver_volumes'
    - 'kube_controllerManager',
    - 'kube_controller_manager_volumes',
    - 'kube_scheduler'
    - 'kube_scheduler_volumes'
    - 'kubelet'
    for the respective control-plane components.
    The user can add, modify or delete the parameters of k8s control-plane
    components under these sections.

    Field Names:
    ------------
    service-parameter fields should be named following the k8s nomenclature,
    used in kubeadm.conf file. Currently there are some parameters that are
    defined with name fields that not match the names expected by k8s components
    APIs. The apiserver_schema, scheduler_schema, controller_manager_schema and
    etc_schema are used to rebuild the structure of that sections and to
    translate the fields that not match k8s expected names.
    i.e.: service-parameters accept "admission_plugins" but the name expected by
    k8s for this field is "enabled-admission-plugins", so a translation is
    needed.

    Recovery:
    ---------
    - Automatic Recovery
    After an update process a monitor is activated to check kube-apiserver
    health. If something goes wrong and kube-apiserver go out of service a
    recovery process is initiated to restore it. This function is activated
    by default. The user also can set a flag to deactivate this recovery process
    only for debugging purpose. Also is possible to set timeout, tries and
    try_sleep of k8s health check.
    Those variables must be defined in the 'config' section of 'platform'
    service throught service-parameters:
      automatic_recovery: true|false
      timeout: <seconds>
      tries: <number>
      try_sleep: <seconds>

    Steps:
      - Read the new configuration from puppet files (hieradata).
      - Prepare the ClusterConfiguration to update control-plane components.
      - Execute some task before updating control-plane components.
      - Update control-plane configuration.
      - Execute some task after updating control-plane components.
      - Check k8s control-plane components healthz after the update process.
      - Update kubelet configuration.
      - Check kubelet healthz after the update process.
      - Trigger restore configuration from backup if the update process failed.
      - Update backup files if the update process finished successfully.

    Returns:
     - rc = 0, K8s control-plane components has been updated successfully.
     - rc = 1, The updating process failed but backup configuration has been applied.
               Sysinv won't clear the alarm 250.001 - Configuration is out-of-date.
     - rc = 2, The updating process failed. One ore more control-plane
               components or kubelet could be down.
     - rc = 3, The updating process failed.
    """
    # pylint: disable-msg=too-many-locals
    # pylint: disable-msg=too-many-branches
    # pylint: disable-msg=too-many-statements
    # pylint: disable-msg=too-many-return-statements
    # pylint: disable-msg=broad-except

    # Components Schemas
    # The 'kubernetes' service in service-parameter has a section per k8s
    # component to manage its configurations. Only the 'extraVolumes' parameters
    # are saved in a different section (check available sections in module
    # description)
    # The kubeadm command (used to update components), however, expects a
    # configuration file with a different structure per component. Each component
    # has also different sections, for example: root, extraArgs, etc.
    # Therefore, these schemas are created to map the (sysinv) service parameters
    # kubernetes sections to the expected structure.
    apiserver_schema = {
        'root': {
            'timeoutForControlPlane': 'timeoutForControlPlane'
        },
        'extraArgs': {
            'oidc_issuer_url': 'oidc-issuer-url',
            'oidc_client_id': 'oidc-client-id',
            'oidc_username_claim': 'oidc-username-claim',
            'oidc_groups_claim': 'oidc-groups-claim',
            'admission_plugins': 'enable-admission-plugins',
        },
        'extraVolumes': {},
    }

    controller_manager_schema = {
        'root': {},
    }

    scheduler_schema = {
        'root': {},
    }

    kubelet_schema = {
        'root': {},
    }

    etcd_schema = {
        'root': {},
        'external': {
            'etcd_cafile': 'caFile',
            'etcd_certfile': 'certFile',
            'etcd_keyfile': 'keyFile',
            'etcd_servers': 'endpoints'
        }
    }

    # Args Parameters
    parser = argparse.ArgumentParser()
    parser.add_argument("--hieradata_path", default="/tmp/puppet/hieradata")
    parser.add_argument("--hieradata_file", default="system.yaml")
    parser.add_argument("--backup_path", default="/etc/kubernetes/backup")
    parser.add_argument("--kubeadm_cm_file", default="/tmp/cluster_configmap.yaml")
    parser.add_argument("--kubeadm_cm_bak_file", default="configmap.yaml")
    parser.add_argument("--configmap_patched_file",
                        default="/tmp/cluster_configmap_patched.yaml")
    parser.add_argument("--cluster_config_file", default="/tmp/cluster_config.yaml")
    parser.add_argument("--cluster_config_bak_file", default="cluster_config.yaml")
    parser.add_argument("--kubeadm_kubelet_config_file", default="/tmp/kubeadm_kubelet_config.yaml")
    parser.add_argument("--kubeadm_kubelet_config_bak_file",
                        default="/etc/kubernetes/backup/kubeadm_kubelet_config.yaml")
    parser.add_argument("--kubelet_latest_config_file", default="/var/lib/kubelet/config.yaml")
    parser.add_argument("--kubelet_bak_config_file", default="/var/lib/kubelet/config.yaml.bak")
    parser.add_argument("--kubelet_error_log", default="/tmp/kubelet_errors.log")
    parser.add_argument("--k8s_configmaps_init_flag", default="/tmp/.sysinv_k8s_configmaps_initialized")

    parser.add_argument("--automatic_recovery", default=True)
    parser.add_argument("--timeout", default=RECOVERY_TIMEOUT)
    parser.add_argument("--tries", default=RECOVERY_TRIES)
    parser.add_argument("--try_sleep", default=RECOVERY_TRY_SLEEP)

    parser.add_argument("--etcd_cafile", default='')
    parser.add_argument("--etcd_certfile", default='')
    parser.add_argument("--etcd_keyfile", default='')
    parser.add_argument("--etcd_servers", default='')
    args = parser.parse_args()

    hieradata_file = os.path.join(args.hieradata_path, args.hieradata_file)
    kubeadm_cm_file = args.kubeadm_cm_file
    kubeadm_cm_bak_file = os.path.join(args.backup_path, args.kubeadm_cm_bak_file)
    cluster_config_file = args.cluster_config_file
    cluster_config_bak_file = os.path.join(args.backup_path, args.cluster_config_bak_file)
    configmap_patched_file = args.configmap_patched_file

    kubeadm_kubelet_config_file = args.kubeadm_kubelet_config_file
    kubeadm_kubelet_config_bak_file = args.kubeadm_kubelet_config_bak_file
    kubelet_latest_config_file = args.kubelet_latest_config_file
    kubelet_bak_config_file = args.kubelet_bak_config_file
    kubelet_error_log = args.kubelet_error_log
    k8s_configmaps_init_flag = args.k8s_configmaps_init_flag

    automatic_recovery = args.automatic_recovery
    timeout = args.timeout
    tries = args.tries
    try_sleep = args.try_sleep

    etcd_cafile = args.etcd_cafile
    etcd_certfile = args.etcd_certfile
    etcd_keyfile = args.etcd_keyfile
    etcd_servers = args.etcd_servers

    rc = 2

    # -----------------------------------------------------------------------------
    # Backup k8s cluster and kubelet configuration
    # -----------------------------------------------------------------------------
    # This flag will avoid any error when you try to run this script manually
    # and kube-apiserver is down.
    is_k8s_apiserver_up = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=APISERVER_READYZ_ENDPOINT)

    # K8s control-plane backup config files
    if not os.path.isfile(kubeadm_cm_bak_file) or\
            not os.path.isfile(cluster_config_bak_file):
        LOG.debug("No backup files founded for K8s control-plane components.")
        if is_k8s_apiserver_up:
            LOG.debug("Creating backup from current k8s config.")
            export_k8s_kubeadm_configmap(kubeadm_cm_bak_file)
            export_k8s_cluster_configuration(cluster_config_bak_file)
        else:
            msg = "Apiserver is down and there is not backup file."
            LOG.error(msg)
            return 2

    # Kubeadm with Kubelet backup config file
    if not os.path.isfile(kubeadm_kubelet_config_bak_file):
        LOG.debug("No backup file founded for Kubelet.")
        try:
            shutil.copyfile(kubelet_latest_config_file, kubelet_bak_config_file)
        except Exception as e:
            LOG.error('Creating kubelet bak config file. %s', e)
            return 3
        if generates_kubeadm_config_file(
                kubeadm_config_file=kubeadm_kubelet_config_bak_file,
                kubelet_bak_config_file=kubelet_bak_config_file) != 0:
            return 3

    # -----------------------------------------------------------------------------
    # Initialize k8s configmaps
    # -----------------------------------------------------------------------------
    if not os.path.isfile(k8s_configmaps_init_flag):
        initialize_k8s_configmaps(
            hieradata_file, k8s_configmaps_init_flag,
            apiserver_schema, controller_manager_schema,
            scheduler_schema, kubelet_schema, etcd_schema)

    # -----------------------------------------------------------------------------
    # Load current applied k8s cluster configuration
    # -----------------------------------------------------------------------------
    LOG.debug('Exporting current config to file.')
    if export_k8s_kubeadm_configmap(kubeadm_cm_file) != 0:
        LOG.debug("k8s is not running, copy configmap backup file")
        cmd = ["cp", kubeadm_cm_bak_file, kubeadm_cm_file]

        if _exec_cmd(cmd) != 0:
            msg = "Fail copying configmap backup file."
            LOG.error(msg)
            return 3

    try:
        LOG.debug('Loading current config from file.')
        with open(kubeadm_cm_file, 'r') as file:
            kubeadm_cfg = yaml.load(file, Loader=yaml.RoundTripLoader)
            cluster_cfg = yaml.load(
                kubeadm_cfg['data']['ClusterConfiguration'], Loader=yaml.RoundTripLoader)
    except Exception as e:
        msg = str('Loading configmap from file. {}'.format(e))
        LOG.error(msg)
        return 3

    # -----------------------------------------------------------------------------
    # Load k8s service-parameters from hieradata
    # (updated by user through sysinv > service-parameter)
    # -----------------------------------------------------------------------------
    service_params = get_service_parameters_from_hieradata(
        hieradata_file, apiserver_schema, controller_manager_schema,
        scheduler_schema, kubelet_schema, etcd_schema)

    # -----------------------------------------------------------------------------
    # Building cluster_cfg from service-parameters/hieradata
    # The current (preloaded) cluster configuration is taken as base.
    # New cluster config from hieradata overrides pre existing values.
    # -----------------------------------------------------------------------------
    # Config section --------------------------------------------------------------
    if 'automatic_recovery' in service_params['config'].keys():
        # this value is set by sysinv, and its values are 'true' or 'false'
        value = service_params['config']['automatic_recovery']
        automatic_recovery = value == 'true'

    if 'timeout' in service_params['config'].keys():
        timeout = int(service_params['config']['timeout'])

    if 'tries' in service_params['config'].keys():
        tries = int(service_params['config']['tries'])

    if 'try_sleep' in service_params['config'].keys():
        try_sleep = int(service_params['config']['try_sleep'])

    # kube-apiserver section ------------------------------------------------------
    for param, value in service_params['apiServer'].items():
        if param in apiserver_schema['root'].keys():
            cluster_cfg['apiServer'][param] = value
        else:
            # By default all not known params will be placed in
            # section 'extraArgs'
            if 'extraArgs' not in cluster_cfg['apiServer'].keys():
                cluster_cfg['apiServer']['extraArgs'] = {}
            if param == 'enable-admission-plugins':
                value = _validate_admission_plugins(value)
                cluster_cfg['apiServer']['extraArgs'][param] = value
            else:
                cluster_cfg['apiServer']['extraArgs'][param] = value

    # remove all parameters in 'extraArgs' not present in service-parameter.
    if 'extraArgs' in cluster_cfg['apiServer'].keys():
        for param in list(cluster_cfg['apiServer']['extraArgs'].keys()):
            if param not in service_params['apiServer']:
                cluster_cfg['apiServer']['extraArgs'].pop(param)

    # apiserver_volumes section
    if cluster_cfg['apiServer'] and 'extraVolumes' in cluster_cfg['apiServer']:
        cluster_cfg['apiServer'].pop('extraVolumes')
    for param, value in service_params['apiServerVolumes'].items():
        if 'extraVolumes' not in cluster_cfg['apiServer'].keys():
            cluster_cfg['apiServer']['extraVolumes'] = []
        volume_dict, _ = sp.parse_volume_string_to_dict({'name': param, 'value': value})
        cluster_cfg['apiServer']['extraVolumes'].append(volume_dict)
        if export_configmap_from_volume(volume_dict, 'kube_apiserver_volumes') != 0:
            LOG.error('Exporting configmap from volume: %s', str(volume_dict))
            return 3

    # controller manager section --------------------------------------------------
    for param, value in service_params['controllerManager'].items():
        if param in controller_manager_schema['root'].keys():
            cluster_cfg['controllerManager'][param] = value
        else:
            # By default all not known params will be place in
            # section 'extraArgs'
            if 'extraArgs' not in cluster_cfg['controllerManager'].keys():
                cluster_cfg['controllerManager']['extraArgs'] = {}
            cluster_cfg['controllerManager']['extraArgs'][param] = value

    # remove all parameters in 'extraArgs' not present in service-parameter.
    if 'extraArgs' in cluster_cfg['controllerManager'].keys():
        for param in list(cluster_cfg['controllerManager']['extraArgs'].keys()):
            if param not in service_params['controllerManager']:
                cluster_cfg['controllerManager']['extraArgs'].pop(param)

    # controller_manager_volumes section
    if cluster_cfg['controllerManager'] and 'extraVolumes' in cluster_cfg['controllerManager']:
        cluster_cfg['controllerManager'].pop('extraVolumes')
    for param, value in service_params['controllerManagerVolumes'].items():
        if 'extraVolumes' not in cluster_cfg['controllerManager'].keys():
            cluster_cfg['controllerManager']['extraVolumes'] = []
        volume_dict, _ = sp.parse_volume_string_to_dict({'name': param, 'value': value})
        cluster_cfg['controllerManager']['extraVolumes'].append(volume_dict)
        if export_configmap_from_volume(volume_dict, 'kube_controller_manager_volumes') != 0:
            LOG.error('Exporting configmap from volume: %s', str(volume_dict))
            return 3

    # scheduler section -----------------------------------------------------------
    for param, value in service_params['scheduler'].items():
        if param in scheduler_schema['root'].keys():
            cluster_cfg['scheduler'][param] = value
        else:
            # By default all not known params will be place in
            # section 'extraArgs'
            if 'extraArgs' not in cluster_cfg['scheduler'].keys():
                cluster_cfg['scheduler']['extraArgs'] = {}
            cluster_cfg['scheduler']['extraArgs'][param] = value

    # remove all parameters not present in service-parameter.
    if 'extraArgs' in cluster_cfg['scheduler'].keys():
        for param in list(cluster_cfg['scheduler']['extraArgs'].keys()):
            if param not in service_params['scheduler']:
                cluster_cfg['scheduler']['extraArgs'].pop(param)

    # scheduler_volumes section
    if cluster_cfg['scheduler'] and 'extraVolumes' in cluster_cfg['scheduler']:
        cluster_cfg['scheduler'].pop('extraVolumes')
    for param, value in service_params['schedulerVolumes'].items():
        if 'extraVolumes' not in cluster_cfg['scheduler'].keys():
            cluster_cfg['scheduler']['extraVolumes'] = []
        volume_dict, _ = sp.parse_volume_string_to_dict({'name': param, 'value': value})
        cluster_cfg['scheduler']['extraVolumes'].append(volume_dict)
        if export_configmap_from_volume(volume_dict, 'kube_scheduler_volumes') != 0:
            LOG.error('Exporting configmap from volume: %s', str(volume_dict))
            return 3

    # etcd section ----------------------------------------------------------------
    for param, value in service_params['etcd'].items():
        # Prioritize user-defined arguments, otherwise, the values are taken from hieradata.
        value = etcd_cafile if param == 'caFile' and etcd_cafile else value
        value = etcd_certfile if param == 'certFile' and etcd_certfile else value
        value = etcd_keyfile if param == 'keyFile' and etcd_keyfile else value
        value = etcd_servers if param == 'endpoints' and etcd_servers else value

        # By default all not known params will be place in section 'external'
        if param in etcd_schema['root'].keys():
            cluster_cfg['etcd'][param] = value
        else:
            # params saved like list (value should be separated by comma)
            if param == 'endpoints':
                cluster_cfg['etcd']['external'][param] = value.split(',')
            # by default params are saved like strings
            else:
                cluster_cfg['etcd']['external'][param] = value

    # -----------------------------------------------------------------------------
    # Pre updating tasks and patch kubeadm configmap
    # -----------------------------------------------------------------------------
    # Ensure the yaml is constructed with proper formatting and tabbing
    cluster_cfg_str = yaml.dump(
        cluster_cfg, Dumper=yaml.RoundTripDumper, default_flow_style=False)
    cluster_cfg_str = yaml.scalarstring.PreservedScalarString(cluster_cfg_str)
    kubeadm_cfg['data']['ClusterConfiguration'] = cluster_cfg_str

    # Save updated kubeadm-config into file
    try:
        with open(kubeadm_cm_file, 'w') as file:
            yaml.dump(kubeadm_cfg, file, Dumper=yaml.RoundTripDumper,
                      default_flow_style=False)
    except Exception as e:
        LOG.error('Saving updated kubeadm-config into file. %s', e)
        return 3

    # Run mandatory tasks before the update proccess starts
    if pre_k8s_updating_tasks(post_k8s_tasks) != 0:
        LOG.error('Running pre updating tasks.')
        return 3

    # Patch kubeadm-config configmap with the updated configuration.
    if patch_k8s_kubeadm_configmap(kubeadm_cm_file) != 0:
        LOG.error('Parching kubeadm-config configmap.')
        return 3

    # Export the updated k8s cluster configuration
    if export_k8s_cluster_configuration(cluster_config_file) != 0:
        LOG.error('Exportando k8s cluster configuration.')
        return 3

    # -----------------------------------------------------------------------------
    # Update k8s kube-apiserver
    # -----------------------------------------------------------------------------
    update_k8s_control_plane_components(
        cluster_config_file, target_component='apiserver')

    # Wait for kube-apiserver to be up before executing next steps
    is_k8s_apiserver_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=APISERVER_READYZ_ENDPOINT)

    # Check kube-apiserver health, then backup and restore
    if automatic_recovery:
        if not is_k8s_apiserver_healthy:
            LOG.debug('kube-apiserver is not responding, intializing restore.')
            restore_rc = restore_k8s_control_plane_config(
                kubeadm_cm_bak_file, cluster_config_bak_file, configmap_patched_file,
                tries=tries, try_sleep=try_sleep, timeout=timeout)
            if restore_rc == 2:
                LOG.error("kube-apiserver has failed to start using backup configuration.")
                return 2
            if restore_rc == 1:
                return 1

    # Run mandatory tasks after the update proccess has finished
    post_k8s_updating_tasks(post_k8s_tasks)

    # -----------------------------------------------------------------------------
    # Update k8s kube-controller-manager
    # -----------------------------------------------------------------------------
    update_k8s_control_plane_components(
        cluster_config_file, target_component='controller-manager')

    # Wait for controller-manager to be up
    is_k8s_component_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=CONTROLLER_MANAGER_HEALTHZ_ENDPOINT)

    # Check kube-controller-manager health, then backup and restore
    if automatic_recovery:
        if not is_k8s_component_healthy:
            LOG.debug('kube-controller-manager is not responding, intializing restore.')
            restore_rc = restore_k8s_control_plane_config(
                kubeadm_cm_bak_file, cluster_config_bak_file, configmap_patched_file,
                tries=tries, try_sleep=try_sleep, timeout=timeout)

            if restore_rc == 2:
                msg = "kube-controller-manager has failed to start " +\
                      "using backup configuration."
                LOG.error(msg)
                return 2
            if restore_rc == 1:
                return 1

    # -----------------------------------------------------------------------------
    # Update k8s kube-scheduler
    # -----------------------------------------------------------------------------
    update_k8s_control_plane_components(
        cluster_config_file, target_component='scheduler')

    # Wait for scheduler to be up
    LOG.debug('Waiting for kube-scheduler be online.')
    is_k8s_component_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=SCHEDULER_HEALTHZ_ENDPOINT)

    # Check kube-scheduler health, then backup and restore
    if automatic_recovery:
        if not is_k8s_component_healthy:
            LOG.debug('kube-scheduler is not responding, intializing restore.')
            restore_rc = restore_k8s_control_plane_config(
                kubeadm_cm_bak_file, cluster_config_bak_file, configmap_patched_file,
                tries=tries, try_sleep=try_sleep, timeout=timeout)
            if restore_rc == 2:
                LOG.error("kube-scheduler has failed to start using backup configuration.")
                return 2
            if restore_rc == 1:
                return 1

    # -----------------------------------------------------------------------------
    # Update Kubelet
    # -----------------------------------------------------------------------------
    LOG.debug('Starting the kubelet update')

    # Building kubelet_cfg from service-parameters (hieradata)
    kubelet_cfg = get_kubelet_cfg_from_service_parameters(service_params)

    # Generates kubeadmin config file with KubeletConfiguration
    rc = generates_kubeadm_config_file(
        kubeadm_config_file=kubeadm_kubelet_config_file,
        new_kubelet_cfg=kubelet_cfg,
        kubelet_bak_config_file=kubelet_bak_config_file,
        cluster_cfg=cluster_cfg)
    if rc != 0:
        return 3

    # Updating Kubelet
    if update_k8s_kubelet(kubeadm_kubelet_config_file, kubelet_error_log) != 0:
        is_k8s_component_healthy = False
    else:
        LOG.debug('Waiting for kubelet be online.')
        is_k8s_component_healthy = k8s_health_check(
            timeout=timeout, try_sleep=try_sleep, tries=tries,
            healthz_endpoint=KUBELET_HEALTHZ_ENDPOINT)

    if not is_k8s_component_healthy:
        if not automatic_recovery:
            LOG.debug('Automatic recovery not enabled, exiting...')
            return 2

        # Restore Kubelet and Control-Plane (failure case)
        msg = 'Kubelet is not responding or an error occurred, initializing restore.'
        LOG.debug(msg)
        kubelet_restore_rc = restore_k8s_kubelet_config(
            kubeadm_kubelet_config_bak_file,
            error_log_file=kubelet_error_log + '.autorecovery',
            tries=tries, try_sleep=try_sleep, timeout=timeout)

        if kubelet_restore_rc == 1:
            return 1
        return 2

    # -----------------------------------------------------------------------------
    # Update backup files with latest configuration
    # -----------------------------------------------------------------------------
    LOG.debug("Check all k8s control-plane components are up and running.")
    is_k8s_apiserver_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=APISERVER_READYZ_ENDPOINT)
    is_k8s_controller_manager_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=CONTROLLER_MANAGER_HEALTHZ_ENDPOINT)
    is_k8s_scheduler_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=SCHEDULER_HEALTHZ_ENDPOINT)
    is_k8s_kubelet_healthy = k8s_health_check(
        timeout=timeout, try_sleep=try_sleep, tries=tries,
        healthz_endpoint=KUBELET_HEALTHZ_ENDPOINT)

    LOG.debug("Updating backup files with latest configuration ...")
    if is_k8s_apiserver_healthy and is_k8s_controller_manager_healthy and\
            is_k8s_scheduler_healthy and is_k8s_kubelet_healthy:
        # Update kubelet configmap and backup config file
        update_kubelet_bak_config_files(
            kubeadm_kubelet_config_file, kubeadm_kubelet_config_bak_file,
            kubelet_latest_config_file, kubelet_bak_config_file)
        update_kubelet_configmap(kubelet_latest_config_file)

        # Update control-plane backup files
        export_k8s_kubeadm_configmap(kubeadm_cm_bak_file)
        export_k8s_cluster_configuration(cluster_config_bak_file)

        LOG.debug("Successfully Updated.")
        return 0

    return rc


if __name__ == "__main__":
    sys.exit(main())
