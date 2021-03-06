import os
import subprocess
import json

from collections import OrderedDict
from charmhelpers.contrib.openstack.utils import os_release
from charmhelpers.contrib.openstack import templating
from charmhelpers.core.hookenv import (
    config,
    relation_ids,
    relation_get,
    related_units,
)

from charmhelpers.contrib.openstack.utils import (
    make_assess_status_func,
)
import cplane_context
import charmhelpers.core.hookenv as hookenv

from cplane_package_manager import(
    CPlanePackageManager
)

from charmhelpers.fetch import (
    apt_install,
)

TEMPLATES = 'templates/'
CHARM_LIB_DIR = os.environ.get('CHARM_DIR', '') + "/lib/"

ML2_CONFIG = '/etc/neutron/plugins/ml2/ml2_conf.ini'

neutron_config = {"neutron.ml2.mechanism_drivers":
                  {"cplane_mech": ("neutron.plugins.ml2.drivers."
                                   "cplane.mechanism_cplane:CPlaneMechanism")},
                  "neutron.service_plugins":
                  {"cplane_l3": ("neutron.services.l3_router.cplane."
                                 "l3_router_plugin:CPlaneServicePlugin")},
                  "neutron.core_plugins":
                  {"cplane_core_plugin": ("neutron.plugins.cplane."
                                          "cplane_plugin:CPlaneNeutronPlugin"),
                   "cplane_ml2": ("neutron.plugins.ml2.cplane."
                                  "cplane_ml2_plugin:CPlaneML2Plugin")},
                  "neutron.ml2.extension_drivers":
                  {"cplane_qos": ("neutron.plugins.ml2.extensions."
                                  "cplane_qos:CpQosExtensionDriver")}}

cplane_packages = OrderedDict([('cplane-neutron-plugin', -1),
                               ('cplane-neutronclient-extension', -1),
                               ('cplane-nova-extension', -1),
                               ('neutronclient', -1)])

if config('cplane-version') == "1.3.5":
    cplane_packages['cplane-neutron-plugin'] = 439
    del cplane_packages['cplane-neutronclient-extension']
    del cplane_packages['cplane-nova-extension']

if config('cplane-version') == "1.3.7" or "1.3.8":
    del cplane_packages['neutronclient']

PACKAGES = ['neutron-plugin-ml2', 'crudini', 'python-bitarray']

CPLANE_URL = config('cp-package-url')

REQUIRED_INTERFACES = {
    'database': ['shared-db'],
    'cplane-controller': ['cplane-controller'],
    'messaging': ['amqp'],
}

SERVICES = ['neutron-server']


def determine_packages():
    return PACKAGES


def register_configs(release=None):
    resources = OrderedDict([
        (ML2_CONFIG, {
            'services': ['neutron-server'],
            'contexts': [cplane_context.CplaneMl2Context(), ]
        })
    ])
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


def crudini_set(_file, section, key, value):
    option = '--set'
    cmd = ['crudini', option, _file, section, key, value]
    subprocess.check_call(cmd)


def cplane_config(data, config_file):
    for section in data:
        _data = data.get(section)
        for key in _data:
            value = _data.get(key, "")
            crudini_set(config_file, section, key, value)


def install_cplane_packages():
    cp_package = CPlanePackageManager(CPLANE_URL)
    for key, value in cplane_packages.items():
        filename = cp_package.download_package(key, value)
        if key == "neutronclient":
            cmd = ['tar', '-xvf', filename, '-C',
                   '/usr/lib/python2.7/dist-packages/']
            subprocess.check_call(cmd)
        else:
            cmd = ['dpkg', '-i', filename]
            subprocess.check_call(cmd)
            options = "--fix-broken"
            apt_install(options, fatal=True)


def create_link():
    cmd = ['rm', '-f', '/etc/neutron/plugin.ini']
    subprocess.check_call(cmd)
    cmd = ['ln', '-s', ML2_CONFIG, '/etc/neutron/plugin.ini']
    subprocess.check_call(cmd)


def restart_service():
    cmd = ['service', 'neutron-server', 'restart']
    subprocess.check_call(cmd)


def migrate_db():
    cmd = ['neutron-db-manage', '--config-file', '/etc/neutron/neutron.conf',
           '--config-file', '/etc/neutron/plugins/ml2/ml2_conf.ini', 'upgrade',
           'head']
    subprocess.check_call(cmd)


def configure_policy():
    policy_file = "/etc/neutron/policy.json"
    data = json.load(open(policy_file))
    data["create_floatingip:floating_ip_address"] = "rule:admin_or_owner"
    data["get_ogr"] = ""
    data["get_ogrs"] = ""
    data["delete_ogr"] = "rule:admin_only"
    data["update_ogr"] = "rule:admin_or_owner"
    if config('cplane-version') == "1.3.8":
        data["update_floatingip_quota"] = "rule:admin_or_owner"
        data["get_floatingip_quota"] = "rule:admin_or_owner"
        data["get_floatingip_quotas"] = ""
    json.dump(data, open(policy_file, 'w'), indent=4)


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


def add_controller_ip():
    cplane_controller = ''
    for rid in relation_ids('cplane-controller'):
        for unit in related_units(rid):
            mport = relation_get(attribute='mport', unit=unit, rid=rid)
            data = relation_get(rid=rid, unit=unit)
            if cplane_controller == '':
                cplane_controller = data['private-address']
            else:
                cplane_controller = (cplane_controller + ',' +
                                     data['private-address'])
            if mport:
                cmd = "sed -ie 's/cplane_controller_hosts.*/cplane_controller_\
hosts = {}/g' /etc/neutron/plugins/ml2/ml2_conf.ini".format(cplane_controller)
                os.system(cmd)
                restart_service()
