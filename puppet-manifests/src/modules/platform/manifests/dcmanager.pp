class platform::dcmanager::params (
  $api_port = undef,
  $region_name = undef,
  $domain_name = undef,
  $domain_admin = undef,
  $domain_pwd = undef,
  $service_name = 'dcmanager',
  $default_endpoint_type = 'internalURL',
  $service_create = false,
  $deploy_base_dir = '/opt/platform/deploy',
  $iso_base_dir_source = '/opt/platform/iso',
  $iso_base_dir_target = '/var/www/pages/iso',
) {
  include ::platform::params

  include ::platform::network::mgmt::params

  $system_mode = $::platform::params::system_mode

  # FQDN can be used after:
  # - after the bootstrap for any installation
  # - mate controller uses FQDN if mgmt::params::fqdn_ready is present
  #     mate controller can use FQDN before the bootstrap flag
  # - just AIO-SX can use FQDN during the an upgrade. For other installs
  #     the active controller in older release can resolve the .internal FQDN
  #     when the mate controller is updated to N+1 version
  if (!str2bool($::is_upgrade_do_not_use_fqdn) or $system_mode == 'simplex') {
    if (str2bool($::is_bootstrap_completed)) {
      $fqdn_ready = true
    } else {
      if ($::platform::network::mgmt::params::fqdn_ready != undef) {
        $fqdn_ready = $::platform::network::mgmt::params::fqdn_ready
      }
      else {
        $fqdn_ready = false
      }
    }
  }
  else {
    $fqdn_ready = false
  }

  if ($fqdn_ready) {
    $api_host = $::platform::params::controller_fqdn
  } else {
    $api_host = $::platform::network::mgmt::params::controller_address
  }
}


class platform::dcmanager
  inherits ::platform::dcmanager::params {
  if $::platform::params::distributed_cloud_role =='systemcontroller' {
    include ::platform::params
    include ::platform::amqp::params
    include ::platform::network::mgmt::params

    if $::platform::params::init_database {
      include ::dcmanager::db::postgresql
    }

    $system_mode = $::platform::params::system_mode

    # FQDN can be used after:
    # - after the bootstrap for any installation
    # - mate controller uses FQDN if mgmt::params::fqdn_ready is present
    #     mate controller can use FQDN before the bootstrap flag
    # - just AIO-SX can use FQDN during the an upgrade. For other installs
    #     the active controller in older release can resolve the .internal FQDN
    #     when the mate controller is updated to N+1 version
    if (!str2bool($::is_upgrade_do_not_use_fqdn) or $system_mode == 'simplex') {
      if (str2bool($::is_bootstrap_completed)) {
        $fqdn_ready = true
      } else {
        if ($::platform::network::mgmt::params::fqdn_ready != undef) {
          $fqdn_ready = $::platform::network::mgmt::params::fqdn_ready
        }
        else {
          $fqdn_ready = false
        }
      }
    }
    else {
      $fqdn_ready = false
    }

    class { '::dcmanager':
      rabbit_host     => (str2bool($fqdn_ready)) ? {
                            true    => $::platform::amqp::params::host,
                            default => $::platform::amqp::params::host_url,
                          },
      rabbit_port     => $::platform::amqp::params::port,
      rabbit_userid   => $::platform::amqp::params::auth_user,
      rabbit_password => $::platform::amqp::params::auth_password,
    }
    file {$iso_base_dir_source:
      ensure => directory,
      mode   => '0755',
    }
    file {$iso_base_dir_target:
      ensure => directory,
      mode   => '0755',
    }
    file {$deploy_base_dir:
      ensure => directory,
      mode   => '0755',
    }
  }
}

class platform::dcmanager::haproxy
  inherits ::platform::dcmanager::params {
  include ::platform::params
  include ::platform::haproxy::params

  if $::platform::params::distributed_cloud_role =='systemcontroller' {
    platform::haproxy::proxy { 'dcmanager-restapi':
      server_name  => 's-dcmanager',
      public_port  => $api_port,
      private_port => $api_port,
    }
  }

  # Configure rules for https enabled admin endpoint.
  if $::platform::params::distributed_cloud_role == 'systemcontroller' {
    platform::haproxy::proxy { 'dcmanager-restapi-admin':
      https_ep_type     => 'admin',
      server_name       => 's-dcmanager',
      public_ip_address => $::platform::haproxy::params::private_ip_address,
      public_port       => $api_port + 1,
      private_port      => $api_port,
    }
  }
}

class platform::dcmanager::manager {
  if $::platform::params::distributed_cloud_role =='systemcontroller' {
    include ::dcmanager::manager
  }
}

class platform::dcmanager::api
  inherits ::platform::dcmanager::params {
  if $::platform::params::distributed_cloud_role =='systemcontroller' {
    if ($::platform::dcmanager::params::service_create and
        $::platform::params::init_keystone) {
      include ::dcmanager::keystone::auth
    }

    class { '::dcmanager::api':
      bind_host => $api_host,
      sync_db   => $::platform::params::init_database,
    }


    include ::platform::dcmanager::haproxy
  }
}

class platform::dcmanager::fs::runtime {
  if $::platform::params::distributed_cloud_role == 'systemcontroller' {
    include ::platform::dcmanager::params
    $iso_base_dir_source = $::platform::dcmanager::params::iso_base_dir_source
    $iso_base_dir_target = $::platform::dcmanager::params::iso_base_dir_target
    $deploy_base_dir = $::platform::dcmanager::params::deploy_base_dir

    file {$iso_base_dir_source:
      ensure => directory,
      mode   => '0755',
    }

    file {$iso_base_dir_target:
      ensure => directory,
      mode   => '0755',
    }

    file {$deploy_base_dir:
      ensure => directory,
      mode   => '0755',
    }

    exec { "bind mount ${iso_base_dir_target}":
      command => "mount -o bind -t ext4 ${iso_base_dir_source} ${iso_base_dir_target}",
      require => File[ $iso_base_dir_source, $iso_base_dir_target ]
    }
  }
}

class platform::dcmanager::runtime {
  if $::platform::params::distributed_cloud_role == 'systemcontroller' {
    include ::platform::amqp::params
    include ::dcmanager
    include ::dcmanager::db::postgresql
    class { '::dcmanager::api':
      sync_db   => str2bool($::is_standalone_controller),
    }
  }
}
