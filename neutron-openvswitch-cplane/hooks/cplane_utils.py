import subprocess
import commands

from charmhelpers.contrib.openstack.utils import os_release
from charmhelpers.contrib.openstack import templating

from collections import OrderedDict
from charmhelpers.core.hookenv import (
    config,
    log as juju_log,
    relation_ids,
    relation_get,
    related_units,
    hook_name,
)

from charmhelpers.contrib.openstack.utils import (
    make_assess_status_func,
)

import charmhelpers.core.hookenv as hookenv

from charmhelpers.fetch import (
    apt_install,
)
import os

import cplane_context

from cplane_package_manager import(
    CPlanePackageManager
)

from cplane_network import (
    add_bridge,
    check_interface,
)

TEMPLATES = 'templates/'

cplane_packages = OrderedDict([
    ('cplane-neutron-plugin', -1),
    ('openvswitch-common', -1),
    ('openvswitch-datapath-dkms', -1),
    ('openvswitch-switch', -1),
    ('cplane-notification-driver', -1),
    ('cp-agentd', -1),
])

if config('cplane-version') == "1.3.5":
    cplane_packages['cplane-neutron-plugin'] = 439
    del cplane_packages['cplane-notification-driver']

neutron_config = {
    'rabbit_userid': config('rabbit-user'),
    'rabbit_virtual_host': config('rabbit-vhost'),
    'rabbit_password': 'password',
    'rabbit_host': 'localhost',
}

NEUTRON_CONF = '/etc/neutron/neutron.conf'

PACKAGES = ['neutron-metadata-agent', 'neutron-plugin-ml2', 'crudini',
            'dkms', 'iputils-arping', 'dnsmasq']


REQUIRED_INTERFACES = {
    'neutron-api-cplane': ['cplane-ovs'],
    'cplane-controller': ['cplane-controller'],
    'messaging': ['amqp'],
}

SERVICES = ['cp-agentd']

NEUTRON_METADATA_AGENT_CONF = '/etc/neutron/metadata_agent.ini'

METADATA_RESOURCE_MAP = OrderedDict([
    (NEUTRON_METADATA_AGENT_CONF, {
        'services': ['neutron-metadata-agent'],
        'contexts': [cplane_context.SharedSecretContext(),
                     cplane_context.APIIdentityServiceContext()],
    }),
])


CPLANE_URL = config('cp-package-url')

SYSTEM_CONF = '/etc/sysctl.conf'
system_config = OrderedDict([
    ('net.ipv4.conf.all.rp_filter', '0'),
    ('net.ipv4.ip_forward', '1'),
    ('net.ipv4.conf.default.rp_filter', '0'),
    ('net.bridge.bridge-nf-call-iptables', '1'),
    ('net.bridge.bridge-nf-call-ip6tables', '1'),
])


def register_configs(release=None):
    resources = METADATA_RESOURCE_MAP
    release = os_release('neutron-common')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resources.iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def api_ready(relation, key):
    ready = 'no'
    for rid in relation_ids(relation):
        for unit in related_units(rid):
            ready = relation_get(attribute=key, unit=unit, rid=rid)
    return ready == 'yes'


def is_neutron_api_ready():
    return api_ready('neutron-plugin-api-subordinate', 'neutron-api-ready')


def determine_packages():
    if get_os_release() == '16.04':
        PACKAGES.extend(['bc'])
    return PACKAGES


def disable_neutron_agent():
    cmd = ['service', 'neutron-plugin-openvswitch-agent', 'stop']
    subprocess.check_call(cmd)

    cmd = ['update-rc.d', 'neutron-plugin-openvswitch-agent', 'disable']
    subprocess.check_call(cmd)


def crudini_set(_file, section, key, value):
    option = '--set'
    cmd = ['crudini', option, _file, section, key, value]
    subprocess.check_call(cmd)


def cplane_config(data, config_file, section):
    for key, value in data.items():
        crudini_set(config_file, section, key, value)


def install_cplane_packages():

    cp_package = CPlanePackageManager(CPLANE_URL)
    for key, value in cplane_packages.items():
        filename = cp_package.download_package(key, value)
        cmd = ['dpkg', '-i', filename]
        subprocess.check_call(cmd)
        options = "--fix-broken"
        apt_install(options, fatal=True)


def manage_fip():
    for rid in relation_ids('cplane-controller'):
        for unit in related_units(rid):
            fip_mode = relation_get(attribute='fip-mode', unit=unit, rid=rid)
            if fip_mode == 'True':
                if check_interface(config('fip-interface')):
                    add_bridge('br-fip', config('fip-interface'))
                else:
                    juju_log('Fip interface doesnt exist, and \
                    will be used by default by Cplane controller')


def set_cp_agent():
    juju_log('Settig cp-agentd configuration for {} hook'.format(hook_name()))
    mport = 0
    for rid in relation_ids('cplane-controller'):
        for unit in related_units(rid):
            mport = relation_get(attribute='mport', unit=unit, rid=rid)
            uport = relation_get(attribute='uport', unit=unit, rid=rid)
            unicast_mode = config('enable-unicast')
            cplane_controller = relation_get('private-address')
            if mport:
                key = 'mcast-port=' + mport
                cmd = ['cp-agentd', 'set-config', key]
                subprocess.check_call(cmd)
                key = 'mgmt-iface=' + config('mgmt-int')
                cmd = ['cp-agentd', 'set-config', key]
                subprocess.check_call(cmd)
                if unicast_mode is True:
                    key = 'ucast-ip=' + cplane_controller
                    cmd = ['cp-agentd', 'set-config', key]
                    subprocess.check_call(cmd)
                else:
                    cmd = "sed -i '/ucast-ip/d' /etc/cplane/cp-config.json"
                    os.system(cmd)
                key = 'ucast-port=' + uport
                cmd = ['cp-agentd', 'set-config', key]
                subprocess.check_call(cmd)
                key = 'log-level=' + str(config('cp-agent-log-level'))
                with open('/etc/cplane/cp-config.json', 'r') as file:
                    filedata = file.read()
                if '"{}"'.format(config('cp-agent-log-level')) not in filedata:
                    cmd = ['cp-agentd', 'set-config', key]
                    subprocess.check_call(cmd)
                key = 'vm-mtu=' + str(config('cp-vm-mtu'))
                cmd = ['cp-agentd', 'set-config', key]
                subprocess.check_call(cmd)

                return

    key = 'mcast-port=' + str(config('cp-controller-mport'))
    cmd = ['cp-agentd', 'set-config', key]
    subprocess.check_call(cmd)
    key = 'mgmt-iface=' + config('mgmt-int')
    cmd = ['cp-agentd', 'set-config', key]
    subprocess.check_call(cmd)
    key = 'ucast-ip=' + config('cplane-controller-ip')
    cmd = ['cp-agentd', 'set-config', key]
    subprocess.check_call(cmd)
    key = 'ucast-port=' + str(config('cp-controller-uport'))
    cmd = ['cp-agentd', 'set-config', key]
    subprocess.check_call(cmd)
    key = 'log-level=' + str(config('cp-agent-log-level'))
    with open('/etc/cplane/cp-config.json', 'r') as file:
        filedata = file.read()
    if '"{}"'.format(config('cp-agent-log-level')) not in filedata:
        cmd = ['cp-agentd', 'set-config', key]
        subprocess.check_call(cmd)
    key = 'vm-mtu=' + str(config('cp-vm-mtu'))
    cmd = ['cp-agentd', 'set-config', key]
    subprocess.check_call(cmd)


def restart_services():
    cmd = ['service', 'neutron-metadata-agent', 'restart']
    subprocess.check_call(cmd)
    cmd = ['service', 'openvswitch-switch', 'restart']
    subprocess.check_call(cmd)
    juju_log('Restarting cp-agentd service for {} hook'.format(hook_name()))
    cmd = ['service', 'cp-agentd', 'stop']
    subprocess.check_call(cmd)
    cmd = ['service', 'cp-agentd', 'start']
    subprocess.check_call(cmd)

    cmd = ['update-rc.d', 'cp-agentd', 'enable']
    subprocess.check_call(cmd)


def restart_cp_agentd():
    juju_log('Restarting cp-agentd service for {} hook'.format(hook_name()))
    cmd = ['service', 'cp-agentd', 'restart']
    subprocess.check_call(cmd)


def assess_status(configs):
    assess_status_func(configs)()
    hookenv.application_version_set(
        config('cplane-version'))


def assess_status_func(configs):
    required_interfaces = REQUIRED_INTERFACES.copy()
    return make_assess_status_func(
        configs, required_interfaces, services=SERVICES
    )


class FakeOSConfigRenderer(object):
    def complete_contexts(self):
        interfaces = []
        for key, values in REQUIRED_INTERFACES.items():
            for value in values:
                for rid in relation_ids(value):
                    for unit in related_units(rid):
                        interfaces.append(value)
        return interfaces

    def get_incomplete_context_data(self, interfaces):
        return {}


def fake_register_configs():
    return FakeOSConfigRenderer()


def get_os_release():
    ubuntu_release = commands.getoutput('lsb_release -r')
    return ubuntu_release.split()[1]
