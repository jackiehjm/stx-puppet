#
# Files in this package are licensed under Apache; see LICENSE file.
#
# Copyright (c) 2013-2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

#
# == Parameters
#
# [use_syslog]
#   Use syslog for logging.
#   (Optional) Defaults to false.
#
# [log_facility]
#   Syslog facility to receive log lines.
#   (Optional) Defaults to LOG_USER.

class sysinv (
  $database_connection         = '',
  $database_idle_timeout       = 3600,
  $database_max_pool_size      = 5,
  $database_max_overflow       = 10,
  $journal_max_size            = 51200,
  $journal_min_size            = 1024,
  $journal_default_size        = 1024,
  $rpc_backend                 = 'sysinv.openstack.common.rpc.impl_kombu',
  $rpc_backend_zeromq          = true,
  $rpc_zeromq_bind_ip              = '::',
  $rpc_zeromq_conductor_bind_ip    = '::',
  $rpc_zeromq_conductor_bind_port  = '9501',
  $rpc_zeromq_agent_bind_port      = '9502',
  $control_exchange            = 'openstack',
  $rabbit_host                 = '127.0.0.1',
  $rabbit_port                 = 5672,
  $rabbit_hosts                = false,
  $rabbit_virtual_host         = '/',
  $rabbit_userid               = 'guest',
  $rabbit_password             = false,
  $qpid_hostname               = 'localhost',
  $qpid_port                   = '5672',
  $qpid_username               = 'guest',
  $qpid_password               = false,
  $qpid_reconnect              = true,
  $qpid_reconnect_timeout      = 0,
  $qpid_reconnect_limit        = 0,
  $qpid_reconnect_interval_min = 0,
  $qpid_reconnect_interval_max = 0,
  $qpid_reconnect_interval     = 0,
  $qpid_heartbeat              = 60,
  $qpid_protocol               = 'tcp',
  $qpid_tcp_nodelay            = true,
  $package_ensure              = 'present',
  $api_paste_config            = '/etc/sysinv/api-paste.ini',
  $use_stderr                  = false,
  $log_file                    = 'sysinv.log',
  $log_dir                     = '/var/log/sysinv',
  $use_syslog                  = false,
  $log_facility                = 'LOG_USER',
  $verbose                     = false,
  $debug                       = false,
  $sysinv_api_port             = 6385,
  $sysinv_mtc_inv_label        = '/v1/hosts/',
  $region_name                 = 'RegionOne',
  $neutron_region_name         = 'RegionOne',
  $cinder_region_name          = 'RegionOne',
  $nova_region_name            = 'RegionOne',
  $barbican_region_name        = 'RegionOne',
  $fm_catalog_info             = undef,
  $fernet_key_repository       = undef,
  $periodic_interval_conductor = {  default => 60,
                                    agent_update_request => 60,
                                    kubernetes_local_secrets => 86400,
                                    deferred_runtime_config => 60,
                                    controller_config_active_apply => 60,
                                    upgrade_status => 180,
                                    install_states => 60,
                                    kubernetes_labels => 180,
                                    image_conversion => 60,
                                    storage_backend_failure => 400,
                                    k8s_application => 60,
                                    device_image_update => 300 },
  $periodic_interval_agent     = {  default => 60,
                                    inventory_audit => 60,
                                    lldp_audit => 300 }
) {

  include sysinv::params
  include ::platform::kubernetes::params
  include ::platform::docker::params

  Package['sysinv'] -> Sysinv_config<||>
  Package['sysinv'] -> Sysinv_api_paste_ini<||>
  Package['sysinv'] -> Certmon_config<||>
  Package['sysinv'] -> Certalarm_config<||>

  # this anchor is used to simplify the graph between sysinv components by
  # allowing a resource to serve as a point where the configuration of sysinv begins
  anchor { 'sysinv-start': }

  package { 'sysinv':
    ensure  => $package_ensure,
    name    => $::sysinv::params::package_name,
    require => Anchor['sysinv-start'],
  }

  file { $::sysinv::params::sysinv_conf:
    ensure  => present,
    owner   => 'sysinv',
    group   => 'sysinv',
    mode    => '0600',
    require => Package['sysinv'],
  }

  file { $::sysinv::params::sysinv_paste_api_ini:
    ensure  => present,
    owner   => 'sysinv',
    group   => 'sysinv',
    mode    => '0600',
    require => Package['sysinv'],
  }

  if $rpc_backend == 'sysinv.openstack.common.rpc.impl_kombu' {

    if ! $rabbit_password {
      fail('Please specify a rabbit_password parameter.')
    }

    sysinv_config {
      'DEFAULT/rabbit_password':     value => $rabbit_password, secret => true;
      'DEFAULT/rabbit_userid':       value => $rabbit_userid;
      'DEFAULT/rabbit_virtual_host': value => $rabbit_virtual_host;
      'DEFAULT/control_exchange':    value => $control_exchange;
    }

    if $rabbit_hosts {
      sysinv_config { 'DEFAULT/rabbit_hosts':     value => join($rabbit_hosts, ',') }
      sysinv_config { 'DEFAULT/rabbit_ha_queues': value => true }
    } else {
      sysinv_config { 'DEFAULT/rabbit_host':      value => $rabbit_host }
      sysinv_config { 'DEFAULT/rabbit_port':      value => $rabbit_port }
      sysinv_config { 'DEFAULT/rabbit_hosts':     value => "${rabbit_host}:${rabbit_port}" }
      sysinv_config { 'DEFAULT/rabbit_ha_queues': value => false }
    }
  }

  if $rpc_backend == 'sysinv.openstack.common.rpc.impl_qpid' {

    if ! $qpid_password {
      fail('Please specify a qpid_password parameter.')
    }

    sysinv_config {
      'DEFAULT/qpid_hostname':               value => $qpid_hostname;
      'DEFAULT/qpid_port':                   value => $qpid_port;
      'DEFAULT/qpid_username':               value => $qpid_username;
      'DEFAULT/qpid_password':               value => $qpid_password, secret => true;
      'DEFAULT/qpid_reconnect':              value => $qpid_reconnect;
      'DEFAULT/qpid_reconnect_timeout':      value => $qpid_reconnect_timeout;
      'DEFAULT/qpid_reconnect_limit':        value => $qpid_reconnect_limit;
      'DEFAULT/qpid_reconnect_interval_min': value => $qpid_reconnect_interval_min;
      'DEFAULT/qpid_reconnect_interval_max': value => $qpid_reconnect_interval_max;
      'DEFAULT/qpid_reconnect_interval':     value => $qpid_reconnect_interval;
      'DEFAULT/qpid_heartbeat':              value => $qpid_heartbeat;
      'DEFAULT/qpid_protocol':               value => $qpid_protocol;
      'DEFAULT/qpid_tcp_nodelay':            value => $qpid_tcp_nodelay;
    }
  }

  sysinv_config {
    'DEFAULT/verbose':             value => $verbose;
    'DEFAULT/debug':               value => $debug;
    'DEFAULT/api_paste_config':    value => $api_paste_config;
    'DEFAULT/rpc_backend':         value => $rpc_backend;
    'DEFAULT/rpc_backend_zeromq':             value => $rpc_backend_zeromq;
    'DEFAULT/rpc_zeromq_bind_ip':             value => $rpc_zeromq_bind_ip;
    'DEFAULT/rpc_zeromq_conductor_bind_ip':   value => $rpc_zeromq_conductor_bind_ip;
    'DEFAULT/rpc_zeromq_agent_bind_port':     value => $rpc_zeromq_agent_bind_port;
    'DEFAULT/rpc_zeromq_conductor_bind_port': value => $rpc_zeromq_conductor_bind_port;
  }

  # Automatically add psycopg2 driver to postgresql (only does this if it is missing)
  $real_connection = regsubst($database_connection,'^postgresql:','postgresql+psycopg2:')

  sysinv_config {
    'database/connection':               value => $real_connection, secret => true;
    'database/connection_recycle_time':  value => $database_idle_timeout;
    'database/max_pool_size':            value => $database_max_pool_size;
    'database/max_overflow':             value => $database_max_overflow;
  }

  sysinv_config {
    'journal/journal_max_size':     value => $journal_max_size;
    'journal/journal_min_size':     value => $journal_min_size;
    'journal/journal_default_size': value => $journal_default_size;
  }

  if $use_syslog {
    sysinv_config {
      'DEFAULT/use_syslog':           value => true;
      'DEFAULT/syslog_log_facility':  value => $log_facility;
    }
  } else {
    sysinv_config {
      'DEFAULT/use_syslog':           value => false;
      'DEFAULT/use_stderr':           value => false;
      'DEFAULT/log_file'  :           value => $log_file;
      'DEFAULT/log_dir'   :           value => $log_dir;
    }
  }

  sysinv_config {
    'DEFAULT/sysinv_api_port':         value => $sysinv_api_port;
    'DEFAULT/MTC_INV_LABEL':           value => $sysinv_mtc_inv_label;
  }

  sysinv_config {
    'keystone_authtoken/region_name':  value => $region_name;
    'openstack_keystone_authtoken/region_name':  value => $region_name;
    'openstack_keystone_authtoken/neutron_region_name':  value => $neutron_region_name;
    'openstack_keystone_authtoken/cinder_region_name':  value => $cinder_region_name;
    'openstack_keystone_authtoken/nova_region_name':  value => $nova_region_name;
    'openstack_keystone_authtoken/barbican_region_name':  value => $barbican_region_name;
  }

  sysinv_config {
    'fm/catalog_info':    value => $fm_catalog_info;
    'fm/os_region_name':  value => $region_name;
    'fernet_repo/key_repository':  value => $fernet_key_repository;
  }

  sysinv_config {
    'conductor_periodic_task_intervals/default':
        value => $periodic_interval_conductor[default];
    'conductor_periodic_task_intervals/agent_update_request':
        value => $periodic_interval_conductor[agent_update_request];
    'conductor_periodic_task_intervals/kubernetes_local_secrets':
        value => $periodic_interval_conductor[kubernetes_local_secrets];
    'conductor_periodic_task_intervals/deferred_runtime_config':
        value => $periodic_interval_conductor[deferred_runtime_config];
    'conductor_periodic_task_intervals/controller_config_active_apply':
        value => $periodic_interval_conductor[controller_config_active_apply];
    'conductor_periodic_task_intervals/upgrade_status':
        value => $periodic_interval_conductor[upgrade_status];
    'conductor_periodic_task_intervals/install_states':
        value => $periodic_interval_conductor[install_states];
    'conductor_periodic_task_intervals/kubernetes_labels':
        value => $periodic_interval_conductor[kubernetes_labels];
    'conductor_periodic_task_intervals/image_conversion':
        value => $periodic_interval_conductor[image_conversion];
    'conductor_periodic_task_intervals/storage_backend_failure':
        value => $periodic_interval_conductor[storage_backend_failure];
    'conductor_periodic_task_intervals/k8s_application':
        value => $periodic_interval_conductor[k8s_application];
    'conductor_periodic_task_intervals/device_image_update':
        value => $periodic_interval_conductor[device_image_update];
  }

  sysinv_config {
    'agent_periodic_task_intervals/default':
        value => $periodic_interval_agent[default];
    'agent_periodic_task_intervals/inventory_audit':
        value => $periodic_interval_agent[inventory_audit];
    'agent_periodic_task_intervals/lldp_audit':
        value => $periodic_interval_agent[lldp_audit];
  }

  sysinv_api_paste_ini {
    'filter:authtoken/region_name': value => $region_name;
  }

}
