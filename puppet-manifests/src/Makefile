#
# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2019-2022 Intel Corporation
#

BINDIR ?= /usr/local/bin
CONFIGDIR ?= /etc/puppet
MODULEDIR ?= /usr/share/puppet/modules

install:
ifdef ignore_puppet_warnings
	install -m 755 -D bin/puppet-manifest-apply-ignore-warnings.sh $(BINDIR)/puppet-manifest-apply.sh
else
	install -m 755 -D bin/puppet-manifest-apply.sh $(BINDIR)/puppet-manifest-apply.sh
endif
	install -m 755 -D bin/apply_network_config.sh $(BINDIR)/apply_network_config.sh
	install -m 755 -D bin/network_ifupdown.sh $(BINDIR)/network_ifupdown.sh
	install -m 755 -D bin/network_sysconfig.sh $(BINDIR)/network_sysconfig.sh
	install -m 755 -D bin/puppet-update-default-grub.sh $(BINDIR)/puppet-update-default-grub.sh
	install -m 755 -D bin/puppet-update-grub-env.py $(BINDIR)/puppet-update-grub-env.py
	install -m 755 -D bin/remove_unused_calico_hostendpoints.sh $(BINDIR)/remove_unused_calico_hostendpoints.sh
	install -d -m 0755 $(CONFIGDIR)
ifdef hiera_v5
	install -m 640 etc/hiera_v5.yaml $(CONFIGDIR)/hiera.yaml
else
	install -m 640 etc/hiera.yaml $(CONFIGDIR)
endif
	cp -R hieradata $(CONFIGDIR)/
	cp -R manifests $(CONFIGDIR)/
	install -d -m 0755 $(MODULEDIR)
	cp -R modules/platform $(MODULEDIR)/
	cp -R modules/openstack $(MODULEDIR)/
