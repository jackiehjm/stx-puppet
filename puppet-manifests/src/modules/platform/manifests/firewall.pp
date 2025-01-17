define platform::firewall::rule (
  $service_name,
  $chain = 'INPUT',
  $destination = undef,
  $ensure = present,
  $host = 'ALL',
  $jump  = undef,
  $outiface = undef,
  $ports = undef,
  $proto = 'tcp',
  $table = undef,
  $tosource = undef,
) {

  include ::platform::params
  include ::platform::network::oam::params

  $ip_version = $::platform::network::oam::params::subnet_version

  $provider = $ip_version ? {
    6 => 'ip6tables',
    default => 'iptables',
  }

  $source = $host ? {
    'ALL' => $ip_version ? {
      6  => '::/0',
      default => '0.0.0.0/0'
    },
    default => $host,
  }

  $heading = $chain ? {
    'OUTPUT' => 'outgoing',
    'POSTROUTING' => 'forwarding',
    default => 'incoming',
  }

  # NAT rule
  if $jump == 'SNAT' or $jump == 'MASQUERADE' {
    firewall { "500 ${service_name} ${heading} ${title}":
      ensure      => $ensure,
      table       => $table,
      proto       => $proto,
      outiface    => $outiface,
      jump        => $jump,
      tosource    => $tosource,
      destination => $destination,
      source      => $source,
      provider    => $provider,
      chain       => $chain,
    }
  }
  else {
    if $ports == undef {
      firewall { "500 ${service_name} ${heading} ${title}":
        ensure   => $ensure,
        proto    => $proto,
        action   => 'accept',
        source   => $source,
        provider => $provider,
        chain    => $chain,
      }
    }
    else {
      firewall { "500 ${service_name} ${heading} ${title}":
        ensure   => $ensure,
        proto    => $proto,
        dport    => $ports,
        action   => 'accept',
        source   => $source,
        provider => $provider,
        chain    => $chain,
      }
    }
  }
}

class platform::firewall::calico::oam::services {
  include ::platform::params
  include ::platform::network::oam::params
  include ::platform::nfv::params
  include ::platform::fm::params
  include ::platform::patching::params
  include ::platform::sysinv::params
  include ::platform::smapi::params
  include ::platform::ceph::params
  include ::openstack::barbican::params
  include ::openstack::keystone::params
  include ::openstack::horizon::params
  include ::platform::dcmanager::params
  include ::platform::dcorch::params
  include ::platform::docker::params

  $ip_version = $::platform::network::oam::params::subnet_version

  # icmp
  $t_icmp_proto = $ip_version ? {
    6 => 'ICMPv6',
    default => 'ICMP'
  }

  # udp
  $sm_port = [2222, 2223]
  $ntp_port = [123]
  $ptp_port = [319, 320]

  # tcp
  $ssh_port = [22]

  if $::platform::fm::params::service_enabled {
    $fm_port = [$::platform::fm::params::api_port]
  } else {
    $fm_port = []
  }

  $nfv_vim_port = [$::platform::nfv::params::api_port]
  $patching_port = [$::platform::patching::params::public_port]
  $sysinv_port = [$::platform::sysinv::params::api_port]
  $sm_api_port = [$::platform::smapi::params::port]
  $docker_registry_port = [$::platform::docker::params::registry_port]
  $docker_token_port = [$::platform::docker::params::token_port]
  $kube_apiserver_port = [6443]

  if $::platform::ceph::params::service_enabled {
    $ceph_radosgw_port = [$::platform::ceph::params::rgw_port]
  } else {
    $ceph_radosgw_port = []
  }

  $barbican_api_port = [$::openstack::barbican::params::api_port]

  $keystone_port = [$::openstack::keystone::params::api_port]

  if $::platform::params::distributed_cloud_role != 'subcloud'  {
    if $::openstack::horizon::params::enable_https {
      $horizon_port = [$::openstack::horizon::params::https_port]
    } else {
      $horizon_port = [$::openstack::horizon::params::http_port]
    }
  } else {
    $horizon_port = []
  }

  if $::platform::params::distributed_cloud_role == 'systemcontroller' {
    $dc_port = [$::platform::dcmanager::params::api_port,
                $::platform::dcorch::params::sysinv_api_proxy_port,
                $::platform::dcorch::params::patch_api_proxy_port,
                $::platform::dcorch::params::identity_api_proxy_port]
  } else {
    $dc_port = []
  }

  $t_ip_version = $ip_version
  $t_udp_ports = concat($sm_port, $ntp_port, $ptp_port)
  $t_tcp_ports = concat($ssh_port,
                        $fm_port, $nfv_vim_port, $patching_port, $sysinv_port, $sm_api_port,
                        $kube_apiserver_port, $docker_registry_port, $docker_token_port,
                        $ceph_radosgw_port, $barbican_api_port, $keystone_port, $horizon_port,
                        $dc_port)

  $file_name = '/tmp/gnp_all_oam.yaml'
  $oam_if_gnp = 'controller-oam-if-gnp'
  file { $file_name:
      ensure  => file,
      content => template('platform/calico_oam_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
  }
  # Remove annotation as it contains last-applied-configuration with
  # resourceVersion in it, which will require the gnp re-apply to
  # provide a matching resourceVersion in the yaml file.
  -> exec { "remove annotation from ${oam_if_gnp}":
    path    => '/usr/bin:/usr/sbin:/bin',
    command => @("CMD"/L),
      kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
      ${oam_if_gnp} kubectl.kubernetes.io/last-applied-configuration-
      | CMD
    onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${oam_if_gnp}"
  }
  -> exec { "apply resource ${file_name}":
    path    => '/usr/bin:/usr/sbin:/bin',
    command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name}",
    onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
  }
}

class platform::firewall::calico::oam::endpoints {
  include ::platform::params
  include ::platform::network::oam::params

  $host = $::platform::params::hostname
  $oam_if = $::platform::network::oam::params::interface_name
  $oam_addr = $::platform::network::oam::params::interface_address

  # create/update host endpoint to represent oam interface
  $file_name_oam = "/tmp/hep_${host}_oam.yaml"
  file { $file_name_oam:
    ensure  => file,
    content => template('platform/calico_oam_if_hep.yaml.erb'),
    owner   => 'root',
    group   => 'root',
    mode    => '0640',
  }
  -> exec { "apply resource ${file_name_oam}":
    path    => '/usr/bin:/usr/sbin:/bin',
    command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_oam}",
    onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
  }
}

class platform::firewall::calico::controller {
  contain ::platform::firewall::calico::oam::endpoints
  contain ::platform::firewall::calico::oam::services
  contain ::platform::firewall::calico::mgmt
  contain ::platform::firewall::calico::cluster_host
  contain ::platform::firewall::calico::pxeboot
  contain ::platform::firewall::calico::storage
  contain ::platform::firewall::calico::admin
  contain ::platform::firewall::calico::hostendpoint

  Class['::platform::kubernetes::gate'] -> Class[$name]

  Class['::platform::firewall::calico::oam::endpoints']
  -> Class['::platform::firewall::calico::oam::services']
  -> Class['::platform::firewall::calico::mgmt']
  -> Class['::platform::firewall::calico::cluster_host']
  -> Class['::platform::firewall::calico::pxeboot']
  -> Class['::platform::firewall::calico::storage']
  -> Class['::platform::firewall::calico::admin']
  -> Class['::platform::firewall::calico::hostendpoint']
}

class platform::firewall::calico::worker {
  contain ::platform::firewall::calico::mgmt
  contain ::platform::firewall::calico::cluster_host
  contain ::platform::firewall::calico::pxeboot
  contain ::platform::firewall::calico::storage
  contain ::platform::firewall::calico::hostendpoint

  Class['::platform::kubernetes::worker'] -> Class[$name]

  Class['::platform::firewall::calico::mgmt']
  -> Class['::platform::firewall::calico::cluster_host']
  -> Class['::platform::firewall::calico::pxeboot']
  -> Class['::platform::firewall::calico::storage']
  -> Class['::platform::firewall::calico::hostendpoint']
}

class platform::firewall::runtime {
  include ::platform::firewall::calico::oam::endpoints
  include ::platform::firewall::calico::oam::services
  include ::platform::firewall::calico::mgmt
  include ::platform::firewall::calico::cluster_host
  include ::platform::firewall::calico::pxeboot
  include ::platform::firewall::calico::storage
  include ::platform::firewall::calico::admin
  include ::platform::firewall::calico::hostendpoint

  Class['::platform::firewall::calico::oam::endpoints']
  -> Class['::platform::firewall::calico::oam::services']
  -> Class['::platform::firewall::calico::mgmt']
  -> Class['::platform::firewall::calico::cluster_host']
  -> Class['::platform::firewall::calico::pxeboot']
  -> Class['::platform::firewall::calico::storage']
  -> Class['::platform::firewall::calico::admin']
  -> Class['::platform::firewall::calico::hostendpoint']
}

class platform::firewall::calico::mgmt (
  $config = {}
) {
  if $config != {} {
    $yaml_config = hash2yaml($config)
    $gnp_name = "${::personality}-mgmt-if-gnp"
    $file_name_gnp = "/tmp/gnp_${gnp_name}.yaml"
    file { $file_name_gnp:
      ensure  => file,
      content => template('platform/calico_platform_network_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
    }
    # Remove annotation as it contains last-applied-configuration with
    # resourceVersion in it, which will require the gnp re-apply to
    # provide a matching resourceVersion in the yaml file.
    -> exec { "remove annotation from ${gnp_name}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => @("CMD"/L),
        kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
        ${gnp_name} kubectl.kubernetes.io/last-applied-configuration-
        | CMD
      onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${gnp_name}"
    }
    -> exec { "apply resource ${file_name_gnp}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_gnp}",
      onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
    }
  }
}

class platform::firewall::calico::cluster_host  (
  $config = {}
) {
  if $config != {} {
    $yaml_config = hash2yaml($config)
    $gnp_name = "${::personality}-cluster_host-if-gnp"
    $file_name_gnp = "/tmp/gnp_${gnp_name}.yaml"
    file { $file_name_gnp:
      ensure  => file,
      content => template('platform/calico_platform_network_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
    }
    # Remove annotation as it contains last-applied-configuration with
    # resourceVersion in it, which will require the gnp re-apply to
    # provide a matching resourceVersion in the yaml file.
    -> exec { "remove annotation from ${gnp_name}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => @("CMD"/L),
        kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
        ${gnp_name} kubectl.kubernetes.io/last-applied-configuration-
        | CMD
      onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${gnp_name}"
    }
    -> exec { "apply resource ${file_name_gnp}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_gnp}",
      onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
    }
  }
}

class platform::firewall::calico::pxeboot  (
  $config = {}
) {
  if $config != {} {
    $yaml_config = hash2yaml($config)
    $gnp_name = "${::personality}-pxeboot-if-gnp"
    $file_name_gnp = "/tmp/gnp_${gnp_name}.yaml"
    file { $file_name_gnp:
      ensure  => file,
      content => template('platform/calico_platform_network_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
    }
    # Remove annotation as it contains last-applied-configuration with
    # resourceVersion in it, which will require the gnp re-apply to
    # provide a matching resourceVersion in the yaml file.
    -> exec { "remove annotation from ${gnp_name}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => @("CMD"/L),
        kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
        ${gnp_name} kubectl.kubernetes.io/last-applied-configuration-
        | CMD
      onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${gnp_name}"
    }
    -> exec { "apply resource ${file_name_gnp}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_gnp}",
      onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
    }
  }
}

class platform::firewall::calico::storage  (
  $config = {}
) {
  if $config != {} {
    $yaml_config = hash2yaml($config)
    $gnp_name = "${::personality}-storage-if-gnp"
    $file_name_gnp = "/tmp/gnp_${gnp_name}.yaml"
    file { $file_name_gnp:
      ensure  => file,
      content => template('platform/calico_platform_network_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
    }
    # Remove annotation as it contains last-applied-configuration with
    # resourceVersion in it, which will require the gnp re-apply to
    # provide a matching resourceVersion in the yaml file.
    -> exec { "remove annotation from ${gnp_name}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => @("CMD"/L),
        kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
        ${gnp_name} kubectl.kubernetes.io/last-applied-configuration-
        | CMD
      onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${gnp_name}"
    }
    -> exec { "apply resource ${file_name_gnp}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_gnp}",
      onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
    }
  }
}

class platform::firewall::calico::admin  (
  $config = {}
) {
  if $config != {} {
    $yaml_config = hash2yaml($config)
    $gnp_name = "${::personality}-admin-if-gnp"
    $file_name_gnp = "/tmp/gnp_${gnp_name}.yaml"
    file { $file_name_gnp:
      ensure  => file,
      content => template('platform/calico_platform_network_if_gnp.yaml.erb'),
      owner   => 'root',
      group   => 'root',
      mode    => '0640',
    }
    # Remove annotation as it contains last-applied-configuration with
    # resourceVersion in it, which will require the gnp re-apply to
    # provide a matching resourceVersion in the yaml file.
    -> exec { "remove annotation from ${gnp_name}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => @("CMD"/L),
        kubectl --kubeconfig=/etc/kubernetes/admin.conf annotate globalnetworkpolicies.crd.projectcalico.org \
        ${gnp_name} kubectl.kubernetes.io/last-applied-configuration-
        | CMD
      onlyif  => "kubectl --kubeconfig=/etc/kubernetes/admin.conf get globalnetworkpolicies.crd.projectcalico.org ${gnp_name}"
    }
    -> exec { "apply resource ${file_name_gnp}":
      path    => '/usr/bin:/usr/sbin:/bin',
      command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_gnp}",
      onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
    }
  }
}

class platform::firewall::calico::hostendpoint (
  $config = {}
) {
  $active_heps = keys($config)
  if $config != {} {
    $config.each |$key, $value| {
      # create/update host endpoint
      $file_name_hep = "/tmp/hep_${key}.yaml"
      $yaml_config = hash2yaml($value)
      file { $file_name_hep:
        ensure  => file,
        content => template('platform/calico_platform_firewall_if_hep.yaml.erb'),
        owner   => 'root',
        group   => 'root',
        mode    => '0640',
      }
      -> exec { "apply resource ${file_name_hep}":
        path    => '/usr/bin:/usr/sbin:/bin',
        command => "kubectl --kubeconfig=/etc/kubernetes/admin.conf apply -f ${file_name_hep}",
        onlyif  => 'kubectl --kubeconfig=/etc/kubernetes/admin.conf get customresourcedefinitions.apiextensions.k8s.io'
      }
    }
  }
  # storage nodes do not run k8s
  if $::personality != 'storage' {
    exec { "get active hostendepoints: ${active_heps}":
      command => "echo ${active_heps} > /tmp/hep_active.txt",
    }
    -> exec { 'remove unused hostendepoints':
      command => 'remove_unused_calico_hostendpoints.sh',
      onlyif  => 'test -f /tmp/hep_active.txt'
    }
  }
}